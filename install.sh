#!/bin/bash
# Ixzib installer for macOS (Apple Silicon).
#
#   curl -fsSL https://raw.githubusercontent.com/fu7ur3gh057/ixzib/main/install.sh | bash
#   bash install.sh [--name "Имя"] [--no-login] [--no-models] [--no-launch]
#
# Needs: a Mac with an Apple chip (M1 or newer), macOS 13+, internet, a Claude account (Pro/Max).
# Does NOT need: Homebrew, Node, Xcode, or any programming knowledge.
set -euo pipefail

REPO_SLUG="fu7ur3gh057/ixzib"          # <- filled in when the project is published
BRANCH="main"
ELECTRON_VERSION="45.3.2"

NAME=""; NO_LOGIN=0; NO_MODELS=0; NO_LAUNCH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="${2:-}"; shift 2 ;;
    --no-login) NO_LOGIN=1; shift ;;
    --no-models) NO_MODELS=1; shift ;;
    --no-launch) NO_LAUNCH=1; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "Неизвестный параметр: $1"; exit 2 ;;
  esac
done

APP_DIR="${IXZIB_APP_DIR:-$HOME/Applications}"
HOME_DIR="${IXZIB_HOME:-$HOME/.ixzib}"
LOG_DIR="$HOME/Library/Logs/Ixzib"; mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/install.log") 2>&1

say()  { printf "\n\033[1;36m==>\033[0m \033[1m%s\033[0m\n" "$*"; }
ok()   { printf "   \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "   \033[33m!\033[0m %s\n" "$*"; }
die()  { printf "\n\033[1;31mОшибка:\033[0m %s\n" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- where is the project?
SRC="${BASH_SOURCE[0]:-}"
ROOT=""
if [ -n "$SRC" ] && [ -f "$SRC" ]; then ROOT="$(cd "$(dirname "$SRC")" && pwd)"; fi
if [ -z "$ROOT" ] || [ ! -d "$ROOT/core" ]; then
  say "Скачиваю Ixzib"
  DEST="$HOME/Ixzib"; mkdir -p "$DEST"
  curl -fsSL "https://github.com/$REPO_SLUG/archive/refs/heads/$BRANCH.tar.gz" | tar -xz --strip-components=1 -C "$DEST" \
    || die "Не удалось скачать проект с GitHub ($REPO_SLUG). Проверьте интернет и ссылку."
  ok "Проект лежит в $DEST"
  exec bash "$DEST/install.sh" "$@"
fi
cd "$ROOT"

# ---------------------------------------------------------------- checks
say "Проверяю Mac"
[ "$(uname)" = "Darwin" ] || die "Ixzib работает только на macOS."
[ "$(uname -m)" = "arm64" ] || die "Нужен Mac с чипом Apple (M1, M2, M3, M4…). Ваш Mac на Intel — к сожалению, не подойдёт."
MACOS_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
[ "$MACOS_MAJOR" -ge 13 ] || die "Нужна macOS 13 или новее (у вас $(sw_vers -productVersion)). Обновите систему и запустите установку снова."
FREE_GB="$(df -g "$HOME" | awk 'NR==2{print $4}')"
[ "${FREE_GB:-0}" -ge 6 ] || warn "Свободно всего ${FREE_GB} ГБ, нужно около 6 ГБ."
curl -fsI https://github.com >/dev/null || die "Нет доступа к интернету."
ok "macOS $(sw_vers -productVersion), чип Apple, интернет есть"

# ---------------------------------------------------------------- uv (python manager, no admin rights needed)
say "Готовлю среду Python"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || die "Не удалось поставить менеджер Python (uv)."
  export PATH="$HOME/.local/bin:$PATH"
fi
ok "uv: $(uv --version)"
( cd "$ROOT/core" && uv sync ) || die "Не удалось установить библиотеки Python."
PY="$ROOT/core/.venv/bin/python"
ok "Библиотеки установлены"

# ---------------------------------------------------------------- Electron (the widget window)
say "Скачиваю оболочку виджета (Electron)"
EL_DIR="$ROOT/widget/electron"
if [ ! -x "$EL_DIR/Electron.app/Contents/MacOS/Electron" ]; then
  TMPZIP="$(mktemp -t electron).zip"
  curl -fL --progress-bar "https://github.com/electron/electron/releases/download/v$ELECTRON_VERSION/electron-v$ELECTRON_VERSION-darwin-arm64.zip" -o "$TMPZIP" \
    || die "Не удалось скачать Electron."
  mkdir -p "$EL_DIR" && ditto -x -k "$TMPZIP" "$EL_DIR" && rm -f "$TMPZIP"
  xattr -dr com.apple.quarantine "$EL_DIR" 2>/dev/null || true
fi
[ -x "$EL_DIR/Electron.app/Contents/MacOS/Electron" ] || die "Electron не распаковался."
ok "Готово"

# ---------------------------------------------------------------- settings
say "Настройки"
mkdir -p "$HOME_DIR"
CONFIG="$HOME_DIR/config.json"
if [ ! -f "$CONFIG" ]; then
  if [ -z "$NAME" ]; then
    DEFAULT_NAME="$(id -F 2>/dev/null | awk '{print $1}')"
    if [ -r /dev/tty ]; then
      printf "   Как вас называть? (по-русски, например «Иван») [%s]: " "$DEFAULT_NAME"
      read -r NAME < /dev/tty || true
    fi
    NAME="${NAME:-$DEFAULT_NAME}"
  fi
  "$PY" - "$CONFIG" "$NAME" <<'PYEOF'
import json, sys
json.dump({"name": sys.argv[2], "workdir": "~", "safe_root": "~"}, open(sys.argv[1], "w"), ensure_ascii=False, indent=2)
PYEOF
  ok "Имя: $NAME  (файл $CONFIG — можно поменять в любой момент)"
else
  ok "Настройки уже есть: $CONFIG"
fi

# ---------------------------------------------------------------- Claude account
CLAUDE="$("$PY" -c 'import claude_agent_sdk,os;print(os.path.join(os.path.dirname(claude_agent_sdk.__file__),"_bundled","claude"))')"
if [ "$NO_LOGIN" -eq 0 ]; then
  say "Вход в аккаунт Claude"
  if "$CLAUDE" auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
    ok "Вы уже вошли в Claude"
  else
    echo "   Сейчас откроется браузер. Войдите в свой аккаунт Claude (тот же, что в приложении Claude) и вернитесь сюда."
    "$CLAUDE" auth login || true
    if "$CLAUDE" auth status 2>/dev/null | grep -q '"loggedIn": *true'; then ok "Вход выполнен"
    else warn "Вход не завершён. Позже запустите:  bash login.sh"; fi
  fi
fi

# ---------------------------------------------------------------- models
if [ "$NO_MODELS" -eq 0 ]; then
  say "Скачиваю модели речи (около 2 ГБ, один раз — это самый долгий шаг)"
  ( cd "$ROOT/core" && IXZIB_CONFIG="$CONFIG" "$PY" ixzib_core.py --setup ) || die "Не удалось скачать модели речи."
  ok "Модели готовы"
fi

# ---------------------------------------------------------------- the app
say "Создаю приложение Ixzib"
APP_PATH="$(bash "$ROOT/scripts/make_app.sh" "$ROOT" "$APP_DIR")"
ok "$APP_PATH"

say "Готово!"
cat <<EOM

   Как запускать:  нажмите  ⌘ Пробел,  введите  Ixzib  и Enter.
   Первый запуск займёт около 30 секунд (загрузка моделей в память).
   macOS спросит доступ к микрофону для Ixzib — нажмите «Разрешить».

   Потом скажите:  «Эй, Иксзиб» — и просто говорите.
   Левый клик по шару — слушать, правый клик — меню (там «Открыть» — окно с чатом).

EOM
if [ "$NO_LAUNCH" -eq 0 ]; then open "$APP_PATH" || true; fi
