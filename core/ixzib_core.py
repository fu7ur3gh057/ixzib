"""Ixzib core: mic -> VAD (energy) -> Whisper -> WebSocket events for the widget."""
import asyncio, json, queue, time
import os, re, sys, subprocess, uuid, threading, collections, difflib, random
import numpy as np
import sounddevice as sd
from num2words import num2words
import websockets
from claude_agent_sdk import (ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, TextBlock,
                              ToolUseBlock, ResultMessage, PermissionResultAllow, PermissionResultDeny)

# make tools installed in the venv (yt-dlp) visible to Claude's Bash tool
os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")


def _first_name():
    try:
        full = subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=3).stdout.strip()
        return full.split()[0] if full else ""
    except Exception:
        return ""


def load_config():
    """~/.ixzib/config.json: {"name": "...", "workdir": "~", "safe_root": "~"}"""
    cfg = {"name": "", "workdir": "~", "safe_root": "~"}
    path = os.path.expanduser(os.environ.get("IXZIB_CONFIG", "~/.ixzib/config.json"))
    try:
        with open(path) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        print("config error:", e, flush=True)
    if not cfg.get("name"):
        cfg["name"] = _first_name()
    return cfg


CFG = load_config()
USER_NAME = CFG["name"]
WORKDIR = os.path.expanduser(CFG["workdir"])
SAFE_ROOT = os.path.expanduser(CFG["safe_root"])
os.makedirs(WORKDIR, exist_ok=True)

SR = 16000
MODEL = "mlx-community/whisper-large-v3-turbo"
SILENCE_RMS = 0.012      # below = silence
SILENCE_SEC = 1.2        # auto-stop after this much silence (after speech began)
MAX_SEC = 30

READONLY = {"Read", "Glob", "Grep", "LS", "WebSearch", "WebFetch"}
SYSTEM = ("You are Ixzib, a voice assistant wrapping Claude Code. The user speaks to you; input is "
          "speech-to-text so expect small errors. Reply in the user's language (usually Russian). "
          + (f"The user's name is {USER_NAME}; address them by name only occasionally. " if USER_NAME else "") +
          "This is a live, continuous voice conversation: be natural, warm and brief. "
          "SPOKEN REPLIES MUST BE VERY SHORT: one sentence of at most ~12 words for any action or confirmation, "
          "at most 2-3 short sentences otherwise. Never narrate what you are doing (no 'сейчас поищу', 'нашёл...'); "
          "just give the result. No markdown, no code, no lists. "
          "To play music or a video: find the top result with `yt-dlp --get-id --get-title \"ytsearch1:<query>\"`, "
          "then END your reply with the tag [[play:https://www.youtube.com/watch?v=<ID>]]. Do NOT run `open` yourself: "
          "the app opens the link only after you finish speaking, so the music never starts under your voice. "
          "Your spoken line must be tiny, e.g. 'Включаю Cry Me a River.' For ambiguous song names prefer the official "
          "video of the most famous artist. "
          "Conversation control: end EVERY reply with exactly one hidden tag after the last sentence. "
          "[[open]] is the DEFAULT: use it for any normal answer, even a complete one, because the user may follow up. "
          "[[wait]]: you asked the user a question or need their answer. "
          "[[end]]: ONLY when the user clearly wraps up (goodbye, 'that\'s all', 'thanks, done', a closing 'ok/понял/ясно' with nothing else) "
          "or you tried and truly cannot help further (dead end). Then reply with one short, warm closing line. "
          "Never mention or explain the tags.")

YOLO = os.environ.get("IXZIB_YOLO") == "1"   # skip ALL confirmations
RISKY = re.compile(
    r"\brm\b|\bsudo\b|\bmv\b.*\s/|git\s+(push|reset|clean|checkout\s+--|branch\s+-D)|\bkill(all)?\b|\bpkill\b"
    r"|\bchmod\b|\bchown\b|\bdd\b|\bmkfs|\bdiskutil|curl[^|]*\|\s*(ba|z)?sh|>\s*/(etc|usr|System|Library)|\bshutdown|\breboot|\bdrop\s+(table|database)",
    re.I)


def needs_confirm(tool, inp):
    if YOLO or tool in READONLY:
        return False
    if tool == "Bash":
        return bool(RISKY.search(inp.get("command", "")))
    if tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        p = os.path.realpath(os.path.expanduser(inp.get("file_path", "")))
        return not p.startswith(SAFE_ROOT + os.sep)
    return False


# ---------------- TTS (Silero, offline) ----------------
VOICE = "eugene"
TTS_SR = 48000
tts_model = None
_tts_lock = threading.Lock()
speak_task = None
convo_task = None
phase = "idle"          # idle | listening | thinking | speaking (inside a conversation)


def tts_load():
    global tts_model
    import torch
    torch.set_num_threads(4)
    tts_model, _ = torch.hub.load("snakers4/silero-models", "silero_tts", language="ru", speaker="v5_ru", trust_repo=True)


CTRL_RE = re.compile(r"\[\[\s*(end|wait|open)\s*\]\]", re.I)
PLAY_RE = re.compile(r"\[\[\s*play\s*:\s*(https?://[^\]\s]+)\s*\]\]", re.I)
pending_play = None          # URL to open AFTER the spoken reply has finished


def split_ctrl(t):
    """-> (text without control tags, last tag or None). A [[play:URL]] tag is remembered in pending_play."""
    global pending_play
    for u in PLAY_RE.findall(t):
        pending_play = u
    t = PLAY_RE.sub("", t)
    found = CTRL_RE.findall(t)
    return CTRL_RE.sub("", t).strip(), (found[-1].lower() if found else None)


def clean_for_tts(t):
    t = PLAY_RE.sub(" ", CTRL_RE.sub(" ", t))
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"https?://\S+", "ссылка", t)
    t = re.sub(r"^\s*[-•*]\s+", "", t, flags=re.M)
    t = re.sub(r"[*_#>|~]+", " ", t)
    t = re.sub(r"\d{1,12}", lambda m: num2words(int(m.group()), lang="ru"), t)
    return re.sub(r"\s+", " ", t).strip()


def split_sentences(t):
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", t) if p.strip()]
    out = []
    for p in parts:                       # glue very short fragments, break very long ones
        if out and len(out[-1]) < 25:
            out[-1] += " " + p
        else:
            out.append(p)
    final = []
    for p in out:
        while len(p) > 350:
            cut = p.rfind(",", 0, 350)
            cut = cut if cut > 80 else 350
            final.append(p[:cut + 1].strip()); p = p[cut + 1:].strip()
        if p:
            final.append(p)
    return final


def synth(sentence):
    try:
        with _tts_lock:
            a = tts_model.apply_tts(text=sentence, speaker=VOICE, sample_rate=TTS_SR, put_accent=True, put_yo=True)
        return np.concatenate([a.numpy().astype(np.float32), np.zeros(int(0.12 * TTS_SR), dtype=np.float32)])
    except Exception as e:
        print("TTS error:", e, "|", sentence[:60], flush=True)
        return None


async def play(audio):
    sd.play(audio, TTS_SR)
    dur, t0 = len(audio) / TTS_SR, time.time()
    while (el := time.time() - t0) < dur:
        w = audio[int(el * TTS_SR): int(el * TTS_SR) + 2400]
        rms = float(np.sqrt(np.mean(w ** 2))) if len(w) else 0.0
        await broadcast(level=min(1.0, rms * 5))
        await asyncio.sleep(0.05)


MAX_SPOKEN_CHARS, MAX_SPOKEN_SENTENCES = 260, 3


def cap_spoken(sents):
    """Speak only the first few sentences; the full answer stays in the chat."""
    out, total = [], 0
    for x in sents:
        if out and (total + len(x) > MAX_SPOKEN_CHARS or len(out) >= MAX_SPOKEN_SENTENCES):
            break
        out.append(x)
        total += len(x)
    return out


async def speak(text, rewake=True):
    sents = cap_spoken(split_sentences(clean_for_tts(text)))
    print(f"[tts] speak: {len(sents)} sentence(s)", flush=True)
    if not sents:
        if rewake:
            set_wake(True)
        return
    await broadcast(state="speaking", label="")
    jobs = [asyncio.create_task(asyncio.to_thread(synth, x)) for x in sents]
    try:
        for j in jobs:
            audio = await j
            if audio is None:
                print("[tts] synth returned nothing", flush=True)
                continue
            print(f"[tts] playing {len(audio)/TTS_SR:.1f}s peak={float(np.abs(audio).max()):.2f} device={sd.default.device}", flush=True)
            await play(audio)
    except asyncio.CancelledError:
        raise
    except Exception:
        import traceback
        print("[tts] ERROR:\n" + traceback.format_exc(), flush=True)
    finally:
        sd.stop()
        for j in jobs:
            j.cancel()
        await broadcast(state="idle", label="", level=0)
        if rewake:
            set_wake(True)


async def stop_speaking():
    global speak_task
    if speak_task and not speak_task.done():
        speak_task.cancel()
        await asyncio.gather(speak_task, return_exceptions=True)
    speak_task = None


clients = set()
pending = {}      # perm id -> Future[bool]
brain = None
listening = False
loop = None


history = []          # chat log shown in the terminal client


async def chat(role, text, **kw):
    ev = {"role": role, "text": text, "ts": time.time(), **kw}
    history.append(ev)
    del history[:-500]
    await broadcast(chat=ev)


async def broadcast(**msg):
    data = json.dumps(msg, ensure_ascii=False)
    await asyncio.gather(*(c.send(data) for c in list(clients)), return_exceptions=True)


record_abort = threading.Event()


def record_blocking(level_cb, idle_timeout=9.0):
    """Record until silence after speech, or MAX_SEC. Returns float32 mono array."""
    q = queue.Queue()
    chunks, spoke, last_voice = [], False, time.time()

    def cb(indata, frames, t, status):
        q.put(indata[:, 0].copy())

    start = time.time()
    with sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=1600, callback=cb):
        while time.time() - start < MAX_SEC and not record_abort.is_set():
            try:
                x = q.get(timeout=0.5)
            except queue.Empty:
                continue
            chunks.append(x)
            rms = float(np.sqrt(np.mean(x ** 2)))
            level_cb(min(1.0, rms * 12))
            if rms > SILENCE_RMS:
                spoke, last_voice = True, time.time()
            elif spoke and time.time() - last_voice > SILENCE_SEC:
                break
            elif not spoke and time.time() - start > idle_timeout:
                break  # nobody spoke
    if record_abort.is_set():
        return None
    return np.concatenate(chunks) if chunks and spoke else None


_tr_lock = threading.Lock()


def transcribe(audio, language=None):
    import mlx_whisper
    kw = {"language": language} if language else {}
    with _tr_lock:
        r = mlx_whisper.transcribe(audio, path_or_hf_repo=MODEL, **kw)
    return r["text"].strip()


# ---------------- wake word ("эй Иксзиб") ----------------
WAKE_ON = os.environ.get("IXZIB_WAKE", "1") != "0"
SPEAK_TYPED = os.environ.get("IXZIB_SPEAK_TYPED", "1") != "0"   # also read out replies to typed messages
wake_enabled = threading.Event()


def set_wake(on):
    (wake_enabled.set if on and WAKE_ON else wake_enabled.clear)()


WAKE_NAMES = ("иксзиб", "икзиб", "иксиб", "ixzib", "ixib", "икссиб", "иксзип", "экзип", "экзиб", "эксзиб", "эксзип", "икзип", "эксип", "иксип", "экзим", "эксзим", "иксзим", "икзим", "гзип", "гзиб", "гсип", "ксзиб", "xzip", "xzib", "exhib", "exzip", "exib", "exip", "ixzip", "ixip", "ekzip", "ekzib", "ixhib", "exeb")


def _wake_ratio(w):
    return max(difflib.SequenceMatcher(None, w, name).ratio() for name in WAKE_NAMES)


def match_wake(text):
    """Return the command after the wake word ('' if none), or None if no wake word near the start."""
    words = list(re.finditer(r"[A-Za-zА-Яа-яЁё]+", text))
    for i, m in enumerate(words[:3]):
        w = m.group().lower()
        if w in ("эй", "hey", "хей", "ей", "ээй"):
            continue
        if len(w) >= 4 and _wake_ratio(w) >= 0.75:
            return text[m.end():].lstrip(" ,.!?:;-—").strip()
        if i + 1 < len(words):                       # "икс зиб" said as two words
            nxt = words[i + 1]
            if _wake_ratio(w + nxt.group().lower()) >= 0.8:
                return text[nxt.end():].lstrip(" ,.!?:;-—").strip()
    return None


def wake_loop():
    q = queue.Queue()
    floor = 0.004
    while True:
        if not wake_enabled.is_set():
            time.sleep(0.1)
            continue
        try:
            while not q.empty():
                q.get_nowait()
            pre, seg, in_sp, last_voice, start = collections.deque(maxlen=3), [], False, 0.0, 0.0
            with sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=1600,
                                callback=lambda i, f, t, st: q.put(i[:, 0].copy())):
                print("[wake] listening for wake word", flush=True)
                while wake_enabled.is_set():
                    try:
                        x = q.get(timeout=0.3)
                    except queue.Empty:
                        continue
                    rms = float(np.sqrt(np.mean(x ** 2)))
                    now = time.time()
                    if rms > max(SILENCE_RMS, floor * 3.5):
                        if not in_sp:
                            in_sp, seg, start = True, list(pre), now
                        seg.append(x)
                        last_voice = now
                    else:
                        if not in_sp:
                            floor += (rms - floor) * 0.05
                            pre.append(x)
                        else:
                            seg.append(x)
                    if in_sp and (now - last_voice > 0.6 or now - start > 12):
                        audio, seg, in_sp = np.concatenate(seg), [], False
                        pre.clear()
                        dur = len(audio) / SR
                        print(f"[wake] segment {dur:.1f}s peak={float(np.abs(audio).max()):.3f} floor={floor:.4f}", flush=True)
                        if 0.35 < dur < 13:
                            text = transcribe(audio, language="ru")
                            print(f"[wake] heard: {text!r}", flush=True)
                            cmd = match_wake(text)
                            if cmd is not None:
                                print("WAKE:", text, flush=True)
                                set_wake(False)
                                asyncio.run_coroutine_threadsafe(on_wake(cmd), loop)
        except Exception as e:
            print("wake error:", e, flush=True)
            time.sleep(1)


def describe(tool, inp):
    d = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("path") or ""
    return f"{tool}: {str(d)[:70]}"


async def can_use_tool(tool, inp, ctx):
    if not needs_confirm(tool, inp):
        return PermissionResultAllow()
    pid = uuid.uuid4().hex[:8]
    fut = asyncio.get_running_loop().create_future()
    pending[pid] = fut
    await broadcast(perm={"id": pid, "text": describe(tool, inp)}, state="thinking", label="разрешить?")
    await chat("system", f"Разрешить {describe(tool, inp)} ? (y/n)")
    try:
        ok = await asyncio.wait_for(fut, 60)
    except asyncio.TimeoutError:
        ok = False
    pending.pop(pid, None)
    await broadcast(perm=None)
    await chat("system", "разрешено" if ok else "отклонено")
    return PermissionResultAllow() if ok else PermissionResultDeny(message="User denied")


async def ask_claude(text):
    global pending_play
    pending_play = None
    await broadcast(state="thinking", label="думаю…", answer="")
    await brain.query(text)
    final, flag = "", "open"
    async for msg in brain.receive_response():
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, ToolUseBlock):
                    await broadcast(state="working", label=describe(b.name, b.input))
                    await chat("tool", describe(b.name, b.input))
                elif isinstance(b, TextBlock) and b.text.strip():
                    clean, f = split_ctrl(b.text)
                    if f:
                        flag = f
                    if clean:
                        final = clean
                        await chat("assistant", clean)
        elif isinstance(msg, ResultMessage):
            clean, f = split_ctrl(msg.result or final)
            if f:
                flag = f
            final = clean or final
            sid = getattr(msg, "session_id", None)
            if sid:
                try:
                    d = os.path.expanduser("~/.ixzib"); os.makedirs(d, exist_ok=True)
                    with open(os.path.join(d, "session.json"), "w") as f:
                        json.dump({"session_id": sid, "cwd": WORKDIR}, f)
                except Exception as e:
                    print("session save error:", e, flush=True)
    print(f"IXZIB[{flag}]:", final, flush=True)
    await broadcast(state="idle", label="", answer=final)
    return final, flag


def _task_done(task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        import traceback
        print("[task] ERROR:", "".join(traceback.format_exception(exc)), flush=True)
        set_wake(True)


def _lowpass(x, fc, sr, order=4):
    """Zero-phase FFT low-pass (steep, no ringing loops in Python)."""
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1.0 / sr)
    return np.fft.irfft(X / (1.0 + (f / fc) ** order), n=len(x))


def _voice(f_t, cutoff_t, sr, harmonics=12, detune=1.0):
    """Saw-like drone whose low-pass cutoff sweeps over time (warm, never bright)."""
    ph = 2 * np.pi * np.cumsum(f_t * detune) / sr
    out = np.zeros_like(f_t)
    for k in range(1, harmonics + 1):
        out += (1.0 / k) * np.sin(k * ph) / (1.0 + ((k * f_t) / cutoff_t) ** 4)
    return out


def _sweep_noise(n, sr, c0, c1, width=0.9, seed=1):
    """Band-limited noise whose centre frequency glides from c0 to c1 (airy whoosh)."""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n + 2048)
    N, hop = 1024, 256
    win = np.hanning(N)
    f = np.fft.rfftfreq(N, 1.0 / sr)
    out = np.zeros(n + 2 * N)
    for i in range(0, n, hop):
        c = c0 * (c1 / c0) ** (i / max(n, 1))
        gain = np.exp(-(np.log2(np.maximum(f, 1.0) / c) / width) ** 2)
        out[i:i + N] += np.fft.irfft(np.fft.rfft(x[i:i + N] * win) * gain, N) * win
    out = out[:n]
    return out / max(float(np.abs(out).max()), 1e-6)


def _reverb(x, sr, rt, seed, mix):
    """Dark, spacious tail: convolve with decaying low-passed noise."""
    m = int(rt * sr)
    rng = np.random.default_rng(seed)
    ir = rng.standard_normal(m) * np.exp(-np.arange(m) / sr * 6.9 / rt)
    ir = _lowpass(ir, 1800.0, sr, 2)
    ir /= np.sqrt(np.sum(ir ** 2))
    size = 1 << int(np.ceil(np.log2(len(x) + m)))
    y = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[:len(x) + m]
    return np.concatenate([x, np.zeros(m)]) + mix * y


def make_cue(kind, sr=TTS_SR):
    """Two soft cosmic cues (no bells, no bright partials):
    listen : 'power-up'   - low hum + airy whoosh rising and opening        (~0.4 s + short tail)
    standby: 'power-down' - hum sinking and closing, whoosh fading out      (~0.9 s + longer tail)
    """
    if kind == "listen":
        T, tail, gain = 0.40, 0.22, 0.24
        t = np.arange(int(sr * T)) / sr; u = t / T
        f_t = 70.0 * (3.0 ** u)                                   # 70 -> 210 Hz
        cut = 250.0 * (4.4 ** u)                                  # filter opens 250 -> 1100 Hz
        env = np.sin(np.pi * np.clip(u / 0.85, 0, 1) * 0.5) ** 2 * np.clip((1 - u) / 0.22, 0, 1) ** 0.7
        hum = (_voice(f_t, cut, sr) + 0.8 * _voice(f_t, cut, sr, detune=1.007)) * env
        whoosh = _sweep_noise(len(t), sr, 300.0, 1500.0, 0.9, seed=3) * np.sin(np.pi * u) ** 2 * 0.55
        dry = hum / max(float(np.abs(hum).max()), 1e-6) * 0.7 + whoosh
        rt, mix = 0.18, 0.50
    else:
        T, tail, gain = 0.90, 0.50, 0.24
        t = np.arange(int(sr * T)) / sr; u = t / T
        f_t = 190.0 * (0.30 ** u)                                 # 190 -> 57 Hz
        cut = 1000.0 * (0.14 ** u)                                # filter closes 1000 -> 140 Hz
        env = np.minimum(1.0, t / 0.06) * np.exp(-u * 2.6)
        hum = (_voice(f_t, cut, sr) + 0.8 * _voice(f_t, cut, sr, detune=0.993)) * env
        whoosh = _sweep_noise(len(t), sr, 1000.0, 130.0, 0.9, seed=5) * np.minimum(1.0, t / 0.05) * np.exp(-u * 4.0) * 0.5
        dry = hum / max(float(np.abs(hum).max()), 1e-6) * 0.7 + whoosh
        rt, mix = 0.60, 0.65
    dry = _lowpass(dry, 1400.0, sr, 4)                            # nothing bright survives
    L = _reverb(dry, sr, rt, 11, mix)
    R = _reverb(dry, sr, rt, 23, mix)
    out = np.stack([L, R], axis=1)
    out *= gain / max(float(np.abs(out).max()), 1e-6)
    n = len(out)
    fade = np.clip((n - np.arange(n)) / (0.05 * sr), 0, 1)       # no click at the end
    return (out * fade[:, None]).astype(np.float32)


_CUES = {}


async def cue(kind):
    """Play a cue and wait for it to finish (so the mic never records it)."""
    arr = _CUES.get(kind)
    if arr is None:
        arr = _CUES[kind] = make_cue(kind)
    sd.play(arr, TTS_SR)
    await asyncio.sleep(len(arr) / TTS_SR + 0.05)


_TR = {"iya": "ия", "sch": "щ", "ey": "ей", "iy": "ий", "ia": "ия", "ii": "ий", "ay": "ай", "oy": "ой", "ov": "ов", "er": "ер", "sh": "ш", "ch": "ч", "zh": "ж", "kh": "х", "ts": "ц", "yu": "ю", "ya": "я", "yo": "ё", "ye": "е",
       "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и", "j": "дж", "k": "к",
       "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в",
       "w": "в", "x": "кс", "y": "и", "z": "з"}


def spoken_name(name):
    """Latin name -> Cyrillic so the Russian voice can pronounce it."""
    if not re.search(r"[A-Za-z]", name):
        return name
    low, out, i = name.lower(), "", 0
    while i < len(low):
        for ln in (3, 2, 1):
            chunk = low[i:i + ln]
            if chunk in _TR:
                out += _TR[chunk]; i += ln; break
        else:
            out += low[i]; i += 1
    return out.capitalize()


def _lines(templates):
    nm = spoken_name(USER_NAME)
    return [t.replace("{name}", nm) for t in templates if USER_NAME or "{name}" not in t]


ACKS = _lines(["Слушаю, {name}.", "Да, {name}?", "Я здесь, {name}.", "Слушаю тебя.", "Да?"])
BYES = _lines(["Пока, {name}!", "До связи, {name}.", "Хорошо, до встречи.", "Обращайся!"])
JUNK = ("продолжение следует", "субтитры", "редактор субтитров", "спасибо за просмотр", "подписывайтесь", "dimatorzok", "корректор")
BYE_START = ("пока", "стоп", "хватит", "до свидания", "до встречи", "отбой", "на этом всё", "на этом все",
             "заверши", "закончим", "закончили", "всё спасибо", "все спасибо", "спасибо пока", "спасибо хватит", "спасибо всё", "спасибо все")


def is_junk(text):
    t = text.lower()
    return any(j in t for j in JUNK)


def is_goodbye(text):
    t = re.sub(r"[^\w\s]", " ", text.lower())
    words = t.split()
    while words and (difflib.SequenceMatcher(None, words[0], "иксзиб").ratio() >= 0.6 or words[0] in ("эй", "ну", "ixzib", "xzip")):
        words = words[1:]
    t = " ".join(words)
    return 0 < len(words) <= 3 and any(t == k or t.startswith(k + " ") for k in BYE_START)


async def flush_play():
    """Open a pending [[play:URL]] now (after the voice reply is done). Returns True if something was opened."""
    global pending_play
    url, pending_play = pending_play, None
    if not url:
        return False
    print("[play]", url, flush=True)
    await chat("tool", f"▶ {url}")
    subprocess.Popen(["open", url])
    return True


async def speak_wait(text):
    """Speak and wait until finished (or interrupted). Does not re-enable the wake word."""
    global speak_task, phase
    phase = "speaking"
    speak_task = asyncio.create_task(speak(text, rewake=False))
    speak_task.add_done_callback(_task_done)
    await asyncio.gather(speak_task, return_exceptions=True)      # interruption (click / hotkey) just ends the wait


async def capture_text(idle_timeout=9.0):
    global phase
    phase = "listening"
    record_abort.clear()
    await broadcast(state="listening", label="слушаю…")
    try:
        await cue("listen")
    except Exception as e:
        print("cue error:", e, flush=True)
    lv = lambda v: asyncio.run_coroutine_threadsafe(broadcast(level=v), loop)
    audio = await asyncio.to_thread(record_blocking, lv, idle_timeout)
    if audio is None:
        return None
    phase = "thinking"
    await broadcast(state="thinking", label="распознаю…")
    return await asyncio.to_thread(transcribe, audio)


async def turn(text, via):
    global phase
    phase = "thinking"
    print("YOU:", text, flush=True)
    await broadcast(label=text[:40], you=text)
    await chat("user", text, via=via)
    return await ask_claude(text)


async def converse(first=None, ack=None):
    """A continuous voice conversation: listen -> think -> speak -> listen ... until goodbye or silence."""
    global listening, phase
    listening = True
    set_wake(False)
    reason = "тишина"
    try:
        if ack:
            await speak_wait(ack)
            await asyncio.sleep(0.35)                      # let the room go quiet before listening
        text, strikes, idle = first, 0, 7.0
        while True:
            if text is None:
                text = await capture_text(idle)
                if not text:
                    break                                   # nobody spoke -> end of conversation
                if is_junk(text):
                    strikes += 1
                    text = None
                    if strikes >= 2:
                        break
                    continue
            if is_goodbye(text):
                await chat("user", text, via="voice")
                reason = "прощание"
                await speak_wait(random.choice(BYES))
                break
            final, flag = await turn(text, "voice")
            text, strikes = None, 0
            if final:
                await speak_wait(final)
                await asyncio.sleep(0.35)
            if await flush_play():                           # music / video started -> stand by so the mic does not hear it
                reason = "включил музыку"
                break
            if flag == "end":                              # finished / dead end -> back to standby (wake word)
                reason = "разговор исчерпан"
                break
            idle = 14.0 if flag == "wait" else 7.0          # it asked a question -> be more patient
    except asyncio.CancelledError:
        reason = "завершено вручную"
        raise
    finally:
        record_abort.set()
        listening, phase = False, "idle"
        await broadcast(state="idle", label="", level=0)
        await chat("system", f"диалог завершён ({reason})")
        try:
            await cue("standby")                              # distinct "going to standby" sound
        except Exception as e:
            print("cue error:", e, flush=True)
        set_wake(True)


def start_convo(first=None, ack=None):
    global convo_task
    if convo_task and not convo_task.done():
        return
    convo_task = asyncio.create_task(converse(first=first, ack=ack))
    convo_task.add_done_callback(_task_done)


async def typed_turn(txt):
    """A message typed in the chat: one turn (optionally spoken)."""
    global listening, speak_task
    listening = True
    set_wake(False)
    final = None
    try:
        final, _flag = await turn(txt, "kbd")
    finally:
        listening = False
        if not final:
            set_wake(True)
    if final and SPEAK_TYPED:
        speak_task = asyncio.create_task(speak(final))
        speak_task.add_done_callback(_task_done)
        await asyncio.gather(speak_task, return_exceptions=True)
    elif final:
        set_wake(True)
    await flush_play()


async def on_wake(cmd):
    await broadcast(state="listening", label="")
    if len(re.sub(r"\W+", "", cmd)) >= 3:
        start_convo(first=cmd)                             # run the command right away
    else:
        start_convo(ack=random.choice(ACKS))               # just the name -> "Слушаю, <имя>."


async def handler(ws):
    global speak_task
    clients.add(ws)
    await ws.send(json.dumps({"state": "idle", "label": "ixzib", "name": USER_NAME}, ensure_ascii=False))
    await ws.send(json.dumps({"history": history}, ensure_ascii=False))
    try:
        async for raw in ws:
            m = json.loads(raw)
            if m.get("cmd") == "toggle":
                in_convo = convo_task is not None and not convo_task.done()
                if speak_task is not None and not speak_task.done():      # barge-in: stop talking, listen now
                    await stop_speaking()
                    if not in_convo and not listening:
                        start_convo()
                elif in_convo:
                    if phase == "listening":                              # click while it listens = end the conversation
                        record_abort.set()
                        convo_task.cancel()
                elif not listening:
                    start_convo()
            elif m.get("cmd") == "text":
                txt = (m.get("text") or "").strip()
                if txt:
                    await stop_speaking()
                    if listening:
                        await chat("system", "занят, дождись ответа…")
                    else:
                        asyncio.create_task(typed_turn(txt)).add_done_callback(_task_done)
            elif m.get("cmd") == "say":          # debug: speak arbitrary text through the real pipeline
                await stop_speaking()
                speak_task = asyncio.create_task(speak(m.get("text") or "Проверка голоса."))
            elif m.get("cmd") == "perm" and m["id"] in pending and not pending[m["id"]].done():
                pending[m["id"]].set_result(bool(m.get("allow")))
    except websockets.ConnectionClosed:
        pass                                  # a client (chat / widget) went away: not an error
    finally:
        clients.discard(ws)


async def main():
    global loop, brain
    loop = asyncio.get_running_loop()
    brain = ClaudeSDKClient(ClaudeAgentOptions(
        cwd=WORKDIR, system_prompt={"type": "preset", "preset": "claude_code", "append": SYSTEM},
        can_use_tool=can_use_tool, permission_mode="default"))
    await brain.connect()
    print("warming up whisper…", flush=True)
    await asyncio.to_thread(transcribe, np.zeros(SR, dtype=np.float32))
    print("loading voice…", flush=True)
    await asyncio.to_thread(tts_load)
    await asyncio.to_thread(synth, "Готов.")
    threading.Thread(target=wake_loop, daemon=True).start()
    set_wake(True)
    async with websockets.serve(handler, "127.0.0.1", 8765):
        print("core ready on ws://127.0.0.1:8765", flush=True)
        await asyncio.Future()


def setup_models():
    """Download everything the first launch needs (called by install.sh)."""
    print("Скачиваю модель распознавания речи (Whisper, ~1.6 ГБ)…", flush=True)
    transcribe(np.zeros(SR, dtype=np.float32))
    print("Скачиваю голос (Silero)…", flush=True)
    tts_load()
    synth("Готов.")
    print("Модели готовы.", flush=True)


if __name__ == "__main__":
    if "--setup" in sys.argv:
        setup_models()
        sys.exit(0)
    asyncio.run(main())
