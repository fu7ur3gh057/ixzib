"""Ixzib chat: a futuristic terminal client for the live conversation.

The history lives in the Ixzib core, not in this tab: close the tab any time, open a new one and
the whole conversation is replayed.

Run:  .venv/bin/python ixzib_chat.py      (connects to the core on ws://127.0.0.1:8765)
"""
import asyncio
import json
import os
import signal
import threading
import time
from collections import deque

import websockets
from rich import box
from rich.align import Align
from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Static

URL = os.environ.get("IXZIB_WS", "ws://127.0.0.1:8765")
YES = {"y", "yes", "д", "да", "ok", "ок"}
NO = {"n", "no", "н", "нет"}

CYAN, VIOLET, PINK, AMBER, GREEN, GREY = "#00e5ff", "#a855f7", "#ff5fa2", "#ffb454", "#3ddc97", "#6b7094"
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BARS = " ▁▂▃▄▅▆▇█"
STATUS = {  # state -> (glyph, label, colour)
    "idle": ("●", "ГОТОВ", GREEN),
    "listening": ("◉", "СЛУШАЮ", CYAN),
    "thinking": ("◌", "ДУМАЮ", VIOLET),
    "working": ("⚙", "РАБОТАЮ", AMBER),
    "speaking": ("♪", "ГОВОРЮ", PINK),
    "offline": ("○", "ОФФЛАЙН", GREY),
}


def gradient(s, c1=(0, 229, 255), c2=(192, 132, 252), bold=True):
    t, n = Text(), max(len(s) - 1, 1)
    for i, ch in enumerate(s):
        r, g, b = (int(c1[k] + (c2[k] - c1[k]) * i / n) for k in range(3))
        t.append(ch, style=f"{'bold ' if bold else ''}rgb({r},{g},{b})")
    return t


def stamp(ev):
    return time.strftime("%H:%M", time.localtime(ev.get("ts", time.time())))


def render(ev):
    role, body = ev.get("role"), ev.get("text", "")
    if role == "user":
        via = "🎤 голос" if ev.get("via") == "voice" else "⌨  клавиатура"
        return Panel(Text(body, style="bold #eafcff"), box=box.ROUNDED, border_style=CYAN, padding=(0, 2),
                     title=Text.assemble(("  ТЫ ", f"bold {CYAN}"), (f"· {via} ", GREY)), title_align="left",
                     subtitle=Text(f" {stamp(ev)} ", style=GREY), subtitle_align="right")
    if role == "assistant":
        return Panel(Markdown(body), box=box.DOUBLE, border_style=VIOLET, padding=(0, 2),
                     title=Text.assemble(("  ◆ ", f"bold {VIOLET}"), gradient("IXZIB ", (192, 132, 252), (255, 95, 162))),
                     title_align="left", subtitle=Text(f" {stamp(ev)} ", style=GREY), subtitle_align="right",
)
    if role == "tool":
        return Text.assemble(("╰─ ⚙ ", GREY), (body, f"italic {GREY}"))
    return Align.center(Text.assemble(("◇ ", AMBER), (body, f"italic {AMBER}"), (" ◇", AMBER)))


WELCOME = Align.center(Group(
    Text(""),
    gradient("╭─────────────────────────────╮", bold=False),
    Align.center(Text.assemble(("│    ", "#4b5078"), gradient("I   X   Z   I   B"), ("    │", "#4b5078"))),
    gradient("╰─────────────────────────────╯", (192, 132, 252), (0, 229, 255), bold=False),
    Text(""),
    Text("голосовой ИИ поверх Claude Code", style=GREY, justify="center"),
    Text(""),
    Text.assemble(("скажи ", GREY), ("«Эй, Иксзиб»", f"bold {CYAN}"), (" или напиши ниже", GREY), justify="center"),
))


class Chat(App):
    CSS = f"""
    Screen {{ background: ansi_default; }}
    #top {{ height: 3; padding: 1 2 0 2; background: ansi_default; border-bottom: hkey #1d2140; }}
    #brand {{ width: 1fr; }}
    #status {{ width: auto; content-align: right middle; }}
    #log {{ background: ansi_default; padding: 0 1; scrollbar-size-vertical: 1; scrollbar-background: ansi_default;
           scrollbar-color: #2b3060; scrollbar-color-hover: {VIOLET}; }}
    .msg {{ height: auto; }}
    .user {{ margin: 1 1 0 12; }}
    .assistant {{ margin: 1 12 0 1; }}
    .tool {{ margin: 0 12 0 3; }}
    .system {{ margin: 1 6 0 6; }}
    .welcome {{ height: auto; margin: 3 0 0 0; }}
    #bar {{ dock: bottom; height: auto; background: ansi_default; }}
    #typing {{ height: 1; padding: 0 3; }}
    #input {{ margin: 0 2; border: round #2e3470; background: ansi_default; color: #eaf0ff; padding: 0 1; }}
    #input:focus {{ border: round {CYAN}; background: ansi_default; }}
    #hint {{ height: 1; padding: 0 3; color: #444a73; }}
    """
    BINDINGS = [("ctrl+l", "clear", "Очистить"), ("ctrl+q", "quit", "Выход"),
                ("pageup", "page(-1)", ""), ("pagedown", "page(1)", "")]

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static(id="brand")
            yield Static(id="status")
        yield VerticalScroll(id="log")
        with Vertical(id="bar"):
            yield Static(id="typing")
            yield Input(placeholder="❯  напиши сообщение…", id="input")
            yield Static("Enter — отправить  ·  PgUp/PgDn — прокрутка  ·  Ctrl+L — очистить  ·  Ctrl+Q — закрыть чат", id="hint")

    def on_mount(self) -> None:
        self.title = "Ixzib"
        self.ws, self.perm, self.welcome = None, None, None
        self.state, self.label, self.frame = "offline", "", 0
        self.levels = deque([0.0] * 10, maxlen=10)
        self.query_one("#brand", Static).update(Text.assemble(gradient("◢ IXZIB ◣"), ("   voice · claude code", GREY)))
        self.show_welcome()
        self.refresh_status()
        self.query_one("#input", Input).focus()
        self.set_interval(0.1, self.tick)
        self.run_worker(self.connect_loop(), exclusive=True)

    # ----- rendering -----
    def show_welcome(self):
        if self.welcome is None:
            self.welcome = Static(WELCOME, classes="welcome")
            self.query_one("#log", VerticalScroll).mount(self.welcome)

    def add(self, ev, scroll=True):
        log = self.query_one("#log", VerticalScroll)
        if self.welcome is not None:
            self.welcome.remove()
            self.welcome = None
        log.mount(Static(render(ev), classes=f"msg {ev.get('role', 'system')}"))
        if scroll:
            self.call_after_refresh(log.scroll_end, animate=False)

    def refresh_status(self):
        glyph, name, color = STATUS.get(self.state, STATUS["idle"])
        t = Text.assemble((f"{glyph} {name}", f"bold {color}"))
        if self.state in ("listening", "speaking"):
            t.append("  ")
            t.append("".join(BARS[min(8, int(v * 8))] for v in self.levels), style=color)
        self.query_one("#status", Static).update(t)

    def tick(self) -> None:
        self.frame += 1
        if self.state not in ("listening", "speaking"):
            self.levels.append(0.0)
        typing = self.query_one("#typing", Static)
        if self.state in ("thinking", "working"):
            color = STATUS[self.state][2]
            typing.update(Text.assemble((SPIN[self.frame % len(SPIN)] + " ", f"bold {color}"), (self.label or "", f"italic {GREY}")))
        elif self.state == "speaking":
            typing.update(Text.assemble(("♪ ", f"bold {PINK}"), ("говорит…", f"italic {GREY}")))
        else:
            typing.update("")
        if self.state in ("listening", "speaking"):
            self.refresh_status()

    # ----- connection -----
    async def connect_loop(self):
        while True:
            try:
                async with websockets.connect(URL, max_size=None) as ws:
                    self.ws = ws
                    async for raw in ws:
                        try:
                            self.handle(json.loads(raw))
                        except Exception:
                            _log_error()
            except Exception:
                pass
            self.ws = None
            self.state, self.label = "offline", ""
            self.refresh_status()
            await asyncio.sleep(1.5)

    def handle(self, m):
        if "history" in m:                       # (re)connect: replay the whole conversation
            log = self.query_one("#log", VerticalScroll)
            log.remove_children()
            self.welcome = None
            if m["history"]:
                for ev in m["history"]:
                    self.add(ev, scroll=False)
                self.call_after_refresh(log.scroll_end, animate=False)
            else:
                self.show_welcome()
        if "chat" in m:
            self.add(m["chat"])
        if "level" in m and isinstance(m["level"], (int, float)):
            self.levels.append(float(m["level"]))
        if "perm" in m:
            self.perm = m["perm"]
            self.query_one("#input", Input).placeholder = (
                f"⚠  разрешить «{self.perm['text']}» ?   y / n" if self.perm else "❯  напиши сообщение…")
        if m.get("state"):
            self.state, self.label = m["state"], m.get("label") or ""
            self.refresh_status()

    # ----- input / actions -----
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        txt = event.value.strip()
        event.input.value = ""
        if not txt or self.ws is None:
            return
        if self.perm and txt.lower() in YES | NO:
            await self.ws.send(json.dumps({"cmd": "perm", "id": self.perm["id"], "allow": txt.lower() in YES}))
            return
        await self.ws.send(json.dumps({"cmd": "text", "text": txt}, ensure_ascii=False))

    def action_clear(self) -> None:
        self.query_one("#log", VerticalScroll).remove_children()
        self.welcome = None
        self.show_welcome()

    def action_page(self, direction: int) -> None:
        log = self.query_one("#log", VerticalScroll)
        (log.scroll_page_down if direction > 0 else log.scroll_page_up)(animate=False)


def _log_error():
    import traceback
    try:
        d = os.path.expanduser("~/Library/Logs/Ixzib"); os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "chat.log"), "a") as f:
            f.write(time.strftime("%F %T ") + traceback.format_exc() + "\n")
    except Exception:
        pass


def _die_with_terminal():
    """Tab closed -> tty is gone / we were re-parented to launchd. Never linger and burn CPU."""
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, lambda *_: os._exit(0))

    def watch():
        while True:
            time.sleep(1.5)
            try:
                gone = os.getppid() == 1 or not os.isatty(0)
            except OSError:
                gone = True
            if gone:
                os._exit(0)
    threading.Thread(target=watch, daemon=True).start()


if __name__ == "__main__":
    _die_with_terminal()
    Chat(ansi_color=True).run()
