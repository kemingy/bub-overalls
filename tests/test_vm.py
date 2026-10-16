from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bub_sandbox.environment import MicroVMEnvironment
from bub_sandbox.settings import SandboxSettings
from bub_sandbox.smoke import collect, run_smoke

pytestmark = [
    pytest.mark.vm,
    pytest.mark.skipif(os.environ.get("BUB_SANDBOX_VM_TEST") != "1", reason="enable with make test-vm"),
]


async def test_real_vm_execution_and_isolation() -> None:
    report = await run_smoke(SandboxSettings.from_env())
    assert report["status"] == "passed"


async def test_session_disks_do_not_share_writes(tmp_path: Path) -> None:
    settings = replace(SandboxSettings.from_env(), import_workspace=False)
    first = await MicroVMEnvironment.create(settings, tmp_path)
    second = None
    try:
        await first.write_text("private.txt", "session one")
        second = await MicroVMEnvironment.create(settings, tmp_path)
        assert (await collect(second, "test ! -e /workspace/private.txt"))[0] == 0
        assert await first.read_text("private.txt") == "session one"
    finally:
        await first.close()
        if second:
            await second.close()


async def test_code_timeout_kills_guest_runner(tmp_path: Path) -> None:
    environment = await MicroVMEnvironment.create(replace(SandboxSettings.from_env(), import_workspace=False), tmp_path)
    output: list[str] = []

    async def no_tools(name: str, arguments: dict[str, Any]) -> Any:
        raise AssertionError("unexpected callback")

    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.3):
                await environment.run_code(
                    "import os,time; print(os.getpid(), flush=True); time.sleep(60)",
                    tools=[],
                    call_tool=no_tools,
                    write=output.append,
                )
        pid = int("".join(output).strip())
        code, _, _ = await collect(environment, ["python3", "-c", f"import os; os.kill({pid}, 0)"])
        assert code != 0
    finally:
        await environment.close()


async def test_vm_crash_fails_tools_and_removes_private_disk(tmp_path: Path) -> None:
    environment = await MicroVMEnvironment.create(replace(SandboxSettings.from_env(), import_workspace=False), tmp_path)
    assert environment._temporary is not None and environment._vm is not None
    directory = Path(environment._temporary.name)
    process = await environment.spawn(["sleep", "60"])
    process.close_stdin()
    environment._vm.kill()
    try:
        with pytest.raises(ConnectionError):
            async with asyncio.timeout(5):
                await process.stdout.read()
    finally:
        await environment.close()
    assert not directory.exists()
