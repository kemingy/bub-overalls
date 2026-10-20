"""Real Linux enforcement tests; no emulation or mocked syscall success."""

import ctypes
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bub_overalls import OverallsConfig, OverallsEnvironment
from tests.common import run_command

pytestmark = [pytest.mark.sandbox, pytest.mark.landlock]
_PROGRAMS = Path(__file__).parent / "fixtures" / "linux"


def _build_probe_command(
    environment: OverallsEnvironment, name: str, *arguments: str
) -> list[str]:
    # Stage the script inside existing grants instead of allowing the test tree.
    assert environment.workspace is not None
    program = Path(environment.workspace) / name
    shutil.copyfile(_PROGRAMS / name, program)
    return [environment.python, "-I", str(program), *arguments]


@pytest.mark.parametrize("network_access", [False, True])
async def test_linux_bub_code_runner_and_callbacks(
    tmp_path: Path, network_access: bool
) -> None:
    output: list[str] = []

    async def callback(name: str, arguments: dict[str, Any]) -> Any:
        assert name == "echo"
        return arguments["text"]

    async with OverallsEnvironment(
        OverallsConfig(network_access=network_access), tmp_path
    ) as environment:
        await environment.run_code(
            "print(await tools.echo(text='works'))",
            tools=["echo"],
            call_tool=callback,
            write=output.append,
        )
    assert "works" in "".join(output)


@pytest.mark.parametrize("network_access", [False, True])
async def test_linux_namespaces_isolate_host_ipc(
    tmp_path: Path, network_access: bool
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    queue = libc.msgget(0, 0o600)  # IPC_PRIVATE
    assert queue >= 0, ctypes.get_errno()
    message = (ctypes.c_long * 2)(1, 42)
    size = ctypes.sizeof(ctypes.c_long)
    host_namespaces = {
        name: os.readlink(f"/proc/self/ns/{name}") for name in ("ipc", "net")
    }
    try:
        assert libc.msgsnd(queue, ctypes.byref(message), size, 0) == 0
        async with OverallsEnvironment(
            OverallsConfig(network_access=network_access), tmp_path
        ) as environment:
            returncode, _, stderr = await run_command(
                environment,
                _build_probe_command(
                    environment,
                    "check_namespaces.py",
                    json.dumps(host_namespaces),
                    str(queue),
                    "enabled" if network_access else "disabled",
                ),
            )
            assert returncode == 0, stderr
        assert libc.msgrcv(queue, ctypes.byref(message), size, 0, 0o4000) == size
        assert message[1] == 42
    finally:
        assert libc.msgctl(queue, 0, None) == 0


@pytest_asyncio.fixture
async def linux_environment(tmp_path: Path) -> AsyncIterator[OverallsEnvironment]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with OverallsEnvironment(OverallsConfig(), workspace) as environment:
        yield environment


async def test_linux_files_and_descendants(
    linux_environment: OverallsEnvironment, tmp_path: Path
) -> None:
    environment = linux_environment
    await environment.write_text("nested/file", "allowed")
    assert await environment.read_text("nested/file") == "allowed"
    secret = tmp_path / "secret"
    secret.write_text("sentinel")
    workspace = Path(environment.workspace or "")
    (workspace / "escape").symlink_to(secret)
    for path in (str(secret), "escape"):
        with pytest.raises(OSError):
            await environment.read_text(path)
        with pytest.raises(OSError):
            await environment.write_text(path, "denied")
    returncode, _, _ = await run_command(
        environment, ["/bin/sh", "-c", f"cat {secret}"]
    )
    assert returncode != 0
    assert secret.read_text() == "sentinel"
    # Names created after this command starts remain a documented gap.
    # The following helper invocation sees the name created by the previous one.
    await environment.write_text("nested/.codex/sentinel", "writable")
    await environment.write_text(".git/sentinel", "writable")
    with pytest.raises(OSError):
        await environment.write_text("nested/.codex/sentinel", "denied next command")


@pytest.mark.parametrize("network_access", [False, True])
@pytest.mark.parametrize(
    "family, host", [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")]
)
@pytest.mark.parametrize("kind", [socket.SOCK_STREAM, socket.SOCK_DGRAM])
async def test_linux_live_ip_operations(
    tmp_path: Path, network_access: bool, family: int, host: str, kind: int
) -> None:
    with socket.socket(family, kind) as listener:
        listener.bind((host, 0))
        if kind == socket.SOCK_STREAM:
            listener.listen()
        address = json.dumps(listener.getsockname())
        async with OverallsEnvironment(
            OverallsConfig(network_access=network_access),
            tmp_path,
        ) as environment:
            returncode, _, stderr = await run_command(
                environment,
                _build_probe_command(
                    environment, "connect_ip.py", str(family), str(kind), address
                ),
            )
            assert returncode == (0 if network_access else 23), stderr
        if network_access:
            listener.settimeout(2)
            if kind == socket.SOCK_STREAM:
                connection, _ = listener.accept()
                with connection:
                    assert connection.recv(1) == b"x"
            else:
                assert listener.recv(1) == b"x"


@pytest.mark.parametrize("network_access", [False, True])
async def test_linux_denies_unix_and_explicit_listeners(
    tmp_path: Path, network_access: bool
) -> None:
    with socket.socket(socket.AF_UNIX) as listener:
        address = str(tmp_path / "host.sock")
        listener.bind(address)
        listener.listen()
        with socket.socket(socket.AF_UNIX) as baseline:
            baseline.connect(address)
        connection, _ = listener.accept()
        connection.close()
        async with OverallsEnvironment(
            OverallsConfig(network_access=network_access),
            tmp_path,
        ) as environment:
            returncode, _, stderr = await run_command(
                environment,
                _build_probe_command(environment, "deny_listeners.py", address),
            )
            assert returncode == 0, stderr


async def test_linux_signal_scope_and_async_runtime(
    linux_environment: OverallsEnvironment,
) -> None:
    environment = linux_environment
    command = _build_probe_command(environment, "check_signals.py", str(os.getpid()))
    returncode, _, stderr = await run_command(environment, command)
    assert returncode == 0, stderr
    process = await environment.spawn([*command, "--wait"])
    process.close_stdin()
    await environment.close()
    assert process.returncode in (-signal.SIGTERM, -signal.SIGKILL)
    assert not environment.temporary_path.exists()


async def test_linux_process_memory_syscalls_are_denied(
    linux_environment: OverallsEnvironment,
) -> None:
    returncode, stdout, stderr = await run_command(
        linux_environment,
        _build_probe_command(linux_environment, "deny_process_vm.py"),
    )
    assert (returncode, stdout, stderr) == (0, b"", b"")


async def test_linux_explicit_read_and_write_roots(tmp_path: Path) -> None:
    workspace, read, write = (tmp_path / name for name in ("work", "read", "write"))
    for path in (workspace, read, write):
        path.mkdir()
    (read / "input").write_text("allowed")
    async with OverallsEnvironment(
        OverallsConfig(read_paths=(read,), write_paths=(write,)),
        workspace,
    ) as environment:
        assert await environment.read_text(str(read / "input")) == "allowed"
        with pytest.raises(OSError):
            await environment.write_text(str(read / "input"), "denied")
        await environment.write_text(str(write / "output"), "allowed")


async def test_linux_replaced_grant_cannot_redirect_next_launcher(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    grant = workspace / "grant"
    grant.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("sentinel")
    async with OverallsEnvironment(
        OverallsConfig(read_paths=(grant,)), workspace
    ) as environment:
        # Simulate a root changing between commands; no command may gain access
        # to the new symlink target while the launcher is still unrestricted.
        grant.rename(workspace / "old-grant")
        grant.symlink_to(outside, target_is_directory=True)
        with pytest.raises(OSError):
            await environment.read_text("grant/secret")
    assert (outside / "secret").read_text() == "sentinel"


@pytest.mark.parametrize("name", [".git", ".ssh", ".codex", "nested/.SSH"])
async def test_linux_existing_protected_paths_are_readonly(
    tmp_path: Path, name: str
) -> None:
    protected = tmp_path / name
    protected.mkdir(parents=True)
    sentinel = protected / "sentinel"
    sentinel.write_text("original")
    async with OverallsEnvironment(
        OverallsConfig(write_paths=(protected,)), tmp_path
    ) as environment:
        assert await environment.read_text(str(sentinel)) == "original"
        await environment.write_text("ordinary", "allowed")
        with pytest.raises(OSError):
            await environment.write_text(str(sentinel), "denied")
        returncode, _, stderr = await run_command(
            environment,
            _build_probe_command(environment, "check_readonly.py", str(sentinel)),
        )
        assert returncode == 0, stderr
        # The read-only mount exists only in the command's namespace.
        assert protected.stat().st_mode & 0o200 != 0
        sentinel.write_text("host still writable")
    assert sentinel.read_text() == "host still writable"


async def test_linux_new_names_are_protected_on_the_next_command(
    linux_environment: OverallsEnvironment,
) -> None:
    environment = linux_environment
    returncode, _, stderr = await run_command(
        environment, _build_probe_command(environment, "create_protected_name.py")
    )
    assert returncode == 0, stderr
    assert await environment.read_text(".ssh/key") == "new"
    with pytest.raises(OSError):
        await environment.write_text(".ssh/key", "denied")


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
async def test_linux_protected_aliases_fail_before_target(
    tmp_path: Path, alias: str
) -> None:
    protected = tmp_path / ".ssh"
    protected.mkdir()
    ordinary = tmp_path / "ordinary"
    ordinary.write_text("sentinel")
    if alias == "symlink":
        (protected / "key").symlink_to(ordinary)
    else:
        os.link(ordinary, protected / "key")
    with pytest.raises(
        RuntimeError,
        match="preflight failed",
    ):
        await OverallsEnvironment.create(OverallsConfig(), tmp_path)
    assert ordinary.read_text() == "sentinel"


async def test_linux_linked_worktree_metadata_stays_readonly(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    workspace = tmp_path / "worktree"
    for command in (
        ["git", "init", str(repository)],
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
        ["git", "-C", str(repository), "worktree", "add", str(workspace)],
    ):
        subprocess.run(command, check=True, capture_output=True)
    async with OverallsEnvironment(
        OverallsConfig(write_paths=(repository,)), workspace
    ) as environment:
        assert environment.git_paths is not None
        await environment.write_text("ordinary", "allowed")
        for path in (
            workspace / ".git",
            environment.git_paths.directory / "HEAD",
            environment.git_paths.common_directory / "config",
            environment.git_paths.common_directory / "hooks" / "new",
        ):
            with pytest.raises(OSError):
                await environment.write_text(str(path), "denied")


async def test_linux_mount_changes_and_setup_capabilities_are_unavailable(
    linux_environment: OverallsEnvironment,
) -> None:
    environment = linux_environment
    returncode, _, stderr = await run_command(
        environment, _build_probe_command(environment, "check_capabilities.py")
    )
    assert returncode == 0, stderr


@pytest.mark.parametrize("layout", ["nested-mount", "bind-alias"])
def test_linux_mount_topology(tmp_path: Path, layout: str) -> None:
    protected = tmp_path / ".ssh"
    protected.mkdir()
    target = protected / "nested" if layout == "nested-mount" else tmp_path / "alias"
    target.mkdir()
    # This trusted child must prepare mounts before Overalls restricts execution.
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(_PROGRAMS / "check_mount_topology.py"),
            str(tmp_path),
            layout,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
