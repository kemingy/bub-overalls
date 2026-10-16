"""A session owns one VM, one private disk clone, and a multiplexed guest connection."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import tarfile
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from bub.environment import CallTool, Environment, Process

from bub_sandbox.protocol import MAX_BUFFER, MAX_FRAME, receive, send
from bub_sandbox.settings import SandboxSettings

EXCLUDED = {
    "build",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
    ".venv",
    ".aws",
    ".ssh",
    ".codex",
    "__pycache__",
    "node_modules",
}


def workspace_archive(workspace: Path, destination: Path) -> None:
    """Copy regular files and directories only; never follow host symlinks."""
    size = 0
    count = 0
    with tarfile.open(destination, "w") as archive:
        for directory, directories, files in os.walk(workspace, followlinks=False):
            directories[:] = [name for name in directories if name not in EXCLUDED and not name.startswith(".env")]
            for name in [*directories, *files]:
                path = Path(directory) / name
                if path.is_symlink() or name in EXCLUDED or name.startswith(".env") or not path.exists():
                    continue
                if not path.is_file() and not path.is_dir():
                    continue
                size += path.stat().st_size if path.is_file() else 0
                count += 1
                if size > 128 * 1024 * 1024 or count > 10000:
                    raise ValueError(
                        "workspace import exceeds 128 MiB or 10,000 entries; disable import or narrow workspace"
                    )
                info = archive.gettarinfo(str(path), arcname=str(path.relative_to(workspace)))
                info.uid = info.gid = 1000
                info.uname = info.gname = "sandbox"
                info.mode &= 0o777
                if path.is_file():
                    with path.open("rb") as source:
                        archive.addfile(info, source)
                else:
                    archive.addfile(info)


class GuestProcess(Process):
    def __init__(self, environment: MicroVMEnvironment, pid: int) -> None:
        self.environment = environment
        self.pid = pid
        self._stdout = asyncio.StreamReader()
        self._stderr = asyncio.StreamReader()
        self._returncode: int | None = None
        self._exited: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._alive = True

    @property
    def stdout(self) -> asyncio.StreamReader:
        return self._stdout

    @property
    def stderr(self) -> asyncio.StreamReader:
        return self._stderr

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        return await asyncio.shield(self._exited)

    async def write_stdin(self, data: bytes) -> None:
        for offset in range(0, len(data), 48 * 1024):
            await self.environment._request(
                "stdin", pid=self.pid, data=base64.b64encode(data[offset : offset + 48 * 1024]).decode()
            )

    def close_stdin(self) -> None:
        self.environment._background(self.environment._request("stdin_close", pid=self.pid))

    def signal(self, *, kill: bool) -> None:
        if self._alive:
            self.environment._background(self.environment._request("signal", pid=self.pid, kill=kill))

    def is_running(self) -> bool:
        return self._alive

    def event(self, message: dict[str, Any]) -> None:
        event = message["event"]
        if event in ("stdout", "stderr"):
            data = base64.b64decode(message["data"], validate=True)
            stream = self._stdout if event == "stdout" else self._stderr
            # Bound unread data; abort the VM instead of silently corrupting output.
            if len(stream._buffer) + len(data) > MAX_BUFFER:  # type: ignore[attr-defined]
                raise BufferError("guest process output exceeded 8 MiB of unread data")
            stream.feed_data(data)
        elif event in ("stdout_eof", "stderr_eof"):
            (self._stdout if event == "stdout_eof" else self._stderr).feed_eof()
        elif event == "exit":
            self._returncode = int(message["code"])
            if not self._exited.done():
                self._exited.set_result(self._returncode)
        elif event == "tree_exit":
            self._alive = False

    def fail(self, error: Exception) -> None:
        self._alive = False
        if self._returncode is None:
            self._returncode = -1
        self._stdout.set_exception(error)
        self._stderr.set_exception(error)
        if not self._exited.done():
            # An actual guest exit code is unavailable after a transport/VM failure.
            self._exited.set_result(-1)


class MicroVMEnvironment(Environment):
    workspace = "/workspace"
    python = "/usr/local/bin/python3"
    RPC_TIMEOUT_SECONDS = 30.0

    def __init__(self) -> None:
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._processes: dict[int, GuestProcess] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._counter = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._closing_task: asyncio.Task[None] | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._vm: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._server: asyncio.AbstractServer | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._log: Any = None

    @classmethod
    async def create(cls, settings: SandboxSettings, workspace: Path) -> MicroVMEnvironment:
        settings.validate()
        self = cls()
        # /tmp keeps paths within macOS sockaddr_un's short path limit. mkdtemp is mode 0700.
        self._temporary = tempfile.TemporaryDirectory(prefix="bub-vm-", dir="/tmp")
        directory = Path(self._temporary.name)
        disk = directory / "root.raw"
        server: asyncio.AbstractServer | None = None
        try:
            # Copy before launching a VM; cancellation cannot remove a disk still in use by cp.
            copy = await asyncio.create_subprocess_exec(
                "cp", "-c" if os.uname().sysname == "Darwin" else "--reflink=auto", str(settings.image), str(disk)
            )
            try:
                if await copy.wait() != 0:
                    raise RuntimeError("failed to clone guest disk")
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    copy.kill()
                await copy.wait()
                raise
            disk.chmod(0o600)
            connected: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
                asyncio.get_running_loop().create_future()
            )

            def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                if connected.done() or self._closed:
                    writer.close()
                else:
                    self._writer = writer
                    connected.set_result((reader, writer))

            socket_path = directory / "rpc.sock"
            server = await asyncio.start_unix_server(accept, str(socket_path), limit=MAX_FRAME)
            self._server = server
            self._log = (directory / "vm.log").open("wb")
            self._vm = await asyncio.create_subprocess_exec(
                str(settings.helper),
                str(disk),
                str(socket_path),
                str(settings.cpus),
                str(settings.memory_mib),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=self._log,
                stderr=self._log,
                start_new_session=True,
                env={"PATH": os.defpath},
            )
            async with asyncio.timeout(settings.boot_timeout):
                # Race readiness against early helper exit so configuration failures are immediate.
                exited = asyncio.create_task(self._vm.wait())
                try:
                    done, _ = await asyncio.wait((connected, exited), return_when=asyncio.FIRST_COMPLETED)
                    if exited in done:
                        log = (directory / "vm.log").read_text(errors="replace")[-4000:]
                        raise RuntimeError(f"libkrun exited ({exited.result()}): {log}")
                    reader, self._writer = connected.result()
                    if await receive(reader) != {"event": "hello", "protocol": 1}:
                        raise RuntimeError("guest protocol handshake failed")
                finally:
                    exited.cancel()
                    await asyncio.gather(exited, return_exceptions=True)
            self._reader_task = asyncio.create_task(self._read(reader))
            self._monitor = asyncio.create_task(self._watch_vm())
            if settings.import_workspace:
                archive = directory / "workspace.tar"
                workspace_archive(workspace, archive)
                process = await self.spawn([self.python, "-I", "/opt/bub/agent.py", "--file", "import", "/workspace"])
                output = asyncio.create_task(process.stdout.read())
                errors = asyncio.create_task(process.stderr.read())
                try:
                    with archive.open("rb") as source:
                        while data := source.read(48 * 1024):
                            await process.write_stdin(data)
                    process.close_stdin()
                    if await process.wait() != 0:
                        raise RuntimeError(f"workspace import failed: {(await errors).decode(errors='replace')}")
                    await asyncio.gather(output, errors)
                finally:
                    output.cancel()
                    errors.cancel()
                    await asyncio.gather(output, errors, return_exceptions=True)
                archive.unlink()
            return self
        except BaseException:
            await self.close()
            raise
        finally:
            if server is not None:
                server.close()

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter

    async def _request(self, operation: str, *, _id: int | None = None, **arguments: Any) -> Any:
        if self._closed or self._writer is None:
            raise ConnectionError("sandbox environment is closed")
        request_id = _id if _id is not None else self._next_id()
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(self.RPC_TIMEOUT_SECONDS):
                async with self._lock:
                    await send(self._writer, {"id": request_id, "op": operation, **arguments})
                return await future
        except TimeoutError:
            self._fail(ConnectionError("guest RPC timed out"))
            asyncio.create_task(self.close())
            raise
        finally:
            self._pending.pop(request_id, None)

    def _background(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)

        def complete(task: asyncio.Task[Any]) -> None:
            self._tasks.discard(task)
            if not task.cancelled() and task.exception() is not None and not self._closed:
                asyncio.create_task(self.close())

        task.add_done_callback(complete)

    async def _read(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                message = await receive(reader)
                if "event" in message:
                    process = self._processes.get(message["pid"])
                    if process is not None:
                        process.event(message)
                        if message["event"] == "done":
                            self._processes.pop(process.pid, None)
                else:
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(RuntimeError(message["error"]))
                        else:
                            future.set_result(message["result"])
        except Exception as error:
            self._fail(error)
            asyncio.create_task(self.close())

    async def _watch_vm(self) -> None:
        assert self._vm is not None
        code = await self._vm.wait()
        if not self._closed:
            self._fail(ConnectionError(f"libkrun exited unexpectedly ({code})"))
            asyncio.create_task(self.close())

    def _fail(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        for process in self._processes.values():
            process.fail(error)

    async def spawn(
        self, command: str | Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None
    ) -> GuestProcess:
        if isinstance(command, str):
            argv = ["/bin/bash", "-c", command]
        else:
            argv = list(command)
        if not argv or not all(isinstance(arg, str) and "\0" not in arg for arg in argv):
            raise ValueError("command must contain nonempty string arguments without NUL bytes")
        pid = self._next_id()
        process = GuestProcess(self, pid)
        self._processes[pid] = process
        request = asyncio.create_task(
            self._request("spawn", _id=pid, argv=argv, cwd=cwd or self.workspace, env=dict(env or {}))
        )
        try:
            await asyncio.shield(request)
        except asyncio.CancelledError:
            try:
                await request
                await self._request("signal", pid=pid, kill=True)
                await process.wait()
            finally:
                raise
        except Exception:
            self._processes.pop(pid, None)
            raise
        return process

    async def _file(self, operation: str, path: str, content: str | None = None) -> str:
        process = await self.spawn(
            [self.python, "-I", "/opt/bub/agent.py", "--file", operation, self.resolve_path(path)]
        )
        output = asyncio.create_task(process.stdout.read())
        errors = asyncio.create_task(process.stderr.read())
        try:
            if content is not None:
                await process.write_stdin(content.encode())
            process.close_stdin()
            code = await process.wait()
            stdout, stderr = await asyncio.gather(output, errors)
            if code != 0:
                raise RuntimeError(stderr.decode(errors="replace"))
            return stdout.decode()
        finally:
            process.signal(kill=True)
            output.cancel()
            errors.cancel()
            await asyncio.gather(output, errors, return_exceptions=True)

    async def read_text(self, path: str) -> str:
        return await self._file("read", path)

    async def write_text(self, path: str, content: str) -> None:
        await self._file("write", path, content)

    async def run_code(
        self, code: str, *, tools: Sequence[str], call_tool: CallTool, write: Callable[[str], None]
    ) -> None:
        from bub.builtin.codemode.code_runner import run_code_in_subprocess

        await run_code_in_subprocess(self, code, tools=tools, call_tool=call_tool, write=write)

    async def close(self) -> None:
        if self._closing_task is None:
            self._closed = True
            self._closing_task = asyncio.create_task(self._close())
        cancelled = False
        while not self._closing_task.done():
            try:
                await asyncio.shield(self._closing_task)
            except asyncio.CancelledError:
                cancelled = True
        self._closing_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> None:
        self._fail(ConnectionError("sandbox environment closed"))
        if self._writer is not None:
            self._writer.close()
        current = asyncio.current_task()
        tasks = [task for task in [self._reader_task, self._monitor, *self._tasks] if task and task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._vm is not None and self._vm.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._vm.terminate()
            try:
                async with asyncio.timeout(3):
                    await self._vm.wait()
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self._vm.kill()
                await self._vm.wait()
        if self._writer is not None:
            with contextlib.suppress(ConnectionError, TimeoutError):
                async with asyncio.timeout(1):
                    await self._writer.wait_closed()
            self._writer.transport.abort()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._log is not None:
            self._log.close()
        if self._temporary is not None:
            self._temporary.cleanup()
