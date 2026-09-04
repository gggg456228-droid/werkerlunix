from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path

from worker_common import ROOT, WorkerError, atomic_json, load_json

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except Exception as exc:
    InvalidSignature = Exception
    Ed25519PublicKey = None
    _CRYPTO_IMPORT_ERROR = exc
else:
    _CRYPTO_IMPORT_ERROR = None

NONCE_FILE = ROOT / ".runtime" / "owner_nonces.json"


def canonical_bytes(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class OwnerVerifier:
    def __init__(self, config: dict):
        self.config = config
        self.node_id = str(config.get("node_id") or "").strip()
        sec = config.get("signed_owner") or {}
        self.enabled = bool(sec.get("enabled", False))
        self.max_ttl = max(5, min(3600, int(sec.get("max_ttl_seconds", 120))))
        self.max_clock_skew = max(0, min(600, int(sec.get("max_clock_skew_seconds", 60))))
        self.nonce_retention = max(self.max_ttl * 2, int(sec.get("nonce_retention_seconds", 86400)))
        self.allowed_operations = set(sec.get("allowed_operations") or ["call", "submit", "get_task", "capabilities"])
        self._lock = threading.Lock()
        self._public_key = None

        if self.enabled:
            if not self.node_id:
                raise WorkerError("signed_owner.enabled requires node_id")
            if Ed25519PublicKey is None:
                raise WorkerError(f"cryptography is required: {_CRYPTO_IMPORT_ERROR}")
            key_path = str(sec.get("owner_public_key") or "").strip()
            if not key_path:
                raise WorkerError("signed_owner.owner_public_key is required")
            raw_path = Path(os.path.expandvars(os.path.expanduser(key_path)))
            p = raw_path.resolve() if raw_path.is_absolute() else (ROOT / raw_path).resolve()
            if not p.exists():
                raise WorkerError(f"owner public key not found: {p}")
            key = serialization.load_pem_public_key(p.read_bytes())
            if not isinstance(key, Ed25519PublicKey):
                raise WorkerError("owner public key must be Ed25519")
            self._public_key = key

    def verify(self, envelope: dict) -> dict:
        if not self.enabled or self._public_key is None:
            raise WorkerError("signed owner access is disabled")
        if not isinstance(envelope, dict):
            raise WorkerError("signed request must be an object")

        signature_text = str(envelope.get("signature") or "")
        unsigned = dict(envelope)
        unsigned.pop("signature", None)
        if not signature_text:
            raise WorkerError("missing signature")
        if int(unsigned.get("version", 0)) != 1:
            raise WorkerError("unsupported signed request version")
        if str(unsigned.get("node_id") or "") != self.node_id:
            raise WorkerError("signed request is for another node")

        operation = str(unsigned.get("operation") or "")
        if operation not in self.allowed_operations:
            raise WorkerError(f"operation is not allowed on this node: {operation}")

        now = int(time.time())
        issued = int(unsigned.get("issued_at", 0) or 0)
        expires = int(unsigned.get("expires_at", 0) or 0)
        if not issued or not expires:
            raise WorkerError("issued_at/expires_at are required")
        if issued > now + self.max_clock_skew:
            raise WorkerError("signed request is from the future")
        if expires < now:
            raise WorkerError("signed request expired")
        if expires - issued > self.max_ttl:
            raise WorkerError("signed request TTL exceeds node policy")
        if now - issued > self.max_ttl + self.max_clock_skew:
            raise WorkerError("signed request is too old")

        nonce = str(unsigned.get("nonce") or "").strip()
        if len(nonce) < 16 or len(nonce) > 200:
            raise WorkerError("invalid nonce")

        try:
            sig = base64.b64decode(signature_text, validate=True)
            self._public_key.verify(sig, canonical_bytes(unsigned))
        except (ValueError, InvalidSignature) as exc:
            raise WorkerError("invalid owner signature") from exc

        self._consume_nonce(nonce, now)
        payload = unsigned.get("payload") or {}
        if not isinstance(payload, dict):
            raise WorkerError("payload must be an object")
        return unsigned

    def _consume_nonce(self, nonce: str, now: int) -> None:
        with self._lock:
            data = load_json(NONCE_FILE, {})
            if not isinstance(data, dict):
                data = {}
            cutoff = now - self.nonce_retention
            clean = {}
            for key, value in data.items():
                try:
                    ivalue = int(value)
                    if ivalue >= cutoff:
                        clean[key] = ivalue
                except Exception:
                    pass
            if nonce in clean:
                raise WorkerError("replayed signed request")
            clean[nonce] = now
            atomic_json(NONCE_FILE, clean)
