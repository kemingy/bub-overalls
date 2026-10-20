import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from bub_overalls import main as cli
from tests.common import assert_processes_stopped, read_pid_pair


def test_imports_preserve_host_logging() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            r"""
import io
import logging

output = io.StringIO()
handler = logging.StreamHandler(output)
handler.setFormatter(logging.Formatter("host: %(name)s: %(message)s"))
root = logging.getLogger()
root.handlers = [handler]
root.setLevel(logging.WARNING)
logging.getLogger("host").warning("before")
import bub_overalls.plugin
from bub_overalls import main

assert root.handlers == [handler]
assert root.level == logging.WARNING
main.logger.info("filtered")
main.logger.warning("after")
assert output.getvalue() == "host: host: before\nhost: bub_overalls.main: after\n"
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize("action", [[], ["exec"], ["exec", "--"]])
def test_requires_command(action: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(action)
    assert error.value.code == 2


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_rejects_invalid_timeout(value: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["exec", "--timeout", value, "--", "true"])
    assert error.value.code == 2


def test_exec_preserves_command_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_command(
        command: list[str],
        workspace: Path,
        timeout: float,
        network_access: bool | None = None,
    ) -> int:
        assert command == ["/bin/bash", "-c", "exit 7"]
        assert workspace == Path("/project")
        assert timeout == 2
        return 7

    monkeypatch.setattr(cli, "run_command", run_command)
    assert (
        cli.main(
            [
                "exec",
                "--workspace",
                "/project",
                "--timeout",
                "2",
                "--",
                "/bin/bash",
                "-c",
                "exit 7",
            ]
        )
        == 7
    )


def test_timeout_returns_124(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def run_command(
        command: list[str],
        workspace: Path,
        timeout: float,
        network_access: bool | None = None,
    ) -> int:
        raise TimeoutError

    monkeypatch.setattr(cli, "run_command", run_command)
    assert cli.main(["exec", "--", "sleep", "60"]) == 124
    assert caplog.record_tuples == [
        (cli.__name__, logging.ERROR, "Command timed out; process group stopped.")
    ]


def test_initialization_error_is_concise(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def check_sandbox(network_access: bool | None = None) -> int:
        raise RuntimeError("macOS only")

    monkeypatch.setattr(cli, "check_sandbox", check_sandbox)
    assert cli.main(["doctor"]) == 1
    assert caplog.record_tuples == [(cli.__name__, logging.ERROR, "macOS only")]


def test_cli_configures_info_on_stderr() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from bub_overalls import main

async def check_sandbox(network_access=None):
    main.logger.debug("hidden")
    main.logger.info("ready")
    return 0

main.check_sandbox = check_sandbox
raise SystemExit(main.main(["doctor"]))
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == "INFO: ready\n"


@pytest.mark.sandbox
async def test_doctor_initializes_and_closes_sandbox(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=cli.__name__)
    assert await cli.check_sandbox() == 0
    assert caplog.messages == ["Seatbelt preflight passed (network disabled)."]


@pytest.mark.sandbox
async def test_execute_streams_and_returns_real_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    returncode = await cli.run_command(
        ["/bin/sh", "-c", "printf out; printf err >&2; exit 7"], tmp_path, 5
    )
    assert returncode == 7
    captured = capsys.readouterr()
    assert captured.out == "out"
    assert captured.err == "err"


@pytest.mark.parametrize("mode, expected", [("enabled", True), ("disabled", False)])
@pytest.mark.parametrize("action", ["doctor", "exec"])
def test_cli_network_override(
    mode: str, expected: bool, action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check_sandbox(network_access: bool | None = None) -> int:
        assert network_access is expected
        return 0

    async def run_command(
        command: list[str],
        workspace: Path,
        timeout: float,
        network_access: bool | None = None,
    ) -> int:
        assert network_access is expected
        assert command == ["true"]
        return 0

    monkeypatch.setattr(cli, "check_sandbox", check_sandbox)
    monkeypatch.setattr(cli, "run_command", run_command)
    arguments = [action, "--network", mode]
    if action == "exec":
        arguments.extend(["--", "true"])
    assert cli.main(arguments) == 0


@pytest.mark.parametrize("action", ["doctor", "exec"])
def test_cli_rejects_unknown_network_mode(action: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main([action, "--network", "typo"])
    assert error.value.code == 2


@pytest.mark.sandbox
async def test_doctor_reports_network_configuration(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=cli.__name__)
    monkeypatch.setenv("BUB_OVERALLS_NETWORK_ACCESS", "true")
    assert await cli.check_sandbox() == 0
    assert "network enabled" in caplog.messages[-1]
    assert await cli.check_sandbox(False) == 0
    assert "network disabled" in caplog.messages[-1]


@pytest.mark.sandbox
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, None])
async def test_cli_cleans_process_group_on_signal_or_timeout(
    tmp_path: Path, signum: signal.Signals | None
) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "bub_overalls.main",
        "exec",
        "--workspace",
        str(tmp_path),
        "--timeout",
        "5" if signum is None else "30",
        "--",
        sys.executable,
        "-I",
        "-c",
        "import os,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
        "print(os.getpid(),child.pid,flush=True); time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    pids: list[int] = []
    try:
        async with asyncio.timeout(60):
            assert process.stdout is not None
            line = await process.stdout.readline()
            if not line.split():
                assert process.stderr is not None
                raise AssertionError(
                    f"missing pid line (exit {process.returncode}): "
                    f"{await process.stderr.read()!r}"
                )
            pids = [int(value) for value in line.split()]
            assert len(pids) == 2
            if signum is not None:
                process.send_signal(signum)
            stdout, stderr = await process.communicate()
            assert process.returncode == (124 if signum is None else 128 + signum), (
                stderr
            )
            assert stdout == b""
            if signum is None:
                assert b"Command timed out; process group stopped." in stderr
            else:
                assert stderr == b""
            await assert_processes_stopped(pids)
    finally:
        if pids:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pids[0], signal.SIGKILL)
        if process.returncode is None:
            process.kill()
        await process.communicate()


@pytest.mark.parametrize("cancel", [False, True])
async def test_stalled_output_can_be_cancelled_and_restores_blocking_flags(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    reader, writer = os.pipe()
    try:
        # Fill the real pipe before starting; no reader will make it writable.
        os.set_blocking(writer, False)
        with contextlib.suppress(BlockingIOError):
            while True:
                os.write(writer, b"x" * 65536)
        os.set_blocking(writer, True)
        with os.fdopen(os.dup(writer), "wb", buffering=0) as destination:
            monkeypatch.setattr(sys, "stdout", Mock(buffer=destination))
            # A merged stderr shares stdout's blocking state.
            monkeypatch.setattr(sys, "stderr", Mock(buffer=destination))
            stream = asyncio.StreamReader()
            stream.feed_data(b"pending output")
            stream.feed_eof()
            with cli._nonblocking_output() as (stdout, _):
                assert not os.get_blocking(writer)
                task = asyncio.create_task(cli._forward_stream(stream, stdout))
                await asyncio.sleep(0)
                assert not task.done()
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(TimeoutError):
                        async with asyncio.timeout(0.02):
                            await task
            assert os.get_blocking(writer)
    finally:
        os.close(reader)
        os.close(writer)


async def test_forwarding_preserves_bytes_through_partial_pipe_writes() -> None:
    reader, writer = os.pipe()
    os.set_blocking(reader, False)
    os.set_blocking(writer, False)
    expected = bytes(range(256)) * 1024
    received = bytearray()
    stream = asyncio.StreamReader()
    stream.feed_data(expected)
    stream.feed_eof()

    async def consume() -> None:
        while len(received) < len(expected):
            try:
                received.extend(os.read(reader, 8192))
            except BlockingIOError:
                await asyncio.sleep(0.001)

    try:
        with os.fdopen(os.dup(writer), "wb", buffering=0) as destination:
            async with asyncio.timeout(5):
                await asyncio.gather(
                    cli._forward_stream(stream, destination), consume()
                )
        assert received == expected
    finally:
        os.close(reader)
        os.close(writer)


@pytest.mark.sandbox
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, None])
@pytest.mark.parametrize("descriptor", [1, 2])
async def test_cli_stops_processes_with_stalled_output_consumer(
    tmp_path: Path, signum: signal.Signals | None, descriptor: int
) -> None:
    reader, writer = os.pipe()
    child_source = (
        "import os,subprocess,sys; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
        "Path('pids').write_text(f'{os.getpid()} {child.pid}'); "
        f"exec('while True: os.write({descriptor}, b\"x\" * 65536)')"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "bub_overalls.main",
        "exec",
        "--workspace",
        str(tmp_path),
        "--timeout",
        "5" if signum is None else "30",
        "--",
        sys.executable,
        "-I",
        "-c",
        child_source,
        stdout=writer if descriptor == 1 else asyncio.subprocess.DEVNULL,
        stderr=writer if descriptor == 2 else asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    os.close(writer)
    pids: list[int] = []
    try:
        async with asyncio.timeout(60):
            pids = await read_pid_pair(tmp_path / "pids")
            # Let the output pipe fill; deliberately never read it.
            await asyncio.sleep(0.1)
            if signum is not None:
                process.send_signal(signum)
            assert await process.wait() == (124 if signum is None else 128 + signum)
            await assert_processes_stopped(pids)
    finally:
        os.close(reader)
        if pids:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pids[0], signal.SIGKILL)
        if process.returncode is None:
            process.kill()
        await process.wait()
