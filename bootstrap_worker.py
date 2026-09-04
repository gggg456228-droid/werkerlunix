from __future__ import annotations

import argparse
import hashlib
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
REPO_URL = "https://github.com/gggg456228-droid/worker.git"

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

REQUIRED_FILES = [
    "worker.py",
    "worker_actions.py",
    "worker_common.py",
    "worker_security.py",
    "requirements.txt",
    "config.example.json",
]

PRESERVED_NAMES = {
    ".runtime",
    "config.json",
    "fleet.json",
    "owner_public.pem",
    "owner_private.pem",
    "owner_keys",
}


class BootstrapError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[OMEGA BOOTSTRAP] {message}", flush=True)


def run(argv: list[str], cwd: Path | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(x) for x in argv],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
    )


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def backup_path(rel: str, backup_root: Path) -> None:
    src = ROOT / rel
    if not src.exists() or src.name in PRESERVED_NAMES:
        return
    dst = backup_root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)


def overlay_core(source_root: Path) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = RUNTIME / "backups" / stamp
    changed: list[str] = []
    missing_in_source: list[str] = []

    for rel in CORE_FILES:
        src = source_root / rel
        dst = ROOT / rel
        if not src.exists():
            missing_in_source.append(rel)
            continue

        same = False
        if src.is_file() and dst.is_file():
            try:
                same = sha256(src) == sha256(dst)
            except OSError:
                same = False
        if same:
            continue

        if dst.exists():
            backup_path(rel, backup_root)

        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        changed.append(rel)

    if changed:
        log(f"core files updated: {len(changed)}")
        log(f"backup: {backup_root}")
    else:
        log("core already current")

    return {"changed": changed, "missing_in_source": missing_in_source}


def update_from_git_checkout() -> bool:
    git = shutil.which("git")
    if not git or not (ROOT / ".git").exists():
        return False

    status = run([git, "-C", str(ROOT), "status", "--porcelain"], timeout=30)
    if status.returncode != 0:
        return False

    if status.stdout.strip():
        if verify_core():
            log("git checkout has local changes and missing core files; automatic pull skipped")
            return False
        log("git checkout has local changes; preserving them and using existing core")
        return True

    pull = run([git, "-C", str(ROOT), "pull", "--ff-only"], timeout=180)
    if pull.returncode == 0:
        text = (pull.stdout or "").strip()
        log(text if text else "git checkout is current")
        return True

    detail = (pull.stderr or pull.stdout or "").strip()[-1000:]
    log(f"git pull failed: {detail}")
    return False


def update_from_authenticated_clone() -> bool:
    git = shutil.which("git")
    gh = shutil.which("gh")

    with tempfile.TemporaryDirectory(prefix="omega-worker-auth-update-") as td:
        target = Path(td) / "worker"

        if git:
            cp = run([git, "clone", "--depth", "1", REPO_URL, str(target)], timeout=300)
            if cp.returncode == 0:
                overlay_core(target)
                log("updated from explicitly authorized authenticated git clone")
                return True
            detail = (cp.stderr or cp.stdout or "").strip()[-1000:]
            log(f"authenticated git clone unavailable: {detail}")

        if gh:
            if target.exists():
                shutil.rmtree(target)
            cp = run(
                [gh, "repo", "clone", "gggg456228-droid/worker", str(target), "--", "--depth", "1"],
                timeout=300,
            )
            if cp.returncode == 0:
                overlay_core(target)
                log("updated from explicitly authorized GitHub CLI clone")
                return True
            detail = (cp.stderr or cp.stdout or "").strip()[-1000:]
            log(f"GitHub CLI clone unavailable: {detail}")

    return False


def ensure_requirements() -> None:
    requirements = ROOT / "requirements.txt"
    if not requirements.exists():
        raise BootstrapError("requirements.txt is missing")

    if importlib.util.find_spec("cryptography") is not None:
        return

    log("installing Python requirements")
    cp = run(
        [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
        timeout=600,
    )
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip()[-3000:]
        raise BootstrapError(f"pip install failed: {detail}")


def ensure_config() -> None:
    config = ROOT / "config.json"
    example = ROOT / "config.example.json"
    if not config.exists() and example.exists():
        shutil.copy2(example, config)
        log("created config.json from config.example.json")


def verify_core() -> list[str]:
    return [rel for rel in REQUIRED_FILES if not (ROOT / rel).exists()]


def repair(best_effort: bool = False, skip_update: bool = False, authenticated_update: bool = False) -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)

    if not skip_update:
        if (ROOT / ".git").exists():
            updated = update_from_git_checkout()
            if not updated and authenticated_update:
                update_from_authenticated_clone()
        else:
            log("portable folder detected; no GitHub access attempted")
            if authenticated_update:
                log("authenticated update explicitly requested")
                update_from_authenticated_clone()

    missing = verify_core()
    if missing:
        raise BootstrapError(
            "missing core files after repair: " + ", ".join(missing) + ". "
            "Restore from a controller-built portable package when GitHub authentication is unavailable."
        )

    ensure_config()
    ensure_requirements()
    log("worker core ready")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Omega Worker self-healing bootstrap")
    parser.add_argument("--repair", action="store_true", help="repair/verify worker core")
    parser.add_argument("--best-effort", action="store_true", help="use existing local core when update is unavailable")
    parser.add_argument("--skip-update", action="store_true", help="only verify local core and dependencies")
    parser.add_argument(
        "--authenticated-update",
        action="store_true",
        help="explicitly allow authenticated GitHub clone fallback on a trusted controller machine",
    )
    parser.add_argument("--check", action="store_true", help="print missing core files and exit")
    args = parser.parse_args()

    if args.check:
        missing = verify_core()
        if missing:
            print("MISSING:")
            for rel in missing:
                print(rel)
            return 2
        print("OK")
        return 0

    try:
        return repair(
            best_effort=args.best_effort,
            skip_update=args.skip_update,
            authenticated_update=args.authenticated_update,
        )
    except Exception as exc:
        if args.best_effort and not verify_core():
            log(f"update unavailable, using existing local core: {type(exc).__name__}: {exc}")
            try:
                ensure_config()
                ensure_requirements()
                return 0
            except Exception as inner:
                log(f"ERROR: {type(inner).__name__}: {inner}")
                return 1
        log(f"ERROR: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
