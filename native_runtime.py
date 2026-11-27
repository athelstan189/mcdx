import fcntl
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager


class RuntimeBusy(ValueError):
    pass


def process_env():
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        original = env.get("LD_LIBRARY_PATH_ORIG")
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
    return env


@contextmanager
def locked(path, reason):
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeBusy(reason) from exc
        yield


def import_sessions(source_home, shared, *, quiet=False):
    folders = ("sessions", "archived_sessions")
    imported = existing = changed = 0
    lock_dir = shared / "multicodex" if (shared / "multicodex").is_dir() else shared
    with locked(lock_dir / ".import.lock", "A session import is already running."):
        known = {
            path.name.removesuffix(".zst")
            for folder in folders
            for path in (shared / folder).rglob("rollout-*.jsonl*")
            if path.is_file()
        }
        for folder in folders:
            source = source_home / folder
            for path in sorted(source.rglob("rollout-*.jsonl*")):
                if path.is_symlink() or not path.is_file() or not path.name.endswith((".jsonl", ".jsonl.zst")):
                    continue
                session = path.name.removesuffix(".zst")
                if session in known:
                    existing += 1
                    continue
                target = shared / folder / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=target.parent) as staged, path.open("rb") as original:
                    before = os.fstat(original.fileno())
                    shutil.copyfileobj(original, staged)
                    staged.flush()
                    try:
                        after = path.stat()
                    except FileNotFoundError:
                        changed += 1
                        continue
                    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                        after.st_ino, after.st_size, after.st_mtime_ns
                    ):
                        changed += 1
                        continue
                    os.utime(staged.name, ns=(before.st_atime_ns, before.st_mtime_ns))
                    try:
                        os.link(staged.name, target)
                    except FileExistsError:
                        existing += 1
                    else:
                        imported += 1
                    known.add(session)
    if not quiet or changed:
        print(f"Imported {imported} session files. Already present: {existing}. Changed while reading: {changed}.")
    return int(changed != 0)


class NativeRuntime:
    def __init__(self, root, shared, home, workspace):
        bundled = root / "codex-runtime/bin/codex"
        self.binary = str(bundled) if bundled.is_file() else shutil.which("codex")
        if not self.binary:
            raise ValueError("Codex runtime missing. Use the MultiCodex release package or install Codex for development.")
        self.home = home
        self.codex_home = home
        self.shared = shared
        self.workspace = workspace
        self.env = {**process_env(), "CODEX_HOME": str(self.codex_home),
                    "CODEX_SQLITE_HOME": str(shared)}
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
            self.env.pop(key, None)
        self.command = [self.binary]
        for key, value in (
            ("cli_auth_credentials_store", "file"),
            ("mcp_oauth_credentials_store", "file"),
            ("sqlite_home", str(shared)),
            ("log_dir", str(self.codex_home / "log")),
        ):
            self.command += ["-c", f"{key}={json.dumps(value, ensure_ascii=False)}"]
        self.command += ["-c", "check_for_update_on_startup=false"]

    def invocation(self, action, thread=None):
        args = {
            "login": ["login", "--device-auth"],
            "browser-login": ["login"],
            "status": ["login", "status"],
            "logout": ["logout"],
            "open": [],
            "resume": ["resume", "--", thread] if thread else ["resume"],
        }[action]
        cwd = self.workspace if action in ("open", "resume") else self.home
        return self.command + args, {"cwd": cwd, "env": self.env}

    def run(self, action, thread=None):
        command, options = self.invocation(action, thread)
        return subprocess.call(command, **options)

    def sessions(self):
        command = self.command + ["app-server", "--listen", "stdio://"]
        with app_server(command, cwd=self.workspace, env=self.env) as request:
            return self.read_sessions(request)

    def read_sessions(self, request):
        params = {"cwd": str(self.workspace), "limit": 100, "sortKey": "updated_at",
                  "currentAccountOnly": False}
        sessions = []
        while True:
            page = request("thread/list", params)
            sessions.extend(page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                return sessions
            if cursor == params.get("cursor"):
                raise ValueError("Codex returned a repeated session history cursor.")
            params["cursor"] = cursor


@contextmanager
def app_server(command, *, timeout=15, **options):
    with tempfile.TemporaryFile() as errors, subprocess.Popen(
        command, **options, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
    ) as process:
        try:
            yield app_server_requester(process, timeout)
        except (BrokenPipeError, EOFError, TimeoutError) as exc:
            errors.seek(0, os.SEEK_END)
            errors.seek(max(0, errors.tell() - 1536))
            details = errors.read().decode("utf-8", "replace").strip()
            raise ValueError(details or str(exc)) from exc
        finally:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def app_server_requester(process, timeout):
    request_id = 0
    pending = b""
    fd = process.stdout.fileno()

    def send(message):
        process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        process.stdin.flush()

    def request(method, params):
        nonlocal request_id, pending
        request_id += 1
        send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            line, sep, rest = pending.partition(b"\n")
            if sep:
                pending = rest
                message = json.loads(line)
                if message.get("id") == request_id:
                    if "error" in message:
                        raise ValueError(message["error"]["message"])
                    return message["result"]
                continue
            wait = max(0, deadline - time.monotonic())
            if not select.select([fd], [], [], wait)[0]:
                raise TimeoutError(f"Timed out waiting for {method}.")
            chunk = os.read(fd, 65536)
            if not chunk:
                raise EOFError("Codex closed its App Server.")
            pending += chunk

    request("initialize", {"clientInfo": {"name": "multicodex", "version": "0.1.0"}})
    send({"method": "initialized"})
    return request
