from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zipfile import ZipFile

from bootstrap_worker import ROOT, verify_core

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024

# Files intentionally published by the credential free Linux mirror.
# Controller only helpers and Windows launchers do not have to be present there.
PUBLIC_MIRROR_FILES = [
    ".gitignore",
    "README.md",
    "bootstrap_worker.py",
    "apply_portable_update.py",
    "public_update.py",
    "install_linux.sh",
    "start_linux.sh",
    "config.example.json",
    "config.remote.example.json",
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


def find_source_root(extracted: Path) -> Path:
    if (extracted / "worker.py").is_file() and (extracted / "public_update.py").is_file():
        return extracted
    candidates: list[Path] = []
    for path in extracted.rglob("worker.py"):
        root = path.parent
        if (root / "public_update.py").is_file() and (root / "bootstrap_worker.py").is_file():
            candidates.append(root)
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            unique.append(candidate)
            seen.add(resolved)
    if len(unique) != 1:
        raise RuntimeError("could not locate a unique Omega Worker root in public update archive")
    return unique[0]


def safe_extract(zf: ZipFile, destination: Path) -> None:
    destination = destination.resolve()
    for info in zf.infolist():
        target = (destination / info.filename).resolve()
        try:
            target.relative_to(destination)
        except ValueError as exc:
            raise RuntimeError(f"unsafe path in update archive: {info.filename}") from exc
    zf.extractall(destination)


def download(url: str, destination: Path) -> int:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise RuntimeError("public update URL must use https")
    request = urllib.request.Request(url, headers={"User-Agent": "Omega-Worker-Public-Updater/2"})
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise RuntimeError("public update archive exceeds size limit")
            out.write(chunk)
    return total


def overlay_public_core(source_root: Path) -> dict:
    runtime = ROOT / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = runtime / "backups" / stamp
    changed: list[str] = []

    missing = [rel for rel in PUBLIC_MIRROR_FILES if not (source_root / rel).is_file()]
    if missing:
        raise RuntimeError("public mirror is missing core files: " + ", ".join(missing))

    for rel in PUBLIC_MIRROR_FILES:
        src = source_root / rel
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

    return {
        "changed": changed,
        "backup": str(backup_root) if changed else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Update Omega Worker core from a credential free public ZIP mirror")
    parser.add_argument("url", help="HTTPS ZIP URL for the public Linux worker mirror")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="omega-public-update-") as td:
        temp = Path(td)
        archive = temp / "worker.zip"
        size = download(args.url, archive)
        extracted = temp / "extracted"
        extracted.mkdir(parents=True, exist_ok=True)
        with ZipFile(archive, "r") as zf:
            safe_extract(zf, extracted)
        source_root = find_source_root(extracted)
        result = overlay_public_core(source_root)

    missing_after = verify_core()
    if missing_after:
        raise RuntimeError("core verification failed after update: " + ", ".join(missing_after))

    print(json.dumps({
        "ok": True,
        "source": args.url,
        "downloaded_bytes": size,
        "changed": result["changed"],
        "backup": result["backup"],
        "github_credentials_required": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
