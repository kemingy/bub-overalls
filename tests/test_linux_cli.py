"""Real Linux CLI checks; these complement the Seatbelt-specific CLI tests."""

import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path

import pytest

from tests.common import assert_processes_stopped, read_pid_pair

pytestmark = [pytest.mark.sandbox, pytest.mark.landlock]


async def test_doctor_reports_landlock() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "bub_overalls.main",
        "doctor",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr
    assert stdout == b""
    assert b"Landlock/seccomp preflight passed (network disabled)." in stderr


async def test_exec_streams_and_returns_real_exit_code(tmp_path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "bub_overalls.main",
        "exec",
        "--workspace",
        str(tmp_path),
        "--timeout",
        "5",
        "--",
        sys.executable,
        "-I",
        "-c",
        "import sys; sys.stdout.write('out'); sys.stderr.write('err'); raise SystemExit(7)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 7, stderr
    assert (stdout, stderr) == (b"out", b"err")


async def test_sigterm_stops_processes_with_stalled_stderr(tmp_path: Path) -> None:
    reader, writer = os.pipe()
    child_source = (
        "import os,subprocess,sys; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
        "Path('pids').write_text(f'{os.getpid()} {child.pid}'); "
        "exec('while True: os.write(2, b\"x\" * 65536)')"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "bub_overalls.main",
        "exec",
        "--workspace",
        str(tmp_path),
        "--timeout",
        "30",
        "--",
        sys.executable,
        "-I",
        "-c",
        child_source,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=writer,
        start_new_session=True,
    )
    os.close(writer)
    pids: list[int] = []
    try:
        async with asyncio.timeout(60):
            pids = await read_pid_pair(tmp_path / "pids")
            # Let the stderr pipe fill; deliberately never read it.
            await asyncio.sleep(0.1)
            process.send_signal(signal.SIGTERM)
            assert await process.wait() == 128 + signal.SIGTERM
            await assert_processes_stopped(pids)
    finally:
        os.close(reader)
        if pids:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pids[0], signal.SIGKILL)
        if process.returncode is None:
            process.kill()
        await process.wait()
