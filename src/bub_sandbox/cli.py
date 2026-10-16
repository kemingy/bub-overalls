"""VM diagnostics without an LLM or provider credentials."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import typer

from bub_sandbox.environment import MicroVMEnvironment
from bub_sandbox.settings import SandboxSettings

app = typer.Typer(no_args_is_help=True)


@app.command()
def doctor() -> None:
    """Validate host, image metadata, helper, and dynamically linked libkrun."""
    settings = SandboxSettings.from_env()
    settings.validate()
    subprocess.run([str(settings.helper), "--check"], check=True)
    typer.echo(f"image: {settings.image}")
    typer.echo("Preflight passed; use smoke to verify an actual guest boot.")


@app.command()
def smoke() -> None:
    """Boot a real VM and check execution, files, streams, code callbacks and cancellation."""
    from bub_sandbox.smoke import run_smoke

    typer.echo(json.dumps(asyncio.run(run_smoke(SandboxSettings.from_env())), indent=2))


@app.command(name="exec", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def execute(ctx: typer.Context, workspace: Path = Path("."), timeout: float = 60) -> None:
    """Run argv in a fresh session: bub-sandbox exec -- python3 -c 'print(1)'."""
    if not ctx.args:
        raise typer.BadParameter("provide a command after --")
    if timeout <= 0:
        raise typer.BadParameter("timeout must be positive")

    async def run() -> int:
        environment = await MicroVMEnvironment.create(SandboxSettings.from_env(), workspace.resolve())
        try:
            async with asyncio.timeout(timeout):
                process = await environment.spawn(ctx.args)
                process.close_stdin()

                async def forward(stream: asyncio.StreamReader, error: bool = False) -> None:
                    target = sys.stderr.buffer if error else sys.stdout.buffer
                    while data := await stream.read(64 * 1024):
                        target.write(data)
                        target.flush()

                _, _, code = await asyncio.gather(
                    forward(process.stdout), forward(process.stderr, True), process.wait()
                )
                return int(code)
        finally:
            await environment.close()

    try:
        code = asyncio.run(run())
    except TimeoutError:
        typer.echo("command timed out; VM destroyed", err=True)
        code = 124
    raise typer.Exit(code if code >= 0 else 128 - code)
