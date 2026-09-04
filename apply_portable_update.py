from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"

CORE_FILES = [
    ".gitignore",
    "README.md",
    "START_WORKER.bat",
    "START_REMOTE_NODE.bat",
    "INSTALL_OR_REPAIR_WORKER.bat",
    "bootstrap_worker.py",
    "build_portable_package.py",
    "apply_portable_update.py",
    "public_update.py",
    "install_linux.sh",
    "start_linux.sh",
    "config.example.json",
    "config.remote.example.json",
    "fleet.example.json",
    "fleet_cli.py",
    "owner_cli.py",
    "owner_keygen.py",
    "requirements.txt",
    "worker.py",
    "worker_actions.py",
    "worker_common.py",
    "worker_security.py",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def locate_package_root(extracted: Path) -> Path:
    direct = extracted / "portable_manifest.json"
    if direct.exists():
        return extracted
    candidates = [p.parent for p in extracted.rglob("portable_manifest.json")]
    if len(candidates) != 1:
        raise RuntimeError("portable_manifest.json not found uniquely in update package")
    return candidates[0]


def verify_manifest(package_root: Path) -> dict:
    manifest_path = package_root / "portable_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "omega-worker-portable-v1":
        raise RuntimeError("unsupported portable package format")
    files = manifest.get("files") or {}
    if not isinstance(files, dict):
        raise RuntimeError("invalid portable manifest")
    for rel, expected in files.items():
        path = package_root / str(rel)
        if not path.is_file():
            raise RuntimeError(f"package file missing: {rel}")
        actual = sha256(path)
        if actual != str(expected):
            raise RuntimeError(f"hash mismatch: {rel}")
    return manifest


def main() -> int:
    p = argparse.ArgumentParser(description="Apply a credential-free Omega Worker portable core update")
    p.add_argument("package", help="Path to omega-worker portable ZIP")
    a = p.parse_args()

    package = Path(a.package).expanduser().resolve()
    if not package.is_file():
        raise SystemExit(f"update package not found: {package}")

    RUNTIME.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = RUNTIME / "backups" / stamp
    changed: list[str] = []

    with tempfile.TemporaryDirectory(prefix="omega-update-") as td:
        extracted = Path(td)
        with ZipFile(package, "r") as zf:
            zf.extractall(extracted)
        package_root = locate_package_root(extracted)
        verify_manifest(package_root)

        for rel in CORE_FILES:
            src = package_root / rel
            if not src.is_file():
                raise RuntimeError(f"required core file missing from package: {rel}")
            dst = ROOT / rel
            if dst.is_file() and sha256(dst) == sha256(src):
                continue
            if dst.exists():
                backup = backup_root / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, backup)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            changed.append(rel)

    print(json.dumps({
        "ok": True,
        "changed": changed,
        "backup": str(backup_root) if changed else None,
        "preserved": [
            ".runtime",
            "config.json",
            "fleet.json",
            "owner_public.pem",
            "owner_private.pem",
            "owner_keys",
            "external data directories",
        ],
        "restart_required": bool(changed),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
