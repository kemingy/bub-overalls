import asyncio
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock, call

import pytest
from bub.builtin.environment import LocalProcess

from bub_overalls.config import OverallsConfig
from bub_overalls.environment import OverallsEnvironment
from bub_overalls.process import OverallsProcess


@pytest.fixture(autouse=True)
def supported_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    monkeypatch.setattr("bub_overalls.process._TERMINATE_TIMEOUT_SECONDS", 0.01)


def test_linux_is_explicitly_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    config = OverallsConfig()
    with pytest.raises(RuntimeError, match="macOS only"):
        OverallsEnvironment(config, tmp_path)


def test_missing_sandbox_exec_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "is_file", lambda _: False)
    config = OverallsConfig()
    with pytest.raises(FileNotFoundError, match="sandbox-exec is unavailable"):
        OverallsEnvironment(config, tmp_path)


async def test_failed_preflight_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    async def fail(*args: object, **kwargs: object) -> None:
        raise PermissionError("sandbox unavailable")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fail)
    with pytest.raises(PermissionError, match="sandbox unavailable"):
        await OverallsEnvironment.create(OverallsConfig(), tmp_path)


async def test_file_helpers_use_sandbox_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)

    async def fail(*args: object, **kwargs: object) -> None:
        raise PermissionError("spawn boundary")

    monkeypatch.setattr(environment, "spawn", fail)
    try:
        with pytest.raises(PermissionError, match="spawn boundary"):
            await environment.read_text(str(tmp_path / "secret"))
        with pytest.raises(PermissionError, match="spawn boundary"):
            await environment.write_text(str(tmp_path / "secret"), "value")
        assert not (tmp_path / "secret").exists()
    finally:
        await environment.close()


async def test_close_is_idempotent_and_blocks_new_commands(tmp_path: Path) -> None:
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    temporary_path = environment.temporary_path
    await asyncio.gather(environment.close(), environment.close())
    assert not temporary_path.exists()
    with pytest.raises(RuntimeError, match="closed"):
        await environment.spawn(["true"])


async def test_context_entry_failure_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)

    async def reject_spawn(*args: object, **kwargs: object) -> None:
        raise PermissionError("sandbox unavailable")

    monkeypatch.setattr(environment, "spawn", reject_spawn)
    with pytest.raises(PermissionError, match="sandbox unavailable"):
        async with environment:
            pytest.fail("context body must not run after preflight failure")
    assert not environment.temporary_path.exists()


async def test_closed_environment_cannot_enter_context(tmp_path: Path) -> None:
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    await environment.close()
    with pytest.raises(RuntimeError, match="closed"):
        async with environment:
            pytest.fail("context body must not run after close")


@pytest.mark.parametrize(
    "startup_error", [None, OSError("startup failed"), ValueError("invalid argument")]
)
async def test_cancelled_spawn_finishes_startup_before_propagating_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup_error: Exception | None
) -> None:

    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    process = Mock()
    process.process.communicate = AsyncMock(return_value=(b"", b""))
    process.wait = AsyncMock(return_value=-9)
    process.is_running.return_value = True
    monkeypatch.setattr("bub_overalls.process.OverallsProcess", lambda _: process)

    async def delayed_spawn(
        *args: object, **kwargs: object
    ) -> asyncio.subprocess.Process:
        started.set()
        await release.wait()
        if startup_error is not None:
            raise startup_error
        return cast(asyncio.subprocess.Process, Mock())

    monkeypatch.setattr("asyncio.create_subprocess_exec", delayed_spawn)
    try:
        task = asyncio.create_task(environment.spawn(["true"]))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        if startup_error is None:
            process.signal.assert_called_with(kill=True)
            process.process.communicate.assert_awaited_once()
    finally:
        release.set()
        await environment.close()


@pytest.mark.parametrize(
    "startup_error", [TypeError("startup bug"), RuntimeError("startup bug")]
)
async def test_cancelled_spawn_does_not_hide_unexpected_startup_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup_error: Exception
) -> None:
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    async def fail_startup(
        *args: object, **kwargs: object
    ) -> asyncio.subprocess.Process:
        started.set()
        await release.wait()
        raise startup_error

    monkeypatch.setattr("asyncio.create_subprocess_exec", fail_startup)
    task = asyncio.create_task(environment.spawn(["true"]))
    try:
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(type(startup_error), match="startup bug"):
            await task
    finally:
        release.set()
        await environment.close()


async def test_close_waits_for_inflight_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    process = Mock()
    process.wait = AsyncMock(return_value=-9)
    process.process.communicate = AsyncMock(return_value=(b"", b""))
    monkeypatch.setattr("bub_overalls.process.OverallsProcess", lambda _: process)

    async def delayed_spawn(
        *args: object, **kwargs: object
    ) -> asyncio.subprocess.Process:
        started.set()
        await release.wait()
        return cast(asyncio.subprocess.Process, Mock())

    monkeypatch.setattr("asyncio.create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(environment.spawn(["true"]))
    await started.wait()
    closing = asyncio.create_task(environment.close())
    await asyncio.sleep(0)
    assert not closing.done()
    assert environment.temporary_path.exists()
    release.set()
    with pytest.raises(RuntimeError, match="closed during spawn"):
        await task
    await closing
    process.signal.assert_called_with(kill=True)
    assert not environment.temporary_path.exists()


async def test_repeated_close_cancellation_waits_for_cleanup(tmp_path: Path) -> None:

    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    stopping, release = asyncio.Event(), asyncio.Event()

    async def wait() -> int:
        stopping.set()
        await release.wait()
        return -9

    process = Mock()
    process.wait = AsyncMock(side_effect=wait)
    environment._process_manager._processes.add(process)
    closing = asyncio.create_task(environment.close())
    await stopping.wait()
    for _ in range(2):
        closing.cancel()
        await asyncio.sleep(0)
    assert not closing.done()
    assert environment.temporary_path.exists()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    process.signal.assert_called_with(kill=True)
    assert not environment.temporary_path.exists()
    await environment.close()


async def test_file_cancellation_kills_and_drains_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    started, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    process = Mock()

    async def communicate(content: bytes | None = None) -> tuple[bytes, bytes]:
        if content is not None:
            started.set()
            await asyncio.Event().wait()
        stopping.set()
        await release.wait()
        return b"", b""

    async def spawn(*args: object, **kwargs: object) -> LocalProcess:
        return cast(LocalProcess, process)

    process.process.communicate = AsyncMock(side_effect=communicate)
    monkeypatch.setattr(environment, "spawn", spawn)
    task = asyncio.create_task(environment.write_text("example", "content"))
    try:
        await started.wait()
        task.cancel()
        await stopping.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        process.signal.assert_called_once_with(kill=True)
    finally:
        release.set()
        await environment.close()


async def test_code_cancellation_does_not_interrupt_runner_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    started, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    finished = asyncio.Event()

    async def run_code(*args: object, **kwargs: object) -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            stopping.set()
            await release.wait()
            finished.set()

    monkeypatch.setattr("bub.builtin.environment.LocalEnvironment.run_code", run_code)
    task = asyncio.create_task(
        environment.run_code("pass", tools=[], call_tool=run_code, write=lambda _: None)
    )
    try:
        await started.wait()
        task.cancel()
        await stopping.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    finally:
        release.set()
        await environment.close()


async def test_cleanup_wait_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    monkeypatch.setattr("bub_overalls.process._STOP_TIMEOUT_SECONDS", 0.01)
    process = Mock()
    process.wait = AsyncMock(side_effect=asyncio.Event().wait)
    environment._process_manager._processes.add(process)
    async with asyncio.timeout(1):
        await environment.close()
    process.signal.assert_called_with(kill=True)
    assert not environment.temporary_path.exists()
    assert "Timed out waiting for sandbox process cleanup." in caplog.messages


def test_hard_kill_is_idempotent_across_cleanup_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_process = Mock()
    monkeypatch.setattr(LocalProcess, "signal", signal_process)
    process = OverallsProcess(cast(asyncio.subprocess.Process, Mock()))
    process.signal(kill=False)
    process.signal(kill=True)
    process.signal(kill=True)
    process.signal(kill=False)
    assert signal_process.call_args_list == [call(kill=False), call(kill=True)]


async def test_normal_close_allows_process_group_to_exit(tmp_path: Path) -> None:
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    process = Mock()
    process.is_running.side_effect = [True, False]
    process.wait = AsyncMock(return_value=0)
    environment._process_manager._processes.add(process)
    await environment.close()
    process.signal.assert_called_once_with(kill=False)
    process.wait.assert_awaited_once()
    assert not environment.temporary_path.exists()


async def test_normal_close_escalates_unresponsive_groups_together(
    tmp_path: Path,
) -> None:
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    processes = [Mock(), Mock()]
    for process in processes:
        process.is_running.return_value = True
        process.wait = AsyncMock(return_value=-9)
        environment._process_manager._processes.add(process)
    await environment.close()
    for process in processes:
        assert process.signal.call_args_list == [call(kill=False), call(kill=True)]
        process.wait.assert_awaited_once()


async def test_cancelling_graceful_close_forces_immediate_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    monkeypatch.setattr("bub_overalls.process._TERMINATE_TIMEOUT_SECONDS", 60)
    checking = asyncio.Event()
    process = Mock()

    def is_running() -> bool:
        checking.set()
        return True

    process.is_running.side_effect = is_running
    process.wait = AsyncMock(return_value=-9)
    environment._process_manager._processes.add(process)
    closing = asyncio.create_task(environment.close())
    await checking.wait()
    closing.cancel()
    async with asyncio.timeout(1):
        with pytest.raises(asyncio.CancelledError):
            await closing
    process.signal.assert_called_with(kill=True)
    assert not environment.temporary_path.exists()


async def test_signal_failure_does_not_skip_other_groups_or_files(
    tmp_path: Path,
) -> None:
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    denied, other = Mock(), Mock()
    for process in (denied, other):
        process.wait = AsyncMock(return_value=-9)
        environment._process_manager._processes.add(process)
    denied.pid = 12345
    denied.signal.side_effect = PermissionError("signal denied")
    with pytest.raises(
        ExceptionGroup, match="Failed to stop sandbox process groups"
    ) as error:
        await environment.close()
    other.signal.assert_called_with(kill=True)
    other.wait.assert_awaited_once()
    assert all(isinstance(item, PermissionError) for item in error.value.exceptions)
    assert not environment.temporary_path.exists()


async def test_cancelling_close_kills_registered_groups_during_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    killed = asyncio.Event()
    existing = Mock()
    existing.wait = AsyncMock(return_value=-9)
    environment._process_manager._processes.add(existing)

    def signal_existing(*, kill: bool) -> None:
        if kill:
            killed.set()

    existing.signal.side_effect = signal_existing
    child = Mock()
    child.wait = AsyncMock(return_value=-9)
    child.process.communicate = AsyncMock(return_value=(b"", b""))
    monkeypatch.setattr("bub_overalls.process.OverallsProcess", lambda _: child)

    async def delayed_spawn(
        *args: object, **kwargs: object
    ) -> asyncio.subprocess.Process:
        started.set()
        await release.wait()
        return cast(asyncio.subprocess.Process, Mock())

    monkeypatch.setattr("asyncio.create_subprocess_exec", delayed_spawn)
    spawning = asyncio.create_task(environment.spawn(["true"]))
    await started.wait()
    closing = asyncio.create_task(environment.close())
    await asyncio.sleep(0)
    closing.cancel()
    try:
        async with asyncio.timeout(1):
            await killed.wait()
        assert not closing.done()
    finally:
        release.set()
        with pytest.raises(RuntimeError, match="closed during spawn"):
            await spawning
        with pytest.raises(asyncio.CancelledError):
            await closing
    child.signal.assert_called_with(kill=True)
    assert not environment.temporary_path.exists()


async def test_wait_failure_does_not_skip_waiting_for_other_groups(
    tmp_path: Path,
) -> None:
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    failed, other = Mock(), Mock()
    for process in (failed, other):
        process.is_running.return_value = False
        environment._process_manager._processes.add(process)
    failed.wait = AsyncMock(side_effect=OSError("wait failed"))
    finished = asyncio.Event()

    async def wait_for_other() -> int:
        await asyncio.sleep(0)
        finished.set()
        return 0

    other.wait = AsyncMock(side_effect=wait_for_other)
    with pytest.raises(ExceptionGroup, match="Failed to stop sandbox process groups"):
        await environment.close()
    assert finished.is_set()
    assert not environment.temporary_path.exists()


@pytest.mark.parametrize("git_access", [False, True])
async def test_unsupported_git_layout_only_blocks_git_write_mode(
    tmp_path: Path, git_access: bool
) -> None:
    (tmp_path / ".git").write_text("unsupported layout")
    if git_access:
        with pytest.raises(ValueError, match="cannot resolve Git metadata"):
            OverallsEnvironment(OverallsConfig(git_access=True), tmp_path)
    else:
        environment = OverallsEnvironment(OverallsConfig(), tmp_path)
        try:
            assert environment.git_paths is None
        finally:
            await environment.close()


@pytest.mark.parametrize("rejected_root", ["workspace", "write_grant", "temporary"])
def test_profile_rejection_cleans_up_temporary_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rejected_root: str
) -> None:
    workspace = tmp_path / (
        'unsupported"workspace' if rejected_root == "workspace" else "workspace"
    )
    workspace.mkdir()
    grant = tmp_path / 'unsupported"grant'
    grant.mkdir()
    config = OverallsConfig(
        write_paths=(grant,) if rejected_root == "write_grant" else ()
    )
    prefix = 'unsupported"session-' if rejected_root == "temporary" else "session-"
    # Keep the owner alive so garbage collection cannot explain the removal.
    with tempfile.TemporaryDirectory(prefix=prefix, dir=tmp_path) as temporary:
        owner = tempfile.TemporaryDirectory(prefix=prefix, dir=temporary)
        temporary_path = Path(owner.name)
        assert temporary_path.is_dir()
        monkeypatch.setattr(
            "bub_overalls.environment.tempfile.TemporaryDirectory",
            Mock(return_value=owner),
        )
        try:
            with pytest.raises(ValueError, match="unsupported character in write root"):
                OverallsEnvironment(config, workspace)
            assert not temporary_path.exists()
        finally:
            owner.cleanup()
