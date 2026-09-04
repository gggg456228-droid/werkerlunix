from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
RUNTIME = ROOT / ".runtime"
TOKEN_FILE = RUNTIME / "token.txt"
TASKS_DIR = RUNTIME / "tasks"


class WorkerError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def safe_id(value: str, fallback: str = "task") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._-")
    return (cleaned or fallback)[:120]


def normalize_config(raw: dict) -> dict:
    cfg = dict(raw or {})
    cfg.setdefault("node_id", "")
    cfg.setdefault("bind", "127.0.0.1")
    cfg.setdefault("port", 8765)
    cfg.setdefault("allow_insecure_remote_bind", False)
    cfg.setdefault("token_auth_enabled", True)
    cfg.setdefault("cors_allow_origin", "")
    cfg.setdefault("max_body_bytes", 96 * 1024 * 1024)
    cfg.setdefault("max_file_transfer_bytes", 64 * 1024 * 1024)
    cfg.setdefault("task_workers", 4)
    cfg.setdefault("allowed_roots", ["~/omega-worker-data"])
    cfg.setdefault("create_allowed_roots", True)
    cfg.setdefault("allowed_actions", [])
    cfg.setdefault("denied_actions", [])
    cfg.setdefault("allow_process_execution", False)
    cfg.setdefault("allowed_executables", [])
    cfg.setdefault("allow_process_termination", False)
    cfg.setdefault("services", {})
    cfg.setdefault("signed_owner", {})
    sec = cfg["signed_owner"]
    sec.setdefault("enabled", False)
    sec.setdefault("owner_public_key", "")
    sec.setdefault("max_ttl_seconds", 120)
    sec.setdefault("max_clock_skew_seconds", 60)
    sec.setdefault("nonce_retention_seconds", 86400)
    sec.setdefault("allowed_operations", ["call", "submit", "get_task", "capabilities"])
    return cfg


def load_config(path: Path) -> dict:
    if not path.exists():
        example = ROOT / "config.example.json"
        if example.exists():
            shutil.copy2(example, path)
    return normalize_config(load_json(path, {}))


def ensure_token() -> str:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    return token
