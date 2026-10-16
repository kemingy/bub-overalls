"""Deterministic execution ablations; no LLM, planning, or memory retrieval variance."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from bub_sandbox.environment import MicroVMEnvironment
from bub_sandbox.settings import SandboxSettings
from bub_sandbox.smoke import collect

CPU_CODE = (
    "import hashlib,time,json,sys,ssl; start=time.perf_counter(); "
    "hashlib.pbkdf2_hmac('sha256',b'bub',b'sandbox',600000); "
    "print(json.dumps(dict(seconds=time.perf_counter()-start,python=sys.version,openssl=ssl.OPENSSL_VERSION)))"
)
INTEGER_CODE = (
    "import time,json\nstart=time.perf_counter()\nx=1\n"
    "for i in range(1000000):\n x=(x*1664525+1013904223)&0xffffffff\n"
    "print(json.dumps(dict(seconds=time.perf_counter()-start,result=x)))"
)


def summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "median_ms": round(statistics.median(values) * 1000, 3),
        "p95_ms": round(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)] * 1000, 3),
    }


async def benchmark(samples: int) -> dict[str, Any]:
    settings = replace(SandboxSettings.from_env(), import_workspace=False)
    native = [json.loads(subprocess.check_output([sys.executable, "-c", CPU_CODE])) for _ in range(samples)]
    native_integer = [json.loads(subprocess.check_output([sys.executable, "-c", INTEGER_CODE])) for _ in range(samples)]
    results = []
    with tempfile.TemporaryDirectory(prefix="bub-bench-") as directory:
        for cpus, memory in [(1, 512), (2, 512), (2, 1024)]:
            start = time.perf_counter()
            environment = await MicroVMEnvironment.create(
                replace(settings, cpus=cpus, memory_mib=memory), Path(directory)
            )
            ready = time.perf_counter() - start
            try:
                cpu = []
                integer = []
                warm = []
                for _ in range(samples):
                    code, stdout, stderr = await collect(environment, ["python3", "-c", CPU_CODE])
                    if code:
                        raise RuntimeError(stderr.decode())
                    cpu.append(json.loads(stdout))
                    code, stdout, stderr = await collect(environment, ["python3", "-c", INTEGER_CODE])
                    if code:
                        raise RuntimeError(stderr.decode())
                    integer.append(json.loads(stdout))
                for _ in range(samples * 4):
                    start = time.perf_counter()
                    await collect(environment, ["true"])
                    warm.append(time.perf_counter() - start)
                start = time.perf_counter()
                await asyncio.gather(*(collect(environment, ["python3", "-c", CPU_CODE]) for _ in range(4)))
                parallel = time.perf_counter() - start
                results.append(
                    {
                        "cpus": cpus,
                        "memory_mib": memory,
                        "cold_ready_seconds": round(ready, 3),
                        "guest_compute": summary([sample["seconds"] for sample in cpu]),
                        "guest_compute_samples": cpu,
                        "guest_integer": summary([sample["seconds"] for sample in integer]),
                        "warm_command_roundtrip": summary(warm),
                        "warm_command_seconds": warm,
                        "four_parallel_compute_seconds": round(parallel, 3),
                    }
                )
            finally:
                await environment.close()
        # Natural lifecycle alternative: fresh VM per command instead of a session VM.
        fresh = []
        for _ in range(samples):
            start = time.perf_counter()
            environment = await MicroVMEnvironment.create(settings, Path(directory))
            try:
                await collect(environment, ["true"])
            finally:
                await environment.close()
            fresh.append(time.perf_counter() - start)
    return {
        "host": platform.platform(),
        "machine": platform.machine(),
        "samples": samples,
        "native_compute": summary([sample["seconds"] for sample in native]),
        "native_compute_samples": native,
        "native_integer": summary([sample["seconds"] for sample in native_integer]),
        "configurations": results,
        "fresh_vm_per_command": summary(fresh),
        "limitations": [
            "Host and guest OS, Python patch level, and OpenSSL builds can differ; "
            "compute ratios are not pure VMM overhead.",
            "One host, small sample count, warm image cache; no performance guarantee or cross-platform claim.",
            "Model, planning and retrieval are bypassed; results isolate execution plumbing, not agent quality.",
            "Workspace sharing and alternate VMM comparisons remain future work.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--output", type=Path, default=Path("build/benchmark.json"))
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("use at least two samples")
    result = asyncio.run(benchmark(args.samples))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
