"""Diagnostics and command execution without an LLM."""

import argparse
import asyncio
import contextlib
import io
import logging
import math
import os
import signal
import sys
import tempfile
from collections.abc import Awaitable, Iterator, Sequence
from pathlib import Path
from typing import BinaryIO

from bub_overalls.config import OverallsConfig
from bub_overalls.environment import OverallsEnvironment

logger = logging.getLogger(__name__)


async def check_sandbox(network_access: bool | None = None) -> int:
    """Verify macOS accepts the profile and starts a sandboxed process."""
    config = OverallsConfig.from_env(network_access=network_access)
    with tempfile.TemporaryDirectory(
        prefix="bub-overalls-check-"
    ) as temporary_directory:
        async with OverallsEnvironment(config, Path(temporary_directory)):
            network_status = "enabled" if config.network_access else "disabled"
            logger.info(
                "Seatbelt preflight passed (macOS; network %s).", network_status
            )
    return 0


@contextlib.contextmanager
def _nonblocking_output() -> Iterator[tuple[BinaryIO, BinaryIO]]:
    destinations = (sys.stdout.buffer, sys.stderr.buffer)
    # Record both states before changing either: stdout/stderr may share an open
    # file description (2>&1). A duplicate descriptor shares its blocking flag.
    blocking = {
        destination.fileno(): os.get_blocking(destination.fileno())
        for destination in destinations
        if not isinstance(destination, io.BytesIO)
    }
    try:
        for descriptor in blocking:
            os.set_blocking(descriptor, False)
        yield destinations
    finally:
        for descriptor, was_blocking in blocking.items():
            os.set_blocking(descriptor, was_blocking)


async def _wait_for_output(descriptor: int) -> None:
    loop = asyncio.get_running_loop()
    writable = loop.create_future()

    def ready() -> None:
        if not writable.done():
            writable.set_result(None)

    loop.add_writer(descriptor, ready)
    try:
        await writable
    finally:
        loop.remove_writer(descriptor)


async def _forward_stream(stream: asyncio.StreamReader, destination: BinaryIO) -> None:
    while chunk := await stream.read(64 * 1024):
        if isinstance(destination, io.BytesIO):
            # In-memory capture has no descriptor and cannot block on a consumer.
            destination.write(chunk)
            continue
        descriptor = destination.fileno()
        remaining = memoryview(chunk)
        while remaining:
            try:
                written = os.write(descriptor, remaining)
            except BlockingIOError:
                await _wait_for_output(descriptor)
            else:
                remaining = remaining[written:]


async def run_command(
    command: Sequence[str],
    workspace: Path,
    timeout: float,
    network_access: bool | None = None,
) -> int:
    with _nonblocking_output() as (stdout, stderr):
        async with OverallsEnvironment(
            OverallsConfig.from_env(network_access=network_access), workspace
        ) as environment:
            async with asyncio.timeout(timeout):
                process = await environment.spawn(command)
                process.close_stdin()
                # A failed output sink must also cancel the sibling reader.
                # All forwarding stops before descriptor flags are restored.
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(_forward_stream(process.stdout, stdout))
                    tasks.create_task(_forward_stream(process.stderr, stderr))
                    waiting = tasks.create_task(process.wait())
                returncode = waiting.result()
                return returncode if returncode >= 0 else 128 - returncode


class _OverallsLogHandler(logging.Handler):
    """Keep standalone diagnostics best-effort when stderr's consumer stalls."""

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record) + "\n"
        if isinstance(sys.stderr, io.StringIO) or isinstance(
            sys.stderr.buffer, io.BytesIO
        ):
            sys.stderr.write(message)
            sys.stderr.flush()
            return
        descriptor = sys.stderr.fileno()
        was_blocking = os.get_blocking(descriptor)
        try:
            os.set_blocking(descriptor, False)
            os.write(
                descriptor,
                message.encode(
                    sys.stderr.encoding or "utf-8", errors="backslashreplace"
                ),
            )
        except (BlockingIOError, BrokenPipeError):
            # Never leave buffered diagnostics that could block interpreter exit.
            pass
        finally:
            os.set_blocking(descriptor, was_blocking)


def _parse_timeout(value: str) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive finite number")
    return timeout


async def _run_with_signals(command: Awaitable[int]) -> int:
    """Turn CLI termination signals into cancellation so async cleanup can run."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("CLI requires an asyncio task")
    received_signal: int | None = None
    previous_handlers = {}

    def cancel(signum: int) -> None:
        nonlocal received_signal
        if received_signal is None:
            received_signal = signum
            task.cancel()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            loop.add_signal_handler(signum, cancel, signum)
        try:
            return await command
        except asyncio.CancelledError:
            if received_signal is None:
                raise
            return 128 + received_signal
    finally:
        for signum, previous_handler in previous_handlers.items():
            loop.remove_signal_handler(signum)
            signal.signal(signum, previous_handler)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        handlers=[_OverallsLogHandler()],
    )
    parser = argparse.ArgumentParser(description="Run Bub tools inside macOS Seatbelt.")
    subparsers = parser.add_subparsers(dest="action", required=True)
    doctor_parser = subparsers.add_parser(
        "doctor", help="verify actual sandbox initialization"
    )
    exec_parser = subparsers.add_parser(
        "exec", help="run a command against a live workspace"
    )
    exec_parser.add_argument("--workspace", type=Path, default=Path("."))
    exec_parser.add_argument("--timeout", type=_parse_timeout, default=60.0)
    exec_parser.add_argument("command", nargs=argparse.REMAINDER)
    for command_parser in (doctor_parser, exec_parser):
        command_parser.add_argument(
            "--network",
            choices=("disabled", "enabled"),
            help="override BUB_OVERALLS_NETWORK_ACCESS (default: disabled)",
        )
    arguments = parser.parse_args(argv)
    network_access = (
        None if arguments.network is None else arguments.network == "enabled"
    )
    if arguments.action == "doctor":
        command_coroutine = check_sandbox(network_access)
    else:
        command = arguments.command
        if command[:1] == ["--"]:
            command = command[1:]
        if not command:
            exec_parser.error("provide a command after --")
        command_coroutine = run_command(
            command, arguments.workspace, arguments.timeout, network_access
        )
    try:
        return asyncio.run(_run_with_signals(command_coroutine))
    except TimeoutError:
        logger.error("Command timed out; process group stopped.")
        return 124
    except (OSError, RuntimeError, ValueError, ExceptionGroup) as error:
        logger.error("%s", error)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
