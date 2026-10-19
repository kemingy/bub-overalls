"""Run Bub processes and file operations under a shared platform sandbox policy."""

import asyncio
import contextlib
import json
import os
import platform
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from bub.builtin.environment import LocalEnvironment, LocalProcess
from bub.environment import CallTool

from bub_overalls.config import OverallsConfig
from bub_overalls.git import OverallsGitPaths
from bub_overalls.landlock import (
    _LAUNCHER_SOURCE,
    build_overalls_landlock_policy,
)
from bub_overalls.process import OverallsProcessManager, _await_cleanup
from bub_overalls.profile import build_overalls_profile

_FILE_WORKER_SOURCE = """
import sys
from pathlib import Path

path = Path(sys.argv[2])
if sys.argv[1] == "read":
    sys.stdout.buffer.write(path.read_bytes())
else:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(sys.stdin.buffer.read())
"""


class OverallsEnvironment(LocalEnvironment):
    """Run local tools and file helpers under one platform sandbox policy."""

    def __init__(self, config: OverallsConfig, workspace: Path) -> None:
        config.validate()
        system = platform.system()
        if system == "Linux":
            self.sandbox_backend = "Landlock/seccomp"
        elif system == "Darwin":
            if not Path("/usr/bin/sandbox-exec").is_file():
                raise FileNotFoundError("macOS sandbox-exec is unavailable")
            self.sandbox_backend = "Seatbelt"
        else:
            raise RuntimeError(f"unsupported sandbox platform: {system}")
        workspace_path = workspace.expanduser().resolve(strict=True)
        if not workspace_path.is_dir():
            raise ValueError(f"workspace is not a directory: {workspace_path}")
        super().__init__(workspace_path)
        self.config = config
        self.git_paths = OverallsGitPaths.discover(
            workspace_path, strict=config.git_access
        )
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="bub-overalls-")
        self.temporary_path = Path(self._temporary_directory.name).resolve()
        try:
            if system == "Darwin":
                profile = build_overalls_profile(
                    config,
                    workspace_path,
                    self.temporary_path,
                    git_paths=self.git_paths,
                )
                self._sandbox_prefix = ["/usr/bin/sandbox-exec", "-p", profile]
            else:
                policy = build_overalls_landlock_policy(
                    config,
                    workspace_path,
                    self.temporary_path,
                    git_paths=self.git_paths,
                )
                self._sandbox_prefix = [
                    str(Path(self.python).resolve()),
                    "-I",
                    "-S",
                    "-c",
                    _LAUNCHER_SOURCE,
                    policy.serialize(),
                ]
        except BaseException:
            self._temporary_directory.cleanup()
            raise
        self._process_manager = OverallsProcessManager()
        self._ready = False

    @classmethod
    async def create(cls, config: OverallsConfig, workspace: Path) -> Self:
        """Create an environment whose sandbox preflight has succeeded."""
        return await cls(config, workspace).__aenter__()

    async def __aenter__(self) -> Self:
        if self._process_manager.closed:
            raise RuntimeError("sandbox environment is closed")
        if self._ready:
            return self
        try:
            process = await self.spawn(["/usr/bin/true"])
            process.close_stdin()
            _, stderr, returncode = await asyncio.gather(
                process.stdout.read(), process.stderr.read(), process.wait()
            )
            if returncode:
                raise RuntimeError(
                    f"{self.sandbox_backend} preflight failed ({returncode}): "
                    f"{stderr.decode(errors='replace')}"
                )
        except BaseException:
            await self._close(kill=True)
            raise
        self._ready = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._close(kill=exc_type is not None)

    async def spawn(
        self,
        command: str | Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> LocalProcess:
        if self._process_manager.closed:
            raise RuntimeError("sandbox environment is closed")
        argv = (
            ["/bin/bash", "--noprofile", "--norc", "-c", command]
            if isinstance(command, str)
            else list(command)
        )
        if not argv:
            raise ValueError("command cannot be empty")
        process_env = self._build_process_env(env)
        if self.sandbox_backend == "Landlock/seccomp":
            # Loader/Python overrides belong to the target after confinement, not
            # to the trusted bootstrap. Keep target settings out of argv/ps output.
            target_env = json.dumps(process_env)
            process_env = self._build_process_env(None)
            process_env["_BUB_OVERALLS_TARGET_ENV"] = target_env
        return await self._process_manager.spawn(
            [*self._sandbox_prefix, *argv],
            cwd=cwd or self.workspace,
            env=process_env,
        )

    def _build_process_env(self, overrides: Mapping[str, str] | None) -> dict[str, str]:
        process_env = {
            key: os.environ[key]
            for key in ("USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "DEVELOPER_DIR")
            if key in os.environ
        }
        process_env.update(
            PATH=os.environ.get("PATH", os.defpath),
            HOME=str(Path.home()),
            TMPDIR=str(self.temporary_path),
            PYTHONNOUSERSITE="1",
            PYTHONDONTWRITEBYTECODE="1",
        )
        if self.config.git_access:
            # Git treats an unreadable global config as fatal. Keep home reads
            # explicit and use repository-local identity/settings by default.
            process_env["GIT_CONFIG_GLOBAL"] = os.devnull
        process_env.update(overrides or {})
        return process_env

    async def _run_file_operation(
        self, operation: Literal["read", "write"], path: str, content: bytes = b""
    ) -> bytes:
        process = await self.spawn(
            [
                self.python,
                "-I",
                "-c",
                _FILE_WORKER_SOURCE,
                operation,
                self.resolve_path(path),
            ]
        )
        try:
            stdout, stderr = await process.process.communicate(content)
            if process.returncode:
                raise OSError(
                    f"sandbox file {operation} failed: "
                    f"{stderr.decode(errors='replace').strip()}"
                )
            return stdout
        finally:
            await self._process_manager.stop(process)

    async def read_text(self, path: str) -> str:
        return (await self._run_file_operation("read", path)).decode("utf-8")

    async def write_text(self, path: str, content: str) -> None:
        await self._run_file_operation("write", path, content.encode("utf-8"))

    async def run_code(
        self,
        code: str,
        *,
        tools: Sequence[str],
        call_tool: CallTool,
        write: Callable[[str], None],
    ) -> None:
        # Bub stops the code runner in finally; repeated cancellation must not
        # interrupt that cleanup or the draining of its protocol tasks.
        task = asyncio.create_task(
            super().run_code(code, tools=tools, call_tool=call_tool, write=write)
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _await_cleanup(task)
            raise

    async def _close(self, *, kill: bool) -> None:
        try:
            await self._process_manager.close(kill=kill)
        finally:
            self._temporary_directory.cleanup()

    async def close(self) -> None:
        """Allow brief graceful termination, then kill remaining process groups."""
        await self._close(kill=False)
