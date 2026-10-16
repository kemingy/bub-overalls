"""Installed plugins select the sandbox unconditionally; runtime failures propagate."""

from pathlib import Path

from bub import hookimpl

from bub_sandbox.environment import MicroVMEnvironment
from bub_sandbox.settings import SandboxSettings


@hookimpl
async def provide_environment(session_id: str, workspace: Path) -> MicroVMEnvironment:
    del session_id
    return await MicroVMEnvironment.create(SandboxSettings.from_env(), workspace)
