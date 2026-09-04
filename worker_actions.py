from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from worker_common import TASKS_DIR, WorkerError, atomic_json, expand_path, load_json, now_iso, safe_id


class Worker:
    def __init__(self, config: dict, token: str):
        self.config = config
        self.token = token
        self.started_at = now_iso()
        self.stop_event = __import__("threading").Event()
        self.pool = ThreadPoolExecutor(
            max_workers=max(1, int(config.get("task_workers", 4))),
            thread_name_prefix="omega-worker",
        )
        self.allowed_roots = [expand_path(p) for p in config.get("allowed_roots", [])]
        if config.get("create_allowed_roots", True):
            for root in self.allowed_roots:
                root.mkdir(parents=True, exist_ok=True)
        TASKS_DIR.mkdir(parents=True, exist_ok=True)

    def capabilities(self) -> dict:
        actions = sorted(self._actions().keys())
        allowed = set(self.config.get("allowed_actions") or [])
        denied = set(self.config.get("denied_actions") or [])
        if allowed:
            actions = [a for a in actions if a in allowed]
        actions = [a for a in actions if a not in denied]
        return {
            "node_id": str(self.config.get("node_id") or ""),
            "actions": actions,
            "allowed_roots": [str(p) for p in self.allowed_roots],
            "services": sorted((self.config.get("services") or {}).keys()),
            "process_execution_enabled": bool(self.config.get("allow_process_execution")),
            "process_termination_enabled": bool(self.config.get("allow_process_termination")),
            "max_file_transfer_bytes": int(self.config.get("max_file_transfer_bytes", 64 * 1024 * 1024)),
        }

    def _inside(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _allowed_path(self, value: str, must_exist: bool = False) -> Path:
        p = expand_path(value)
        if not self.allowed_roots:
            raise WorkerError("allowed_roots is empty")
        if not any(self._inside(p, root) for root in self.allowed_roots):
            raise WorkerError(f"path is outside allowed_roots: {p}")
        if must_exist and not p.exists():
            raise WorkerError(f"path does not exist: {p}")
        return p

    def _check_action_policy(self, action: str) -> None:
        allowed = set(self.config.get("allowed_actions") or [])
        denied = set(self.config.get("denied_actions") or [])
        if action in denied:
            raise WorkerError(f"action is denied: {action}")
        if allowed and action not in allowed:
            raise WorkerError(f"action is not allowed: {action}")

    def _actions(self) -> dict:
        return {
            "status": self.action_status,
            "system_info": self.action_system_info,
            "list_dir": self.action_list_dir,
            "stat_path": self.action_stat_path,
            "find_files": self.action_find_files,
            "read_text": self.action_read_text,
            "write_text": self.action_write_text,
            "append_text": self.action_append_text,
            "mkdir": self.action_mkdir,
            "copy": self.action_copy,
            "move": self.action_move,
            "delete_path": self.action_delete_path,
            "get_file": self.action_get_file,
            "put_file": self.action_put_file,
            "list_processes": self.action_list_processes,
            "terminate_process": self.action_terminate_process,
            "run_process": self.action_run_process,
            "list_services": self.action_list_services,
            "run_service": self.action_run_service,
        }

    def execute(self, action: str, params: dict):
        action = str(action or "").strip()
        self._check_action_policy(action)
        fn = self._actions().get(action)
        if not fn:
            raise WorkerError(f"unknown action: {action}")
        if not isinstance(params, dict):
            raise WorkerError("params must be an object")
        return fn(params)

    def submit(self, payload: dict, source: str = "http") -> dict:
        task_id = safe_id(str(payload.get("id") or uuid.uuid4().hex))
        action = str(payload.get("action") or "").strip()
        params = payload.get("params") or {}
        if not action:
            raise WorkerError("missing action")
        task = {
            "id": task_id,
            "action": action,
            "params": params,
            "source": source,
            "status": "queued",
            "created_at": now_iso(),
        }
        atomic_json(TASKS_DIR / f"{task_id}.json", task)
        self.pool.submit(self._execute_task, task)
        return task

    def call(self, payload: dict, source: str = "http") -> dict:
        task_id = safe_id(str(payload.get("id") or uuid.uuid4().hex))
        action = str(payload.get("action") or "").strip()
        params = payload.get("params") or {}
        if not action:
            raise WorkerError("missing action")
        task = {
            "id": task_id,
            "action": action,
            "params": params,
            "source": source,
            "status": "running",
            "created_at": now_iso(),
            "started_at": now_iso(),
        }
        try:
            task["result"] = self.execute(action, params)
            task["status"] = "complete"
        except Exception as exc:
            task["status"] = "error"
            task["error"] = f"{type(exc).__name__}: {exc}"
            task["traceback"] = traceback.format_exc()[-12000:]
        task["completed_at"] = now_iso()
        atomic_json(TASKS_DIR / f"{task_id}.json", task)
        return task

    def _execute_task(self, task: dict) -> None:
        task = dict(task)
        task.update(status="running", started_at=now_iso())
        atomic_json(TASKS_DIR / f"{task['id']}.json", task)
        try:
            task["result"] = self.execute(task["action"], task.get("params") or {})
            task["status"] = "complete"
        except Exception as exc:
            task["status"] = "error"
            task["error"] = f"{type(exc).__name__}: {exc}"
            task["traceback"] = traceback.format_exc()[-12000:]
        task["completed_at"] = now_iso()
        atomic_json(TASKS_DIR / f"{task['id']}.json", task)

    def get_task(self, task_id: str) -> dict:
        path = TASKS_DIR / f"{safe_id(task_id)}.json"
        if not path.exists():
            raise WorkerError("task not found")
        return load_json(path, {})

    def action_status(self, params: dict) -> dict:
        return {
            "ok": True,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "time": now_iso(),
            "capabilities": self.capabilities(),
        }

    def action_system_info(self, params: dict) -> dict:
        return {
            "platform": sys.platform,
            "python": sys.version,
            "cwd": str(Path.cwd()),
            "home": str(Path.home()),
            "pid": os.getpid(),
        }

    def action_list_dir(self, params: dict) -> dict:
        path = self._allowed_path(str(params["path"]), True)
        if not path.is_dir():
            raise WorkerError("path is not a directory")
        limit = min(10000, max(1, int(params.get("limit", 1000))))
        items = []
        for p in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:limit]:
            try:
                st = p.stat()
                items.append({
                    "name": p.name,
                    "path": str(p),
                    "type": "dir" if p.is_dir() else "file",
                    "size": st.st_size,
                    "modified": st.st_mtime,
                })
            except OSError:
                pass
        return {"path": str(path), "items": items}

    def action_stat_path(self, params: dict) -> dict:
        path = self._allowed_path(str(params["path"]), True)
        st = path.stat()
        return {
            "path": str(path),
            "type": "dir" if path.is_dir() else "file",
            "size": st.st_size,
            "modified": st.st_mtime,
            "created": getattr(st, "st_ctime", None),
        }

    def action_find_files(self, params: dict) -> dict:
        root = self._allowed_path(str(params["root"]), True)
        if not root.is_dir():
            raise WorkerError("root is not a directory")
        pattern = str(params.get("pattern") or "*")
        recursive = bool(params.get("recursive", True))
        limit = min(10000, max(1, int(params.get("limit", 500))))
        iterator = root.rglob(pattern) if recursive else root.glob(pattern)
        items = []
        for p in iterator:
            if not p.is_file():
                continue
            try:
                st = p.stat()
                items.append({"path": str(p), "name": p.name, "size": st.st_size, "modified": st.st_mtime})
            except OSError:
                continue
        if bool(params.get("newest_first", True)):
            items.sort(key=lambda x: x["modified"], reverse=True)
        else:
            items.sort(key=lambda x: x["path"].lower())
        return {"root": str(root), "pattern": pattern, "items": items[:limit], "total_found": len(items)}

    def action_read_text(self, params: dict) -> dict:
        path = self._allowed_path(str(params["path"]), True)
        max_chars = min(10_000_000, max(1, int(params.get("max_chars", 1_000_000))))
        text = path.read_text(encoding=str(params.get("encoding", "utf-8")), errors="replace")
        return {"path": str(path), "text": text[:max_chars], "truncated": len(text) > max_chars}

    def action_write_text(self, params: dict) -> dict:
        path = self._allowed_path(str(params["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        text = str(params.get("text", ""))
        path.write_text(text, encoding=str(params.get("encoding", "utf-8")))
        return {"path": str(path), "chars": len(text)}

    def action_append_text(self, params: dict) -> dict:
        path = self._allowed_path(str(params["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        text = str(params.get("text", ""))
        with path.open("a", encoding=str(params.get("encoding", "utf-8"))) as f:
            f.write(text)
        return {"path": str(path), "chars": len(text)}

    def action_mkdir(self, params: dict) -> dict:
        path = self._allowed_path(str(params["path"]))
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path)}

    def action_copy(self, params: dict) -> dict:
        src = self._allowed_path(str(params["src"]), True)
        dst = self._allowed_path(str(params["dst"]))
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=bool(params.get("dirs_exist_ok", False)))
        else:
            shutil.copy2(src, dst)
        return {"src": str(src), "dst": str(dst)}

    def action_move(self, params: dict) -> dict:
        src = self._allowed_path(str(params["src"]), True)
        dst = self._allowed_path(str(params["dst"]))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return {"src": str(src), "dst": str(dst)}

    def action_delete_path(self, params: dict) -> dict:
        path = self._allowed_path(str(params["path"]), True)
        if path in self.allowed_roots:
            raise WorkerError("refusing to delete an allowed root itself")
        if path.is_dir():
            shutil.rmtree(path) if bool(params.get("recursive")) else path.rmdir()
        else:
            path.unlink()
        return {"deleted": str(path)}

    def action_get_file(self, params: dict) -> dict:
        path = self._allowed_path(str(params["path"]), True)
        if not path.is_file():
            raise WorkerError("path is not a file")
        size = path.stat().st_size
        limit = int(self.config.get("max_file_transfer_bytes", 64 * 1024 * 1024))
        if size > limit:
            raise WorkerError(f"file too large for direct transfer: {size} > {limit}")
        raw = path.read_bytes()
        return {
            "path": str(path),
            "name": path.name,
            "size": len(raw),
            "encoding": "base64",
            "data": base64.b64encode(raw).decode("ascii"),
        }

    def action_put_file(self, params: dict) -> dict:
        path = self._allowed_path(str(params["path"]))
        if str(params.get("encoding") or "base64") != "base64":
            raise WorkerError("only base64 file transfer is supported")
        data = str(params.get("data") or "")
        raw = base64.b64decode(data, validate=True)
        limit = int(self.config.get("max_file_transfer_bytes", 64 * 1024 * 1024))
        if len(raw) > limit:
            raise WorkerError(f"file too large for direct transfer: {len(raw)} > {limit}")
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "xb" if bool(params.get("fail_if_exists")) else "wb"
        with path.open(mode) as f:
            f.write(raw)
        return {"path": str(path), "size": len(raw)}

    def action_list_processes(self, params: dict) -> dict:
        cmd = ["tasklist", "/FO", "CSV", "/NH"] if os.name == "nt" else ["ps", "-eo", "pid,comm,args"]
        cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return {"returncode": cp.returncode, "text": (cp.stdout + cp.stderr)[-500_000:]}

    def action_terminate_process(self, params: dict) -> dict:
        if not self.config.get("allow_process_termination"):
            raise WorkerError("process termination is disabled")
        pid = int(params["pid"])
        if pid in {0, 4, os.getpid()}:
            raise WorkerError("refusing protected pid")
        cmd = ["taskkill", "/PID", str(pid), "/T", "/F"] if os.name == "nt" else ["kill", "-TERM", str(pid)]
        cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return {"pid": pid, "returncode": cp.returncode, "output": (cp.stdout + cp.stderr)[-100_000:]}

    def action_run_process(self, params: dict) -> dict:
        if not self.config.get("allow_process_execution"):
            raise WorkerError("process execution is disabled")
        argv = params.get("argv")
        if not isinstance(argv, list) or not argv:
            raise WorkerError("argv must be a non-empty list")
        argv = [str(x) for x in argv]
        executable = Path(argv[0]).name.lower()
        allowed = {str(x).lower() for x in self.config.get("allowed_executables", [])}
        if executable not in allowed:
            raise WorkerError(f"executable is not allowlisted: {executable}")
        cwd = self._allowed_path(str(params["cwd"]), True) if params.get("cwd") else None
        timeout = min(86400, max(1, int(params.get("timeout_seconds", 120))))
        cp = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        return {
            "returncode": cp.returncode,
            "stdout": cp.stdout[-1_000_000:],
            "stderr": cp.stderr[-1_000_000:],
        }

    def action_list_services(self, params: dict) -> dict:
        services = self.config.get("services") or {}
        out = {}
        for name, spec in services.items():
            if not isinstance(spec, dict):
                continue
            out[name] = {
                "description": str(spec.get("description") or ""),
                "cwd": str(spec.get("cwd") or ""),
                "allow_args": bool(spec.get("allow_args", False)),
                "timeout_seconds": int(spec.get("timeout_seconds", 3600)),
            }
        return {"services": out}

    def action_run_service(self, params: dict) -> dict:
        name = str(params.get("name") or "").strip()
        services = self.config.get("services") or {}
        spec = services.get(name)
        if not isinstance(spec, dict):
            raise WorkerError(f"unknown service: {name}")
        argv = spec.get("argv")
        if not isinstance(argv, list) or not argv:
            raise WorkerError(f"service {name} has invalid argv")
        argv = [str(x) for x in argv]
        extra = params.get("args") or []
        if extra:
            if not bool(spec.get("allow_args", False)):
                raise WorkerError(f"service {name} does not accept extra args")
            if not isinstance(extra, list):
                raise WorkerError("service args must be a list")
            argv += [str(x) for x in extra]
        cwd = self._allowed_path(str(spec["cwd"]), True) if spec.get("cwd") else None
        env = os.environ.copy()
        static_env = spec.get("env") or {}
        if not isinstance(static_env, dict):
            raise WorkerError(f"service {name} env must be an object")
        env.update({str(k): str(v) for k, v in static_env.items()})
        timeout = min(86400, max(1, int(spec.get("timeout_seconds", 3600))))
        cp = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        return {
            "service": name,
            "argv": argv,
            "returncode": cp.returncode,
            "stdout": cp.stdout[-1_000_000:],
            "stderr": cp.stderr[-1_000_000:],
        }
