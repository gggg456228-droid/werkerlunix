#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

DATA_DIR="${OMEGA_DATA_DIR:-$HOME/omega-worker-data}"
RUNTIME="$ROOT/.runtime"
PYTHON="$ROOT/.venv/bin/python"
PENDING_DIR="$DATA_DIR/updates/pending"
APPLIED_DIR="$DATA_DIR/updates/applied"
PORTABLE_UPDATE="$PENDING_DIR/omega-worker-update.zip"
PUBLIC_UPDATE_FILE="$ROOT/.omega_public_update_url"

log() {
  printf '[OMEGA START] %s\n' "$*"
}

mkdir -p "$RUNTIME" "$PENDING_DIR" "$APPLIED_DIR"

if [ ! -x "$PYTHON" ]; then
  log "Python environment is missing. Run ./install_linux.sh first."
  exit 1
fi

# A sanitized public distribution mirror may ship only this URL marker.
# The worker downloads a ZIP over HTTPS and overlays core files in place.
# No .git directory, GitHub login, PAT, or owner private key is required.
if [ -f "$PUBLIC_UPDATE_FILE" ]; then
  PUBLIC_UPDATE_URL="$(tr -d '\r\n' < "$PUBLIC_UPDATE_FILE")"
  if [ -n "$PUBLIC_UPDATE_URL" ]; then
    log "Checking public portable mirror for a core update"
    if ! "$PYTHON" "$ROOT/public_update.py" "$PUBLIC_UPDATE_URL"; then
      log "Public mirror update unavailable. Continuing with existing core."
    fi
  fi
fi

# Controller-managed portable nodes update from a delivered package instead.
if [ -f "$PORTABLE_UPDATE" ]; then
  log "Applying pending controller-delivered portable core update"
  "$PYTHON" "$ROOT/apply_portable_update.py" "$PORTABLE_UPDATE"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  mv "$PORTABLE_UPDATE" "$APPLIED_DIR/omega-worker-update-$stamp.zip"
fi

log "Verifying local core and dependencies"
"$PYTHON" "$ROOT/bootstrap_worker.py" --repair --best-effort --skip-update

log "Starting Omega Worker"
exec "$PYTHON" "$ROOT/worker.py" --config "$ROOT/config.json"
