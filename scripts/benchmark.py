"""Paired native/Seatbelt samples with the same Python binary and fixed workloads."""

import argparse
import asyncio
import json
import math
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

from bub_overalls.config import OverallsConfig
from bub_overalls.environment import OverallsEnvironment

_INTEGER_WORKLOAD_SOURCE = """
import json
import sys
import time

started_at = time.perf_counter()
value = 1
for _ in range(1000000):
    value = (value * 1664525 + 1013904223) & 0xffffffff
sys.stdout.write(json.dumps({"seconds": time.perf_counter() - started_at, "result": value}) + "\\n")
"""


def summarize_timings(samples_ms: list[float]) -> dict[str, float | list[float]]:
    return {
        "median": statistics.median(samples_ms),
        "p95": sorted(samples_ms)[math.ceil(len(samples_ms) * 0.95) - 1],
        "samples": samples_ms,
    }


async def benchmark(sample_count: int) -> dict[str, object]:
    config = OverallsConfig.from_env()
    with tempfile.TemporaryDirectory(prefix="bub-benchmark-") as temporary_directory:
        workspace = Path(temporary_directory).resolve()
        started_at = time.perf_counter()
        async with OverallsEnvironment(config, workspace) as environment:
            startup_seconds = time.perf_counter() - started_at
            native_command_ms: list[float] = []
            sandbox_command_ms: list[float] = []
            native_compute_ms: list[float] = []
            sandbox_compute_ms: list[float] = []

            async def run_native(command: list[str]) -> bytes:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=workspace,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                stdout, stderr = await process.communicate()
                if process.returncode:
                    raise RuntimeError(stderr.decode())
                return stdout

            async def run_sandboxed(command: list[str]) -> bytes:
                process = await environment.spawn(command)
                process.close_stdin()
                returncode, stdout, stderr = await asyncio.gather(
                    process.wait(), process.stdout.read(), process.stderr.read()
                )
                if returncode:
                    raise RuntimeError(stderr.decode())
                return stdout

            # Warm both paths, alternate order to reduce fixed-order bias.
            await run_native(["/usr/bin/true"])
            await run_sandboxed(["/usr/bin/true"])
            for sample_index in range(sample_count):
                runners = [
                    (run_native, native_command_ms),
                    (run_sandboxed, sandbox_command_ms),
                ]
                if sample_index % 2 == 0:
                    runners.reverse()
                for run_command, timings_ms in runners:
                    started_at = time.perf_counter()
                    await run_command(["/usr/bin/true"])
                    timings_ms.append((time.perf_counter() - started_at) * 1000)
            for sample_index in range(5):
                runners = [
                    (run_native, native_compute_ms),
                    (run_sandboxed, sandbox_compute_ms),
                ]
                if sample_index % 2 == 0:
                    runners.reverse()
                for run_command, timings_ms in runners:
                    compute_result: dict[str, float | int] = json.loads(
                        await run_command(
                            [environment.python, "-I", "-c", _INTEGER_WORKLOAD_SOURCE]
                        )
                    )
                    if compute_result["result"] != 366300225:
                        raise RuntimeError(
                            f"unexpected arithmetic result: {compute_result}"
                        )
                    timings_ms.append(compute_result["seconds"] * 1000)
            return {
                "host": {
                    "system": platform.system(),
                    "release": platform.release(),
                    "machine": platform.machine(),
                },
                "python": environment.python,
                "ready_seconds": startup_seconds,
                "command_ms": {
                    "native": summarize_timings(native_command_ms),
                    "seatbelt": summarize_timings(sandbox_command_ms),
                },
                "integer_ms": {
                    "native": summarize_timings(native_compute_ms),
                    "seatbelt": summarize_timings(sandbox_compute_ms),
                },
                "method": "alternating paired runs; same interpreter; compute excludes startup; no LLM/planning/retrieval",
            }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.samples < 2:
        parser.error("--samples must be at least 2")
    report_json = json.dumps(asyncio.run(benchmark(arguments.samples)), indent=2)
    if arguments.output:
        arguments.output.write_text(report_json + "\n")
    sys.stdout.write(report_json + "\n")


if __name__ == "__main__":
    main()
