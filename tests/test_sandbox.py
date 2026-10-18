"""Opt-in integration tests: must execute outside another restrictive sandbox."""

import asyncio
import errno
import json
import signal
import socket
from pathlib import Path
from typing import Any

import pytest
from bub.builtin.environment import LocalProcess

from bub_overalls.config import OverallsConfig
from bub_overalls.environment import OverallsEnvironment
from tests.common import assert_processes_stopped, run_command

pytestmark = pytest.mark.sandbox


async def test_native_streams_stdin_and_live_files(
    environment: OverallsEnvironment,
) -> None:
    assert await run_command(environment, "printf out; printf err >&2; exit 7") == (
        7,
        b"out",
        b"err",
    )
    process = await environment.spawn(["/bin/cat"])
    await process.write_stdin(b"binary\x00input")
    process.close_stdin()
    returncode, stdout, stderr = await asyncio.gather(
        process.wait(), process.stdout.read(), process.stderr.read()
    )
    assert (returncode, stdout, stderr) == (0, b"binary\x00input", b"")
    await environment.write_text("nested/example.txt", "native text")
    assert await environment.read_text("nested/example.txt") == "native text"
    assert (
        Path(environment.workspace or "") / "nested/example.txt"
    ).read_text() == "native text"
    returncode, stdout, stderr = await run_command(
        environment, ["/usr/bin/uname", "-s"]
    )
    assert (returncode, stdout, stderr) == (0, b"Darwin\n", b"")


async def test_files_denied_outside_grants_including_symlinks(
    environment: OverallsEnvironment, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel")
    workspace = Path(environment.workspace or "")
    (workspace / "escape").symlink_to(outside)
    for path in (str(outside), "escape"):
        with pytest.raises(OSError):
            await environment.read_text(path)
        with pytest.raises(OSError):
            await environment.write_text(path, "changed")
    assert outside.read_text() == "sentinel"
    (workspace / ".git").mkdir()
    with pytest.raises(OSError):
        await environment.write_text(".git/config", "changed")
    # Child processes inherit the boundary, not just Environment file helpers.
    returncode, _, _ = await run_command(
        environment, ["/bin/sh", "-c", 'cat "$1"', "sh", str(outside)]
    )
    assert returncode != 0


async def test_tcp_and_unix_sockets_denied(environment: OverallsEnvironment) -> None:
    # Live listeners ensure failure means confinement rather than connection refusal.
    with socket.socket() as tcp, socket.socket(socket.AF_UNIX) as unix:
        tcp.bind(("127.0.0.1", 0))
        tcp.listen()
        path = str(environment.temporary_path / "s")
        unix.bind(path)
        unix.listen()
        checks = [
            (socket.AF_INET, tcp.getsockname()),
            (socket.AF_UNIX, path),
        ]
        for family, address in checks:
            with socket.socket(family) as baseline:
                baseline.settimeout(1)
                baseline.connect(address)
            connection, _ = (tcp if family == socket.AF_INET else unix).accept()
            connection.close()
            command = (
                "import socket,sys,json; "
                f"s=socket.socket({int(family)}); s.settimeout(1); "
                f"address=json.loads({json.dumps(json.dumps(address))}); "
                "address=tuple(address) if isinstance(address,list) else address; "
                "\ntry: s.connect(address)\nexcept OSError as e: print(e.errno); sys.exit(23)\n"
            )
            returncode, stdout, stderr = await run_command(
                environment, [environment.python, "-I", "-c", command]
            )
            assert returncode == 23, stderr
            assert int(stdout) in (errno.EPERM, errno.EACCES)


async def test_code_callbacks_and_cancellation(
    environment: OverallsEnvironment,
) -> None:
    output: list[str] = []
    started = asyncio.Event()

    def write(content: str) -> None:
        output.append(content)
        if "started" in "".join(output):
            started.set()

    async def callback(name: str, arguments: dict[str, Any]) -> Any:
        assert name == "echo"
        return arguments["text"]

    await environment.run_code(
        "print(await tools.echo(text='works'))",
        tools=["echo"],
        call_tool=callback,
        write=write,
    )
    assert "works" in "".join(output)
    task = asyncio.create_task(
        environment.run_code(
            "import asyncio; print('started'); await asyncio.sleep(60)",
            tools=[],
            call_tool=callback,
            write=write,
        )
    )
    async with asyncio.timeout(5):
        await started.wait()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.3):
            await task
    assert "started" in "".join(output)


async def test_environment_does_not_inherit_provider_secrets(
    environment: OverallsEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUB_API_KEY", "sentinel-secret")
    monkeypatch.setenv("PYTHONSTARTUP", "/secret/startup.py")
    returncode, stdout, stderr = await run_command(
        environment,
        [
            environment.python,
            "-I",
            "-c",
            "import os; print(os.getenv('BUB_API_KEY')); print(os.getenv('PYTHONSTARTUP'))",
        ],
    )
    assert (returncode, stdout, stderr) == (0, b"None\nNone\n", b"")


async def test_close_stops_ordinary_process_groups(
    environment: OverallsEnvironment,
) -> None:
    process = await environment.spawn("sleep 60 & wait")
    process.close_stdin()
    await environment.close()
    assert process.returncode is not None
    assert not environment.temporary_path.exists()


async def test_explicit_read_and_write_grants(tmp_path: Path) -> None:
    workspace, read, write = (
        tmp_path / name for name in ("workspace", "read", "write")
    )
    for directory in (workspace, read, write):
        directory.mkdir()
    (read / "input").write_text("allowed")
    async with OverallsEnvironment(
        OverallsConfig(read_paths=(read,), write_paths=(write,)), workspace
    ) as environment:
        assert await environment.read_text(str(read / "input")) == "allowed"
        with pytest.raises(OSError):
            await environment.write_text(str(read / "input"), "denied")
        await environment.write_text(str(write / "output"), "allowed")
        assert (write / "output").read_text() == "allowed"


async def test_workspace_with_unicode_and_metacharacters(tmp_path: Path) -> None:
    workspace = tmp_path / "project [中文]"
    workspace.mkdir()
    async with OverallsEnvironment(OverallsConfig(), workspace) as environment:
        await environment.write_text("example.txt", "works")
        assert await environment.read_text("example.txt") == "works"


@pytest.mark.parametrize("network_access", [False, True])
@pytest.mark.parametrize(
    "family, host", [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")]
)
@pytest.mark.parametrize("kind", [socket.SOCK_STREAM, socket.SOCK_DGRAM])
async def test_network_policy_controls_ip_traffic(
    tmp_path: Path, network_access: bool, family: int, host: str, kind: int
) -> None:
    async with OverallsEnvironment(
        OverallsConfig(network_access=network_access), tmp_path
    ) as environment:
        with socket.socket(family, kind) as listener:
            listener.settimeout(2)
            listener.bind((host, 0))
            address = (host, listener.getsockname()[1])
            if kind == socket.SOCK_STREAM:
                listener.listen()
            # Establish the same destination is reachable without the sandbox.
            with socket.socket(family, kind) as baseline:
                baseline.settimeout(2)
                baseline.connect(address)
                baseline.sendall(b"ping")
            if kind == socket.SOCK_STREAM:
                connection, _ = listener.accept()
                connection.close()
            else:
                assert listener.recvfrom(16)[0] == b"ping"
            command = (
                "import socket,sys; "
                f"s=socket.socket({int(family)}, {int(kind)}); s.settimeout(2); "
                "\ntry:\n"
                f" s.connect({address!r}); s.sendall(b'ping'); print('connected')\n"
                "except OSError as e: print(e.errno); sys.exit(23)\n"
            )
            returncode, stdout, stderr = await run_command(
                environment, [environment.python, "-I", "-c", command]
            )
            if network_access:
                assert (returncode, stdout, stderr) == (0, b"connected\n", b"")
                if kind == socket.SOCK_STREAM:
                    connection, _ = listener.accept()
                    with connection:
                        assert connection.recv(16) == b"ping"
                else:
                    assert listener.recvfrom(16)[0] == b"ping"
            else:
                assert returncode == 23, stderr
                assert int(stdout) in (errno.EPERM, errno.EACCES)


async def test_network_enabled_retains_filesystem_and_unix_socket_boundary(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("sentinel")
    async with OverallsEnvironment(
        OverallsConfig(network_access=True), workspace
    ) as environment:
        with pytest.raises(OSError):
            await environment.read_text(str(outside))
        with pytest.raises(OSError):
            await environment.write_text(str(outside), "changed")
        (workspace / ".git").mkdir()
        with pytest.raises(OSError):
            await environment.write_text(".git/config", "changed")
        with socket.socket(socket.AF_UNIX) as listener:
            path = str(environment.temporary_path / "local.sock")
            listener.bind(path)
            listener.listen()
            with socket.socket(socket.AF_UNIX) as baseline:
                baseline.connect(path)
            connection, _ = listener.accept()
            connection.close()
            command = (
                "import socket,sys; s=socket.socket(socket.AF_UNIX); "
                "\ntry:\n"
                f" s.connect({path!r})\n"
                "except OSError as e: print(e.errno); sys.exit(23)\n"
            )
            returncode, stdout, stderr = await run_command(
                environment, [environment.python, "-I", "-c", command]
            )
            assert returncode == 23, stderr
            assert int(stdout) in (errno.EPERM, errno.EACCES)


@pytest.mark.parametrize("network_access", [False, True])
async def test_network_policy_denies_inbound_listeners(
    tmp_path: Path, network_access: bool
) -> None:
    async with OverallsEnvironment(
        OverallsConfig(network_access=network_access), tmp_path
    ) as environment:
        command = (
            "import socket,sys; s=socket.socket(); "
            "\ntry:\n"
            " s.bind(('127.0.0.1',0)); s.listen()\n"
            "except OSError as e: print(e.errno); sys.exit(23)\n"
        )
        returncode, stdout, stderr = await run_command(
            environment, [environment.python, "-I", "-c", command]
        )
        assert returncode == 23, stderr
        assert int(stdout) in (errno.EPERM, errno.EACCES)


@pytest.mark.parametrize("create_first", [False, True])
@pytest.mark.parametrize("raise_in_body", [False, True])
async def test_context_closes_processes_and_preserves_body_errors(
    tmp_path: Path, create_first: bool, raise_in_body: bool
) -> None:
    config = OverallsConfig()
    environment = (
        await OverallsEnvironment.create(config, tmp_path)
        if create_first
        else OverallsEnvironment(config, tmp_path)
    )
    process: LocalProcess | None = None

    async def run_in_context() -> None:
        nonlocal process
        async with environment as active_environment:
            assert active_environment is environment
            process = await active_environment.spawn("sleep 60 & wait")
            process.close_stdin()
            if raise_in_body:
                raise ValueError("body failed")

    if raise_in_body:
        with pytest.raises(ValueError, match="body failed"):
            await run_in_context()
    else:
        await run_in_context()
    assert process is not None
    assert process.returncode is not None
    assert not environment.temporary_path.exists()
    with pytest.raises(RuntimeError, match="closed"):
        await environment.spawn(["/usr/bin/true"])


async def test_context_cancellation_closes_processes(tmp_path: Path) -> None:
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    started = asyncio.Event()
    process: LocalProcess | None = None

    async def run_in_context() -> None:
        nonlocal process
        async with environment:
            process = await environment.spawn("sleep 60 & wait")
            process.close_stdin()
            started.set()
            await asyncio.sleep(60)

    async with asyncio.timeout(5):
        task = asyncio.create_task(run_in_context())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert process is not None
    assert process.returncode is not None
    assert not environment.temporary_path.exists()


@pytest.mark.parametrize("kill", [False, True])
async def test_signals_stop_process_groups(
    environment: OverallsEnvironment, kill: bool
) -> None:
    process = await environment.spawn(
        [
            environment.python,
            "-I",
            "-c",
            "import os,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
            "print(os.getpid(),child.pid,flush=True); time.sleep(60)",
        ]
    )
    try:
        async with asyncio.timeout(5):
            pids = [int(value) for value in (await process.stdout.readline()).split()]
            assert len(pids) == 2
            process.signal(kill=kill)
            assert await process.wait() == -(signal.SIGKILL if kill else signal.SIGTERM)
            await assert_processes_stopped(pids)
        process.signal(kill=True)
    finally:
        process.signal(kill=True)


async def test_close_kills_descendants_after_group_leader_exits(
    environment: OverallsEnvironment,
) -> None:
    process = await environment.spawn(
        [
            environment.python,
            "-I",
            "-c",
            "import subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'], "
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "print(child.pid,flush=True)",
        ]
    )
    async with asyncio.timeout(5):
        child_pid = int(await process.stdout.readline())
        assert await process.wait() == 0
        assert await run_command(environment, ["/usr/bin/true"]) == (0, b"", b"")
        await environment.close()
        await assert_processes_stopped([child_pid])


async def test_cancelling_wait_keeps_caller_owned_process_running(
    environment: OverallsEnvironment,
) -> None:
    process = await environment.spawn(["/bin/sleep", "60"])
    waiter = asyncio.create_task(process.wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert process.is_running()
    process.signal(kill=True)
    async with asyncio.timeout(5):
        assert await process.wait() == -signal.SIGKILL


async def test_normal_close_runs_child_termination_handler(
    environment: OverallsEnvironment,
) -> None:
    marker = Path(environment.workspace or "") / "terminated"
    process = await environment.spawn(
        [
            environment.python,
            "-I",
            "-c",
            "import signal,sys,time\n"
            "from pathlib import Path\n"
            "def stop(signum, frame):\n"
            "    Path(sys.argv[1]).write_text('clean shutdown')\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "print('ready',flush=True)\n"
            "time.sleep(60)\n",
            str(marker),
        ]
    )
    async with asyncio.timeout(5):
        assert await process.stdout.readline() == b"ready\n"
        await environment.close()
    assert process.returncode == 0
    assert marker.read_text() == "clean shutdown"
    assert not environment.temporary_path.exists()


@pytest.mark.parametrize("grace_seconds", [0.0, 0.05])
async def test_normal_close_kills_child_that_ignores_termination(
    environment: OverallsEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    grace_seconds: float,
) -> None:
    monkeypatch.setattr(
        "bub_overalls.process._TERMINATE_TIMEOUT_SECONDS", grace_seconds
    )
    process = await environment.spawn(
        [
            environment.python,
            "-I",
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(60)",
        ]
    )
    async with asyncio.timeout(5):
        assert await process.stdout.readline() == b"ready\n"
        await environment.close()
    assert process.returncode == -signal.SIGKILL


async def test_normal_close_waits_for_descendant_after_leader_exits(
    environment: OverallsEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bub_overalls.process._TERMINATE_TIMEOUT_SECONDS", 0.05)
    marker = Path(environment.workspace or "") / "ready"
    child_source = (
        "import signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "Path(sys.argv[1]).touch(); time.sleep(60)"
    )
    process = await environment.spawn(
        [
            environment.python,
            "-I",
            "-c",
            "import subprocess,sys,time; from pathlib import Path\n"
            "child=subprocess.Popen([sys.executable,'-I','-c',sys.argv[1],sys.argv[2]], "
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "while not Path(sys.argv[2]).exists(): time.sleep(0.01)\n"
            "print(child.pid,flush=True)\n",
            child_source,
            str(marker),
        ]
    )
    async with asyncio.timeout(5):
        child_pid = int(await process.stdout.readline())
        assert await process.wait() == 0
        await environment.close()
        await assert_processes_stopped([child_pid])


@pytest.mark.parametrize("git_access", [False, True])
@pytest.mark.parametrize("prefix", ["", "nested/deep"])
async def test_agent_and_ide_configuration_is_read_only_at_every_depth(
    tmp_path: Path, git_access: bool, prefix: str
) -> None:
    workspace = tmp_path / "workspace"
    extra = tmp_path / "extra"
    files = (
        ".agents/instructions.md",
        ".codex/config.toml",
        ".claude/settings.json",
        ".cursor/settings.json",
        ".vscode/tasks.json",
        ".idea/workspace.xml",
        ".mcp.json",
        ".aws/config",
        ".ssh/config",
    )
    workspace.mkdir()
    extra.mkdir()
    environment = OverallsEnvironment(
        OverallsConfig(git_access=git_access, write_paths=(extra,)), workspace
    )
    roots = (workspace, extra, environment.temporary_path)
    try:
        for root in roots:
            for filename in files:
                path = root / prefix / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("original")
        async with environment:
            for root in roots:
                await _assert_protected_config_files(environment, root / prefix, files)
    finally:
        await environment.close()


async def _assert_protected_config_files(
    environment: OverallsEnvironment, root: Path, files: tuple[str, ...]
) -> None:
    await environment.write_text(str(root / "ordinary.txt"), "allowed")
    returncode, _, stderr = await run_command(
        environment,
        [
            environment.python,
            "-I",
            "-c",
            "import json,pathlib,sys\n"
            "root=pathlib.Path(sys.argv[1])\n"
            "for name in json.loads(sys.argv[2]):\n"
            " path=root/name\n"
            " for operation in (path.unlink, lambda: path.rename(root/'renamed')):\n"
            "  try: operation()\n"
            "  except PermissionError: pass\n"
            "  else: raise AssertionError('protected file was removed or renamed')\n",
            str(root),
            json.dumps(files),
        ],
    )
    assert returncode == 0, stderr
    for filename in files:
        path = root / filename
        with pytest.raises(OSError):
            await environment.write_text(str(path), "changed")
        assert path.read_text() == "original"
        assert await environment.read_text(str(path)) == "original"
        if path.name.lower() not in (".git", ".mcp.json"):
            with pytest.raises(OSError):
                await environment.write_text(str(path.parent / "new"), "new")


@pytest.mark.parametrize("git_access", [False, True])
async def test_recursive_protection_matches_case_but_not_name_prefixes(
    tmp_path: Path, git_access: bool
) -> None:
    workspace = tmp_path / "workspace.a]b[c]*.(d)$^|+?{2} 資料"
    extra = tmp_path / "extra.a]b[c]*.(d)$^|+?{2} 資料"
    workspace.mkdir()
    extra.mkdir()
    protected = (
        "deep/.GiT/config",
        "deep/child/.GIT/hooks/example",
        "deep/.ClAuDe/settings.json",
        "deep/child/.VSCODE/tasks.json",
        "deep/.IDEA/workspace.xml",
        "deep/.MCP.JSON",
        "gitfile/.git",
    )
    # Even grants pointing directly into protected directories cannot unlock them.
    (extra / "deep/.GiT").mkdir(parents=True)
    (extra / "deep/.ClAuDe").mkdir()
    environment = OverallsEnvironment(
        OverallsConfig(
            git_access=git_access,
            write_paths=(extra, extra / "deep/.GiT", extra / "deep/.ClAuDe"),
        ),
        workspace,
    )
    roots = (workspace, extra, environment.temporary_path)
    try:
        for root in roots:
            for filename in protected:
                path = root / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("original")
            (root / "git-alias").symlink_to(root / protected[0])
            (root / "config-alias").symlink_to(root / protected[2])
        async with environment:
            for root in roots:
                await _assert_protected_config_files(environment, root, protected)
                for name in ("git-alias", "config-alias"):
                    with pytest.raises(OSError):
                        await environment.write_text(str(root / name), "changed")
                for name in (
                    "fresh/deep/.git/config",
                    "fresh/deep/.GiT/config",
                    "fresh/deep/.IDEA/workspace.xml",
                    "fresh/deep/.MCP.JSON",
                ):
                    with pytest.raises(OSError):
                        await environment.write_text(str(root / name), "new")
                    assert not (root / name).exists()
                for name in (
                    ".github/config",
                    "deep/.gitignore",
                    "deep/.git-backup/config",
                    "deep/.idealist/file",
                    "deep/.claude-backup/file",
                    "deep/.mcp.json.backup",
                    "deep/ordinary.txt",
                ):
                    await environment.write_text(str(root / name), "allowed")
                    assert (root / name).read_text() == "allowed"
    finally:
        await environment.close()


@pytest.mark.parametrize("git_access", [False, True])
@pytest.mark.parametrize(
    "relative_workspace",
    [".claude/worktrees/feat", ".codex/worktrees/abc/proj", ".git/cache/ws", ".idea"],
)
async def test_protected_ancestors_do_not_make_write_roots_read_only(
    tmp_path: Path, git_access: bool, relative_workspace: str
) -> None:
    workspace = tmp_path / relative_workspace
    extra = tmp_path / ".cursor" / "some-dev-dir"
    workspace.mkdir(parents=True)
    extra.mkdir(parents=True)
    async with OverallsEnvironment(
        OverallsConfig(git_access=git_access, write_paths=(extra,)), workspace
    ) as environment:
        for root in (workspace, extra):
            await environment.write_text(str(root / "file.txt"), "ok")
            assert await environment.read_text(str(root / "file.txt")) == "ok"
            await environment.write_text(str(root / ".github" / "file"), "ok")
            for relative in (".GiT/config", "deep/.git/config", "deep/.IDEA/settings"):
                with pytest.raises(OSError):
                    await environment.write_text(str(root / relative), "denied")


async def test_write_root_case_alias_keeps_nested_metadata_protected(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "Workspace"
    workspace.mkdir()
    alias = tmp_path / "wOrKsPaCe"
    if not alias.is_dir():
        pytest.skip("requires a case-insensitive filesystem")
    async with OverallsEnvironment(OverallsConfig(), alias) as environment:
        await environment.write_text("ordinary.txt", "ok")
        with pytest.raises(OSError):
            await environment.write_text("deep/.git/config", "denied")
