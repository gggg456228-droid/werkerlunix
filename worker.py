from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from worker_actions import Worker
from worker_common import (
    DEFAULT_CONFIG,
    RUNTIME,
    TOKEN_FILE,
    WorkerError,
    atomic_json,
    ensure_token,
    expand_path,
    load_config,
    load_json,
    now_iso,
)
from worker_security import OwnerVerifier


INGEST_DIR = RUNTIME / "ingest"
INGEST_LOCK = threading.Lock()


def _safe_stream_name(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise WorkerError("missing ingest stream")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    if not cleaned:
        raise WorkerError("invalid ingest stream")
    return cleaned[:180]


def _generic_ingest(payload: dict, *, fallback_stream: str = "") -> dict:
    """
    Universal append/merge storage for external scripts.

    Preferred body:
    {
      "stream": "my-source",
      "key": "id",
      "records": [{"id": "1", ...}],
      "meta": {...}
    }

    Compatibility bodies may use "messages" instead of "records".
    """
    if not isinstance(payload, dict):
        raise WorkerError("ingest payload must be an object")

    records = payload.get("records")
    if records is None:
        records = payload.get("messages")
    if not isinstance(records, list):
        raise WorkerError("ingest requires records/messages list")

    stream = _safe_stream_name(payload.get("stream") or fallback_stream)
    key_field = str(payload.get("key") or "id").strip()
    if not key_field or len(key_field) > 120:
        raise WorkerError("invalid ingest key field")

    incoming_meta = payload.get("meta") or {}
    if not isinstance(incoming_meta, dict):
        raise WorkerError("ingest meta must be an object")

    INGEST_DIR.mkdir(parents=True, exist_ok=True)
    path = INGEST_DIR / f"{stream}.json"

    with INGEST_LOCK:
        current = load_json(
            path,
            {
                "stream": stream,
                "key": key_field,
                "meta": {},
                "records": [],
                "created_at": now_iso(),
            },
        )
        if not isinstance(current, dict):
            current = {}

        existing_records = current.get("records") or []
        if not isinstance(existing_records, list):
            existing_records = []

        ordered: list[dict] = []
        positions: dict[str, int] = {}
        for record in existing_records:
            if not isinstance(record, dict):
                continue
            raw_key = record.get(key_field)
            if raw_key is None:
                continue
            record_key = str(raw_key)
            if record_key in positions:
                ordered[positions[record_key]] = record
            else:
                positions[record_key] = len(ordered)
                ordered.append(record)

        new_count = 0
        updated_count = 0
        skipped_count = 0

        for record in records:
            if not isinstance(record, dict):
                skipped_count += 1
                continue
            raw_key = record.get(key_field)
            if raw_key is None or str(raw_key).strip() == "":
                skipped_count += 1
                continue

            record_key = str(raw_key)
            if record_key in positions:
                idx = positions[record_key]
                if ordered[idx] != record:
                    ordered[idx] = record
                    updated_count += 1
            else:
                positions[record_key] = len(ordered)
                ordered.append(record)
                new_count += 1

        meta = current.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        meta.update(incoming_meta)

        out = {
            "stream": stream,
            "key": key_field,
            "meta": meta,
            "records": ordered,
            "created_at": current.get("created_at") or now_iso(),
            "updated_at": now_iso(),
            "count": len(ordered),
        }
        atomic_json(path, out)

    return {
        "ok": True,
        "stream": stream,
        "stored": str(path),
        "count": len(ordered),
        "new": new_count,
        "updated": updated_count,
        "skipped": skipped_count,
    }


def _legacy_discord_ingest(payload: dict) -> dict:
    """
    Compatibility adapter for the previously issued Tampermonkey scanner.
    The core storage remains generic.
    """
    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}

    guild_id = str(meta.get("guildId") or payload.get("guildId") or "unknown")
    channel_id = str(meta.get("channelId") or payload.get("channelId") or "unknown")
    fallback_stream = f"discord_{guild_id}_{channel_id}"

    normalized = {
        "stream": fallback_stream,
        "key": "id",
        "records": payload.get("messages") or [],
        "meta": meta,
    }
    return _generic_ingest(normalized, fallback_stream=fallback_stream)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "OmegaWorker/2.1"

    @property
    def app(self) -> Worker:
        return self.server.app  # type: ignore[attr-defined]

    @property
    def verifier(self) -> OwnerVerifier:
        return self.server.verifier  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] HTTP {self.address_string()} {fmt % args}", flush=True)

    def _cors(self) -> None:
        origin = str(self.app.config.get("cors_allow_origin") or "").strip()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Authorization, X-Worker-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        if not bool(self.app.config.get("token_auth_enabled", True)):
            return False
        auth = self.headers.get("Authorization", "")
        header_token = self.headers.get("X-Worker-Token", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else header_token.strip()
        return bool(token) and secrets.compare_digest(token, self.app.token)

    def _require_auth(self) -> bool:
        if self._auth():
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        max_body = int(self.app.config.get("max_body_bytes", 96 * 1024 * 1024))
        if length <= 0:
            return {}
        if length > max_body:
            raise WorkerError(f"request body too large: {length} > {max_body}")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise WorkerError("request body must be a JSON object")
        return value

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/health":
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "node_id": str(self.app.config.get("node_id") or ""),
                    "time": now_iso(),
                },
            )
            return
        if not self._require_auth():
            return
        try:
            if path == "/api/v1/capabilities":
                result = self.app.capabilities()
                result["ingest"] = {
                    "endpoint": "/api/v1/ingest",
                    "storage": str(INGEST_DIR),
                }
                self._json(HTTPStatus.OK, result)
            elif path.startswith("/api/v1/tasks/"):
                self._json(HTTPStatus.OK, self.app.get_task(path.rsplit("/", 1)[-1]))
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except Exception as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            payload = self._read_body()
            if path == "/api/v1/owner":
                self._handle_owner(payload)
                return
            if not self._require_auth():
                return

            if path == "/api/v1/call":
                self._json(HTTPStatus.OK, self.app.call(payload, source="local_http"))
            elif path == "/api/v1/tasks":
                self._json(HTTPStatus.ACCEPTED, self.app.submit(payload, source="local_http"))
            elif path == "/api/v1/ingest":
                self._json(HTTPStatus.OK, _generic_ingest(payload))
            elif path == "/api/v1/discord/import":
                self._json(HTTPStatus.OK, _legacy_discord_ingest(payload))
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except json.JSONDecodeError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"invalid JSON: {exc}"})
        except Exception as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"{type(exc).__name__}: {exc}"})

    def _handle_owner(self, envelope: dict) -> None:
        try:
            verified = self.verifier.verify(envelope)
            operation = str(verified["operation"])
            payload = verified.get("payload") or {}
            if operation == "call":
                self._json(HTTPStatus.OK, self.app.call(payload, source="signed_owner"))
            elif operation == "submit":
                self._json(HTTPStatus.ACCEPTED, self.app.submit(payload, source="signed_owner"))
            elif operation == "get_task":
                self._json(HTTPStatus.OK, self.app.get_task(str(payload.get("id") or "")))
            elif operation == "capabilities":
                self._json(HTTPStatus.OK, self.app.capabilities())
            else:
                raise WorkerError(f"unsupported owner operation: {operation}")
        except Exception as exc:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": f"{type(exc).__name__}: {exc}"})


def is_loopback(bind: str) -> bool:
    return bind in {"127.0.0.1", "::1", "localhost"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Omega universal worker")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()

    cfg = load_config(expand_path(args.config))
    bind, port = str(cfg.get("bind", "127.0.0.1")), int(cfg.get("port", 8765))
    signed = bool((cfg.get("signed_owner") or {}).get("enabled", False))
    if not is_loopback(bind) and not signed and not bool(cfg.get("allow_insecure_remote_bind", False)):
        raise SystemExit(
            "Refusing non-loopback bind without signed_owner. "
            "Enable signed_owner or explicitly set allow_insecure_remote_bind=true."
        )

    token = ensure_token()
    app = Worker(cfg, token)
    verifier = OwnerVerifier(cfg)
    server = ThreadingHTTPServer((bind, port), ApiHandler)
    server.app = app  # type: ignore[attr-defined]
    server.verifier = verifier  # type: ignore[attr-defined]

    print("===============================================================", flush=True)
    print(" OMEGA UNIVERSAL WORKER 2.1", flush=True)
    print(f" Node: {cfg.get('node_id') or '(unnamed)'}", flush=True)
    print(f" API:  http://{bind}:{port}", flush=True)
    print(f" Signed owner: {'ON' if signed else 'OFF'}", flush=True)
    if cfg.get("token_auth_enabled", True):
        print(f" Local token file: {TOKEN_FILE}", flush=True)
    print(f" Ingest storage: {INGEST_DIR}", flush=True)
    print("===============================================================", flush=True)

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_event.set()
        server.shutdown()
        app.pool.shutdown(wait=False, cancel_futures=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
