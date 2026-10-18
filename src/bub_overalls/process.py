"""Own subprocess startup and process-group shutdown for an Overalls environment."""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from bub.builtin.environment import LocalProcess

logger = logging.getLogger(__name__)

_STOP_TIMEOUT_SECONDS = 3.0
_TERMINATE_TIMEOUT_SECONDS = 1.0


async def _await_cleanup(
    task: asyncio.Task[None], *, on_cancel: Callable[[], None] | None = None
) -> None:
    """Finish owned cleanup even if the caller is cancelled repeatedly."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            if on_cancel is not None:
                on_cancel()
    task.result()
    if cancelled:
        raise asyncio.CancelledError


@dataclass(frozen=True, slots=True)
class _OverallsProcessBatch:
    """Signal and reap a fixed snapshot without owning the live registry."""

    processes: tuple[LocalProcess, ...]

    def signal(self, *, kill: bool) -> list[Exception]:
        errors: list[Exception] = []
        for process in self.processes:
            try:
                process.signal(kill=kill)
            except OSError as error:
                error.add_note(f"Failed to signal sandbox process group {process.pid}")
                errors.append(error)
            try:
                process.close_stdin()
            except OSError as error:
                errors.append(error)
        return errors

    async def wait(self, *, drain: bool = False) -> list[Exception]:
        results: Sequence[object]
        try:
            async with asyncio.timeout(_STOP_TIMEOUT_SECONDS):
                if drain:
                    # Only use this when the owner has stopped all other pipe readers.
                    results = await asyncio.gather(
                        *(process.process.communicate() for process in self.processes),
                        return_exceptions=True,
                    )
                else:
                    results = await asyncio.gather(
                        *(process.wait() for process in self.processes),
                        return_exceptions=True,
                    )
        except TimeoutError:
            logger.warning("Timed out waiting for sandbox process cleanup.")
            return []
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
        return [result for result in results if isinstance(result, Exception)]

    async def stop(self, *, drain: bool = False) -> None:
        errors = self.signal(kill=True)
        errors.extend(await self.wait(drain=drain))
        if errors:
            raise ExceptionGroup("Failed to stop sandbox process groups", errors)

    async def wait_for_termination(
        self, force_stop: asyncio.Event, deadline: float
    ) -> bool:
        loop = asyncio.get_running_loop()
        while not force_stop.is_set():
            if not any(process.is_running() for process in self.processes):
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            # The leader may exit while a descendant keeps the group alive. Probe
            # groups until the shared deadline, and wake immediately on cancellation.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(min(0.02, remaining)):
                    await force_stop.wait()
        return False


class OverallsProcess(LocalProcess):
    """Keep hard termination idempotent across independent cleanup owners."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        super().__init__(process)
        self._killed = False

    def signal(self, *, kill: bool) -> None:
        if self._killed:
            return
        super().signal(kill=kill)
        # macOS can return EPERM for a second killpg while the group is exiting.
        # A successful SIGKILL already prevents its members from running again.
        self._killed = kill


class OverallsProcessManager:
    """Keep every child owned from startup until shutdown finishes."""

    def __init__(self) -> None:
        self._processes: set[LocalProcess] = set()
        self._starting: set[asyncio.Task[LocalProcess]] = set()
        self._closed = False
        self._force_stop = asyncio.Event()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._errors: list[Exception] = []

    @property
    def closed(self) -> bool:
        return self._closed

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None,
        env: Mapping[str, str],
    ) -> LocalProcess:
        if self._closed:
            raise RuntimeError("sandbox environment is closed")
        self._processes = {
            process for process in self._processes if process.is_running()
        }
        # Startup registers its child before publishing it to any caller.
        task = asyncio.create_task(self._start(argv, cwd=cwd, env=env))
        self._starting.add(task)
        try:
            try:
                process = await asyncio.shield(task)
            except asyncio.CancelledError:
                await _await_cleanup(asyncio.create_task(self._cancel_start(task)))
                raise
            if self._closed:
                await self.stop(process)
                raise RuntimeError("sandbox environment closed during spawn")
            return process
        finally:
            self._starting.discard(task)

    async def _start(
        self, argv: Sequence[str], *, cwd: str | None, env: Mapping[str, str]
    ) -> LocalProcess:
        child = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
        process = OverallsProcess(child)
        self._processes.add(process)
        if self._closed:
            # A child that starts during shutdown immediately joins its policy.
            self._errors.extend(
                _OverallsProcessBatch((process,)).signal(kill=self._force_stop.is_set())
            )
        return process

    async def _cancel_start(self, task: asyncio.Task[LocalProcess]) -> None:
        try:
            process = await task
        except (OSError, ValueError):
            # Preserve cancellation for OS failures or invalid subprocess arguments.
            # Unexpected startup errors must remain visible to the caller.
            return
        await _OverallsProcessBatch((process,)).stop(drain=True)

    async def stop(self, process: LocalProcess) -> None:
        """Kill and drain a worker after its operation has released the pipes."""
        await _await_cleanup(
            asyncio.create_task(_OverallsProcessBatch((process,)).stop(drain=True))
        )

    def _force(self) -> None:
        if not self._force_stop.is_set():
            self._force_stop.set()
            # Wake grace waits and stop registered children even if startup stalls.
            self._errors.extend(
                _OverallsProcessBatch(tuple(self._processes)).signal(kill=True)
            )

    async def close(self, *, kill: bool = False) -> None:
        self._closed = True
        caller = asyncio.current_task()
        if kill or (caller is not None and caller.cancelling()):
            self._force()
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup())
        await _await_cleanup(self._cleanup_task, on_cancel=self._force)

    async def _cleanup(self) -> None:
        deadline = asyncio.get_running_loop().time() + _TERMINATE_TIMEOUT_SECONDS
        self._errors.extend(
            _OverallsProcessBatch(tuple(self._processes)).signal(
                kill=self._force_stop.is_set()
            )
        )
        await asyncio.gather(*self._starting, return_exceptions=True)
        processes = _OverallsProcessBatch(tuple(self._processes))
        stopped = False
        if not self._errors:
            try:
                stopped = await processes.wait_for_termination(
                    self._force_stop, deadline
                )
            except OSError as error:
                self._errors.append(error)
        if not stopped:
            self._errors.extend(processes.signal(kill=True))
        self._errors.extend(await processes.wait())
        self._processes.clear()
        if self._errors:
            raise ExceptionGroup("Failed to stop sandbox process groups", self._errors)
