#!/bin/bash
# Sign in to your Claude account (needed once). A browser window opens.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
CLAUDE="$("$ROOT/core/.venv/bin/python" -c 'import claude_agent_sdk,os;print(os.path.join(os.path.dirname(claude_agent_sdk.__file__),"_bundled","claude"))')"
"$CLAUDE" auth login
"$CLAUDE" auth status
