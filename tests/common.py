import asyncio

from bub_overalls import OverallsEnvironment


async def run_command(
    environment: OverallsEnvironment, command: str | list[str]
) -> tuple[int, bytes, bytes]:
    process = await environment.spawn(command)
    process.close_stdin()
    returncode, stdout, stderr = await asyncio.gather(
        process.wait(), process.stdout.read(), process.stderr.read()
    )
    return returncode, stdout, stderr


async def assert_processes_stopped(pids: list[int]) -> None:
    """Treat an exited zombie awaiting its new parent's reap as stopped."""
    async with asyncio.timeout(5):
        while True:
            process = await asyncio.create_subprocess_exec(
                "/bin/ps",
                "-o",
                "stat=",
                "-p",
                ",".join(str(pid) for pid in pids),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            if all(state.startswith(b"Z") for state in stdout.split()):
                return
            await asyncio.sleep(0.01)
