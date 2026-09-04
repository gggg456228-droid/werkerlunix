#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

DATA_DIR="${OMEGA_DATA_DIR:-$HOME/omega-worker-data}"
NODE_ID="${OMEGA_NODE_ID:-}"
BIND="${OMEGA_BIND:-127.0.0.1}"
PORT="${OMEGA_PORT:-8765}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

log() {
  printf '[OMEGA INSTALL] %s\n' "$*"
}

fail() {
  printf '[OMEGA INSTALL] ERROR: %s\n' "$*" >&2
  exit 1
}

command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "Python 3 not found"

if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1 && [ "$(id -u)" -eq 0 ]; then
    log "Installing python3-venv"
    apt-get update -y
    apt-get install -y python3-venv
  else
    fail "Python venv support is missing. Install python3-venv and rerun this script."
  fi
fi

[ -f owner_public.pem ] || fail "owner_public.pem is missing. Copy ONLY the owner public key into this folder."
[ -f requirements.txt ] || fail "requirements.txt is missing"
[ -f worker.py ] || fail "worker.py is missing"
[ -f start_linux.sh ] || fail "start_linux.sh is missing"
[ -f config.remote.example.json ] || fail "config.remote.example.json is missing"

mkdir -p .runtime
mkdir -p "$DATA_DIR/models" "$DATA_DIR/jobs" "$DATA_DIR/outputs" "$DATA_DIR/cache" "$DATA_DIR/services" "$DATA_DIR/updates/pending" "$DATA_DIR/updates/applied"
mkdir -p "$DATA_DIR/services/omega-core-update"

if [ ! -d .venv ]; then
  log "Creating Python virtual environment"
  "$PYTHON_BIN" -m venv .venv
fi

VENV_PY="$ROOT/.venv/bin/python"
VENV_PIP="$ROOT/.venv/bin/pip"

log "Installing worker requirements"
"$VENV_PIP" install --upgrade pip >/dev/null
"$VENV_PIP" install -r requirements.txt

chmod +x "$ROOT/install_linux.sh" "$ROOT/start_linux.sh"

if [ -z "$NODE_ID" ]; then
  NODE_ID="$($VENV_PY - <<'PY'
import secrets
import socket
host = socket.gethostname().strip().replace('_', '-').replace(' ', '-') or 'linux'
print(f"omega-{host}-{secrets.token_hex(4)}")
PY
)"
fi

export OMEGA_INSTALL_ROOT="$ROOT"
export OMEGA_INSTALL_DATA="$DATA_DIR"
export OMEGA_INSTALL_NODE="$NODE_ID"
export OMEGA_INSTALL_BIND="$BIND"
export OMEGA_INSTALL_PORT="$PORT"

"$VENV_PY" - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["OMEGA_INSTALL_ROOT"]).resolve()
data = Path(os.environ["OMEGA_INSTALL_DATA"]).expanduser().resolve()
node_id = os.environ["OMEGA_INSTALL_NODE"]
bind = os.environ["OMEGA_INSTALL_BIND"]
port = int(os.environ["OMEGA_INSTALL_PORT"])

example = json.loads((root / "config.remote.example.json").read_text(encoding="utf-8"))
config_path = root / "config.json"

if config_path.exists():
    config = json.loads(config_path.read_text(encoding="utf-8"))
else:
    config = example

config["node_id"] = node_id
config["bind"] = bind
config["port"] = port
config["allowed_roots"] = [str(data)]
config["create_allowed_roots"] = True
config["token_auth_enabled"] = False
config["allow_insecure_remote_bind"] = False
config["allow_process_execution"] = False
config["allow_process_termination"] = False
config["signed_owner"] = {
    "enabled": True,
    "owner_public_key": "owner_public.pem",
    "max_ttl_seconds": 120,
    "max_clock_skew_seconds": 60,
    "nonce_retention_seconds": 86400,
    "allowed_operations": ["call", "submit", "get_task", "capabilities"],
}

services = config.get("services") or {}
services["omega_core_update"] = {
    "description": "Apply a controller-built portable Omega Worker core update",
    "argv": [str(root / ".venv" / "bin" / "python"), str(root / "apply_portable_update.py")],
    "cwd": str(data / "services" / "omega-core-update"),
    "allow_args": True,
    "timeout_seconds": 600,
}
config["services"] = services

config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

log "Verifying local portable core without contacting private GitHub"
"$VENV_PY" bootstrap_worker.py --repair --best-effort --skip-update

if [ -f .runtime/worker.pid ]; then
  OLD_PID="$(cat .runtime/worker.pid 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    log "Stopping previous worker process $OLD_PID"
    kill "$OLD_PID" || true
    sleep 1
  fi
fi

log "Starting Omega Worker through start_linux.sh"
nohup "$ROOT/start_linux.sh" > .runtime/worker.log 2>&1 &
PID=$!
printf '%s\n' "$PID" > .runtime/worker.pid

sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
  cat .runtime/worker.log >&2 || true
  fail "Worker exited during startup"
fi

HEALTH_HOST="$BIND"
if [ "$HEALTH_HOST" = "0.0.0.0" ] || [ "$HEALTH_HOST" = "::" ]; then
  HEALTH_HOST="127.0.0.1"
fi

HEALTH="$($VENV_PY - <<PY
import urllib.request
url = "http://${HEALTH_HOST}:${PORT}/health"
with urllib.request.urlopen(url, timeout=5) as r:
    print(r.read().decode("utf-8"))
PY
)" || {
  cat .runtime/worker.log >&2 || true
  fail "Worker health check failed"
}

printf '\n'
printf '===============================================================\n'
printf ' OMEGA UNIVERSAL WORKER READY\n'
printf ' NODE_ID: %s\n' "$NODE_ID"
printf ' BIND: %s\n' "$BIND"
printf ' PORT: %s\n' "$PORT"
printf ' PID: %s\n' "$PID"
printf ' RUNTIME: %s/.runtime\n' "$ROOT"
printf ' DATA: %s\n' "$DATA_DIR"
printf ' SECURITY: Ed25519 signed owner commands\n'
printf ' OWNER KEY ON NODE: public only\n'
printf ' PRIVATE GITHUB ACCESS ON NODE: not required\n'
printf ' AUTO UPDATE: HTTPS public ZIP mirror when .omega_public_update_url exists, otherwise controller package\n'
printf ' HEALTH: %s\n' "$HEALTH"
printf ' LOG: %s/.runtime/worker.log\n' "$ROOT"
printf '===============================================================\n'
printf 'For SSH tunnel access, keep BIND=127.0.0.1 and forward remote port %s to your controller.\n' "$PORT"
