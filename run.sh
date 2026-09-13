#!/usr/bin/env bash
# Start the Coomi-on-Kimi agent.
#
#   ./run.sh                 # serve the web UI on 127.0.0.1:8765 (default)
#   ./run.sh chat            # rich terminal REPL against the same core
#   ./run.sh ask "…"         # one-shot prompt, prints the answer
#   ./run.sh doctor          # print runtime + contract facts
#   ./run.sh install         # write config/agent profile into ~/.kimi-code
#   PERMISSION=manual ./run.sh   # ask before every tool call
#
# The Kimi Code binary is looked up on PATH and in ~/.kimi-code/bin.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV="${VENV:-$HERE/.venv}"
if [ ! -x "$VENV/bin/python" ]; then
  echo "creating $VENV" >&2
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV"
    uv pip install --python "$VENV/bin/python" -r requirements.txt
  else
    python3 -m venv "$VENV"
    "$VENV/bin/python" -m pip install -U pip
    "$VENV/bin/python" -m pip install -r requirements.txt
  fi
fi

export PATH="$HOME/.kimi-code/bin:$PATH"
if ! command -v kimi >/dev/null 2>&1; then
  cat >&2 <<'MSG'
kimi: not found on PATH.

Install Kimi Code (native linux-arm64/x86_64 binary) into ~/.kimi-code/bin/kimi
and configure a provider in ~/.kimi-code/config.toml, e.g.

  default_model = "myprefix/model-name"
  default_permission_mode = "manual"
  telemetry = false

  [providers.myprefix]
  type = "openai"
  base_url = "https://example.com/v1"
  api_key  = "sk-..."

  [models."myprefix/model-name"]
  provider = "myprefix"
  model = "model-name"
  max_context_size = 204800          # the real upstream limit, not the marketing one
  capabilities = ["thinking", "image_in", "tool_use"]
MSG
  exit 1
fi

export COOMI_KIMI_PERMISSION="${PERMISSION:-auto-safe}"
export COOMI_KIMI_PORT="${PORT:-8765}"
export COOMI_KIMI_HOST="${HOST:-127.0.0.1}"

CMD="${1:-serve}"
shift || true
exec "$VENV/bin/python" -m kimi_agent.cli "$CMD" "$@"
