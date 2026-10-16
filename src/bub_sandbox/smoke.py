"""Behavioral checks shared by the CLI and optional real-VM integration tests."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from bub_sandbox.environment import MicroVMEnvironment
from bub_sandbox.settings import SandboxSettings


async def collect(environment: MicroVMEnvironment, command: str | list[str]) -> tuple[int, bytes, bytes]:
    process = await environment.spawn(command)
    process.close_stdin()
    code, stdout, stderr = await asyncio.gather(process.wait(), process.stdout.read(), process.stderr.read())
    return code, stdout, stderr


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def run_smoke(settings: SandboxSettings) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="bub-workspace-") as directory:
        workspace = Path(directory)
        (workspace / "source.txt").write_text("source from host")
        (workspace / ".env").write_text("DO_NOT_IMPORT=sentinel")
        start = time.perf_counter()
        environment = await MicroVMEnvironment.create(replace(settings, import_workspace=True), workspace)
        ready = time.perf_counter() - start
        try:
            async with asyncio.timeout(45):
                code, stdout, stderr = await collect(
                    environment,
                    [
                        "python3",
                        "-c",
                        "import os,json; print(json.dumps(dict(uid=os.getuid(),"
                        "sysname=os.uname().sysname,machine=os.uname().machine,cwd=os.getcwd())))",
                    ],
                )
                expect(code == 0, f"execution failed: {stderr!r}")
                identity = json.loads(stdout)
                expect(identity["uid"] == 1000 and identity["sysname"] == "Linux", "workload is not unprivileged Linux")
                expect(await environment.read_text("source.txt") == "source from host", "workspace import failed")
                expect((await collect(environment, "test ! -e /workspace/.env"))[0] == 0, "workspace .env was imported")
                await environment.write_text("notes/message.txt", "hello from guest\n")
                expect(
                    await environment.read_text("notes/message.txt") == "hello from guest\n", "file roundtrip failed"
                )
                expect(not (workspace / "notes").exists(), "guest write reached host workspace")
                code, stdout, stderr = await collect(environment, "printf stdout; printf stderr >&2; exit 7")
                expect((code, stdout, stderr) == (7, b"stdout", b"stderr"), "exit code or stream separation failed")
                process = await environment.spawn(["cat"])
                await process.write_stdin(b"stdin\x00roundtrip")
                process.close_stdin()
                result = await asyncio.gather(process.wait(), process.stdout.read(), process.stderr.read())
                expect(result == [0, b"stdin\x00roundtrip", b""], "binary stdin failed")
                output: list[str] = []

                async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
                    expect(name == "echo", "unexpected callback tool")
                    return arguments["value"]

                await environment.run_code(
                    "print(await tools.echo(value='callback works'))",
                    tools=["echo"],
                    call_tool=call_tool,
                    write=output.append,
                )
                expect("callback works" in "".join(output), "code-mode tool callback failed")
                # A detached descendant must remain killable after the parent shell exits.
                process = await environment.spawn("setsid sh -c 'echo $$ > /workspace/child.pid; sleep 60' & sleep 0.1")
                process.close_stdin()
                await process.wait()
                expect(process.is_running(), "detached descendant was lost after shell exit")
                process.signal(kill=True)
                for _ in range(100):
                    if not process.is_running():
                        break
                    await asyncio.sleep(0.02)
                expect(not process.is_running(), "cgroup kill did not stop descendants")
                await asyncio.gather(process.stdout.read(), process.stderr.read())
                # No IP socket proxy should be available through implicit TSI.
                code, _, _ = await collect(
                    environment,
                    ["python3", "-c", "import socket; s=socket.socket(); s.settimeout(1); s.connect(('1.1.1.1',443))"],
                )
                expect(code != 0, "unexpected IP network access")
                return {
                    "status": "passed",
                    "ready_seconds": round(ready, 3),
                    "guest": identity,
                    "checks": [
                        "workspace import",
                        "secret exclusions",
                        "guest-only files",
                        "exit codes",
                        "stdout/stderr",
                        "binary stdin",
                        "code-mode callbacks",
                        "detached descendant cleanup",
                        "no IP egress",
                    ],
                }
        finally:
            await environment.close()
