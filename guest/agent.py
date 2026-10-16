"""Standard-library-only guest supervisor. Never run this service on the host."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import json
import os
import signal
import socket
import sys
import tarfile
from pathlib import Path
from typing import Any

MAX_FRAME = 8 * 1024 * 1024
CGROUP = Path("/sys/fs/cgroup/bub")
SELF = "/opt/bub/agent.py"
DEFAULT_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/sandbox", "LANG": "C.UTF-8"}


def worker(spec: dict[str, Any]) -> None:
    # Join the command's cgroup BEFORE dropping privileges or executing user code.
    Path(spec["cgroup"], "cgroup.procs").write_text(str(os.getpid()))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS)")
    os.setgroups([])
    os.setgid(1000)
    os.setuid(1000)
    os.chdir(spec.get("cwd") or "/workspace")
    os.execvpe(spec["argv"][0], spec["argv"], DEFAULT_ENV | spec.get("env", {}))


def file_worker(operation: str, path: str) -> None:
    # This mode is reached through worker(), so file access has workload UID 1000.
    target = Path(path)
    if operation == "read":
        sys.stdout.write(target.read_text(encoding="utf-8"))
    elif operation == "write":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(sys.stdin.read(), encoding="utf-8")
    elif operation == "import":
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as archive:
            archive.extractall("/workspace", filter="data")
    else:
        raise ValueError("unknown file operation")


class Agent:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.lock = asyncio.Lock()
        self.processes: dict[int, tuple[asyncio.subprocess.Process, Path]] = {}
        self.starting = 0
        self.tasks: set[asyncio.Task[Any]] = set()

    def task(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def send(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        if len(payload) > MAX_FRAME:
            raise ValueError("protocol frame too large")
        async with self.lock:
            self.writer.write(payload)
            await self.writer.drain()

    async def pump(self, pid: int, name: str, stream: asyncio.StreamReader) -> None:
        while data := await stream.read(48 * 1024):
            await self.send({"event": name, "pid": pid, "data": base64.b64encode(data).decode()})
        await self.send({"event": name + "_eof", "pid": pid})

    async def watch(self, pid: int, proc: asyncio.subprocess.Process, group: Path) -> None:
        assert proc.stdout is not None and proc.stderr is not None
        stdout = asyncio.create_task(self.pump(pid, "stdout", proc.stdout))
        stderr = asyncio.create_task(self.pump(pid, "stderr", proc.stderr))
        try:
            # asyncio's wait() can wait for inherited pipes after the leader exits.
            # Report leader exit independently of descendants holding those pipes.
            while proc.returncode is None:
                await asyncio.sleep(0.01)
            code = proc.returncode
            await self.send({"event": "exit", "pid": pid, "code": code})
            while "populated 1" in (group / "cgroup.events").read_text():
                await asyncio.sleep(0.02)
            await self.send({"event": "tree_exit", "pid": pid})
            await asyncio.gather(stdout, stderr)
            await self.send({"event": "done", "pid": pid})
        finally:
            stdout.cancel()
            stderr.cancel()
            await asyncio.gather(stdout, stderr, return_exceptions=True)
            self.processes.pop(pid, None)
            with contextlib.suppress(OSError):
                group.rmdir()

    async def dispatch(self, request: dict[str, Any]) -> None:
        request_id = request["id"]
        try:
            operation = request["op"]
            pid = request.get("pid", request_id)
            if operation == "spawn":
                if len(self.processes) + self.starting >= 128:
                    raise RuntimeError("session process limit reached")
                group = CGROUP / str(pid)
                group.mkdir()
                (group / "pids.max").write_text("256")
                spec = {"cgroup": str(group), "argv": request["argv"], "cwd": request["cwd"], "env": request["env"]}
                self.starting += 1
                try:
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-I",
                        SELF,
                        "--exec",
                        json.dumps(spec),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                        env=DEFAULT_ENV,
                    )
                except BaseException:
                    group.rmdir()
                    raise
                finally:
                    self.starting -= 1
                self.processes[pid] = (proc, group)
                await self.send({"id": request_id, "result": None})
                self.task(self.watch(pid, proc, group))
                return
            if operation == "ping":
                result: Any = {"protocol": 1, "platform": sys.platform, "machine": os.uname().machine}
            elif operation in ("stdin", "stdin_close", "signal"):
                entry = self.processes.get(pid)
                if entry:
                    proc, group = entry
                    if operation == "stdin":
                        assert proc.stdin is not None
                        proc.stdin.write(base64.b64decode(request["data"], validate=True))
                        await proc.stdin.drain()
                    elif operation == "stdin_close":
                        assert proc.stdin is not None
                        proc.stdin.close()
                    elif request["kill"]:
                        (group / "cgroup.kill").write_text("1")
                    else:
                        # Enumerating the cgroup reaches descendants even after setsid().
                        for process_id in (group / "cgroup.procs").read_text().split():
                            with contextlib.suppress(ProcessLookupError):
                                os.kill(int(process_id), signal.SIGTERM)
                result = None
            else:
                raise ValueError(f"unknown operation: {operation}")
            await self.send({"id": request_id, "result": result})
        except Exception as error:
            await self.send({"id": request_id, "error": f"{type(error).__name__}: {error}"})

    async def run(self) -> None:
        # cgroup.kill is mandatory: silently weakening process cleanup is unsafe.
        CGROUP.mkdir(exist_ok=True)
        (Path("/sys/fs/cgroup") / "cgroup.subtree_control").write_text("+pids")
        (CGROUP / "cgroup.subtree_control").write_text("+pids")
        if not (CGROUP / "cgroup.kill").exists():
            raise RuntimeError("guest kernel needs cgroup v2 cgroup.kill support")
        await self.send({"event": "hello", "protocol": 1})
        try:
            while line := await self.reader.readline():
                if len(self.tasks) >= 256:
                    raise RuntimeError("too many outstanding requests")
                self.task(self.dispatch(json.loads(line)))
        finally:
            for _, group in tuple(self.processes.values()):
                with contextlib.suppress(OSError):
                    (group / "cgroup.kill").write_text("1")
            for task in tuple(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            self.writer.close()
            await self.writer.wait_closed()


async def main() -> None:
    connection = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    connection.connect((2, 18000))
    connection.setblocking(False)
    reader, writer = await asyncio.open_connection(sock=connection, limit=MAX_FRAME)
    await Agent(reader, writer).run()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--exec":
        worker(json.loads(sys.argv[2]))
    elif len(sys.argv) > 1 and sys.argv[1] == "--file":
        file_worker(sys.argv[2], sys.argv[3])
    else:
        asyncio.run(main())
