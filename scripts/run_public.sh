#!/usr/bin/env bash
# Run Signal Desk UI + optional public tunnel for demos.
# NOTE: Cursor cloud agents + trycloudflare quick tunnels are ephemeral.
# For 24×7 use: Docker on Railway / Render / Fly / a VPS (see Dockerfile).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8787}"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install -r requirements.txt
  pip install -e .
else
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

mkdir -p outputs
python -m odte_scanner ui --host 0.0.0.0 --port "$PORT" &
UI_PID=$!
trap 'kill $UI_PID 2>/dev/null || true' EXIT

echo "Signal Desk UI on http://127.0.0.1:${PORT}"
if command -v cloudflared >/dev/null 2>&1; then
  echo "Starting Cloudflare quick tunnel (ephemeral URL)…"
  cloudflared tunnel --url "http://127.0.0.1:${PORT}" --no-autoupdate
else
  echo "Install cloudflared for a public demo URL, or deploy the Dockerfile for 24×7."
  wait "$UI_PID"
fi
