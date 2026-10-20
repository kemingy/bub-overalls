"""Portable policy/launcher checks; these do not establish Linux enforcement."""

import ctypes
import errno
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from bub_overalls import OverallsConfig, OverallsEnvironment, _linux, landlock


def _evaluate_filter(
    rules: list[tuple[int, int, int, int]],
    *,
    arch: int,
    number: int,
    domain: int = 0,
    kind: int = 1,
) -> int:
    accumulator = index = 0
    data = {0: number, 4: arch, 16: domain, 24: kind}
    while index < len(rules):
        code, yes, no, value = rules[index]
        if code == 0x20:
            accumulator = data[value]
        elif code == 0x54:
            accumulator &= value
        elif code == 0x15:
            index += yes if accumulator == value else no
        elif code == 0x35:
            index += yes if accumulator >= value else no
        elif code == 0x06:
            return value
        else:
            pytest.fail(f"unknown BPF instruction {code}")
        index += 1
    pytest.fail("filter did not return")


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
@pytest.mark.parametrize("network_access", [False, True])
def test_filter_covers_network_families_and_bypass_interfaces(
    machine: str, network_access: bool
) -> None:
    syscalls = _linux._get_syscalls(machine)
    rules = _linux._build_seccomp_filter(syscalls, network_access=network_access)
    allow, deny = 0x7FFF0000, 0x00050000 | errno.EPERM
    for domain in (1, 2, 10, 16, 17):  # Unix, IPv4/6, netlink, packet
        assert _evaluate_filter(
            rules, arch=syscalls.audit_arch, number=syscalls.socket, domain=domain
        ) == (allow if network_access and domain in (2, 10) else deny)
    for kind in (1, 2, 3, 5, 1 | 0x80000, 2 | 0x800):
        assert _evaluate_filter(
            rules, arch=syscalls.audit_arch, number=syscalls.socket, domain=2, kind=kind
        ) == (allow if network_access and kind & 0xF in (1, 2) else deny)
    for number in syscalls.blocked:
        assert _evaluate_filter(rules, arch=syscalls.audit_arch, number=number) == deny
    assert _evaluate_filter(rules, arch=0, number=syscalls.socket) == 0x80000000
    assert (
        _evaluate_filter(
            rules, arch=syscalls.audit_arch, number=syscalls.socket | 0x40000000
        )
        == deny
    )
    # execve and anonymous IPC must remain available for Bub's code runner.
    for number in (59, 53) if machine == "x86_64" else (221, 199):
        assert _evaluate_filter(rules, arch=syscalls.audit_arch, number=number) == allow


def test_unknown_architecture_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="architecture"):
        _linux._get_syscalls("riscv64")


def test_file_identity_ignores_timestamp_changes(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("first")
    second.write_text("second")
    before = first.stat()
    os.utime(first, (before.st_atime + 1, before.st_mtime + 1))
    after = first.stat()
    assert _linux._same_file(before, after)
    assert not _linux._same_file(after, second.stat())


@pytest.mark.parametrize("abi", [-1, 0, 1, 3, 5])
def test_old_or_unavailable_kernel_fails_before_restriction(
    abi: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    libc = Mock()
    libc.syscall.return_value = abi
    monkeypatch.setattr("bub_overalls._linux.ctypes.CDLL", lambda *a, **kw: libc)
    monkeypatch.setattr("bub_overalls._linux.platform.machine", lambda: "x86_64")
    ctypes.set_errno(errno.ENOSYS)
    policy = _linux.OverallsLandlockPolicy((tmp_path,), (tmp_path,), False)
    with pytest.raises((OSError, RuntimeError)):
        _linux._restrict(policy)
    libc.prctl.assert_not_called()
    assert libc.syscall.call_count == 1


@pytest.mark.parametrize("network_access", [False, True])
def test_policy_preserves_explicit_grants(tmp_path: Path, network_access: bool) -> None:
    read, write = tmp_path / "read", tmp_path / "write"
    read.mkdir()
    write.mkdir()
    policy = landlock.build_overalls_landlock_policy(
        OverallsConfig(
            read_paths=(read,), write_paths=(write,), network_access=network_access
        ),
        tmp_path / "workspace",
        tmp_path / "temp",
    )
    assert read in policy.read_paths and read not in policy.write_paths
    assert write in policy.read_paths and write in policy.write_paths
    assert json.loads(policy.serialize())["network_access"] is network_access


def test_linux_rejects_selective_git_grants(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="selective Git"):
        landlock.build_overalls_landlock_policy(
            OverallsConfig(git_access=True), tmp_path, tmp_path / "temp"
        )


def test_bootstrap_runtime_cannot_overlap_write_grants(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="runtime must not be writable"):
        landlock.build_overalls_landlock_policy(
            OverallsConfig(write_paths=(Path(sys.base_prefix),)), tmp_path, tmp_path
        )


async def test_linux_spawns_only_immutable_bootstrap_and_defers_target_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    spawn = AsyncMock()
    monkeypatch.setattr(environment._process_manager, "spawn", spawn)
    try:
        await environment.spawn(
            ["/bin/echo", "hello"], env={"LD_PRELOAD": "target-only"}
        )
        args, kwargs = spawn.call_args
        assert args[0][:5] == [
            str(Path(sys.executable).resolve()),
            "-I",
            "-S",
            "-c",
            landlock._LAUNCHER_SOURCE,
        ]
        assert args[0][-2:] == ["/bin/echo", "hello"]
        assert "LD_PRELOAD" not in kwargs["env"]
        assert (
            json.loads(kwargs["env"]["_BUB_OVERALLS_TARGET_ENV"])["LD_PRELOAD"]
            == "target-only"
        )
    finally:
        await environment.close()


async def test_linux_failed_preflight_never_runs_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    environment = OverallsEnvironment(OverallsConfig(), tmp_path)
    process = Mock()
    process.stdout.read = AsyncMock(return_value=b"")
    process.stderr.read = AsyncMock(return_value=b"Landlock unavailable")
    process.wait = AsyncMock(return_value=125)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(environment, "spawn", spawn)
    with pytest.raises(RuntimeError, match="Landlock/seccomp preflight failed"):
        await environment.__aenter__()
    spawn.assert_awaited_once_with(["/usr/bin/true"])
    assert not environment.temporary_path.exists()


def test_launcher_failure_does_not_exec_target(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(policy: _linux.OverallsLandlockPolicy) -> None:
        raise PermissionError("seccomp unavailable")

    policy = _linux.OverallsLandlockPolicy((), (), False)
    monkeypatch.setattr(sys, "argv", ["launcher", policy.serialize(), "/bin/true"])
    monkeypatch.setattr(_linux, "_restrict", fail)
    execute = Mock()
    monkeypatch.setattr("bub_overalls._linux.os.execvpe", execute)
    assert _linux._main() == 125
    execute.assert_not_called()


@pytest.mark.parametrize("payload", ["{}", "[]"])
def test_launcher_rejects_malformed_policy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: str
) -> None:
    monkeypatch.setattr(sys, "argv", ["launcher", payload, "/bin/true"])
    execute = Mock()
    monkeypatch.setattr("bub_overalls._linux.os.execvpe", execute)
    assert _linux._main() == 125
    assert "Landlock/seccomp launcher failed" in capsys.readouterr().err
    execute.assert_not_called()


def test_launcher_missing_target_env_is_concise(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = _linux.OverallsLandlockPolicy((), (), False)
    monkeypatch.setattr(sys, "argv", ["launcher", policy.serialize(), "/bin/true"])
    monkeypatch.setattr(_linux, "_restrict", lambda _: None)
    monkeypatch.delenv("_BUB_OVERALLS_TARGET_ENV", raising=False)
    execute = Mock()
    monkeypatch.setattr("bub_overalls._linux.os.execvpe", execute)
    assert _linux._main() == 125
    assert "Landlock/seccomp launcher failed" in capsys.readouterr().err
    execute.assert_not_called()


def test_isolated_bootstrap_imports_without_bub_or_site_packages() -> None:
    source = landlock._LAUNCHER_SOURCE.replace(
        "        _restrict(policy)",
        "        raise RuntimeError('bootstrap reached restriction boundary')",
    )
    result = subprocess.run(
        [
            str(Path(sys.executable).resolve()),
            "-I",
            "-S",
            "-c",
            source,
            _linux.OverallsLandlockPolicy((), (), False).serialize(),
            "/bin/true",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 125, result.stderr
    assert "bootstrap reached restriction boundary" in result.stderr


def test_redirected_grant_fails_before_restricting_or_execing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    libc = Mock()
    descriptor = os.open(os.devnull, os.O_RDONLY)
    libc.syscall.side_effect = [6, descriptor, -1]
    monkeypatch.setattr("bub_overalls._linux.ctypes.CDLL", lambda *a, **kw: libc)
    monkeypatch.setattr("bub_overalls._linux.platform.machine", lambda: "x86_64")
    monkeypatch.setattr(_linux, "_prepare_isolation", lambda *args: tmp_path)
    ctypes.set_errno(errno.ELOOP)
    with pytest.raises(OSError) as caught:
        _linux._restrict(_linux.OverallsLandlockPolicy((tmp_path,), (), False))
    assert caught.value.errno == errno.ELOOP
    assert libc.syscall.call_args.args[0] == 437  # openat2, no os.open fallback
    libc.prctl.assert_not_called()
    with pytest.raises(OSError):
        os.fstat(descriptor)


@pytest.mark.parametrize("unsupported", ["symlink", "hardlink", "git-pointer"])
def test_mount_scan_rejects_unsupported_protected_layouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsupported: str
) -> None:
    def open_path(libc: ctypes.CDLL, path: Path, *, directory: bool = False) -> int:
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW)

    monkeypatch.setattr(_linux, "_open_path", open_path)
    protected = tmp_path / ".ssh"
    protected.mkdir()
    original = tmp_path / "ordinary"
    original.write_text("sentinel")
    if unsupported == "symlink":
        (protected / "alias").symlink_to(original)
    elif unsupported == "hardlink":
        os.link(original, protected / "alias")
    else:
        (tmp_path / ".git").write_text("gitdir: elsewhere")
    policy = _linux.OverallsLandlockPolicy(
        (), (tmp_path,), False, protected_names=(".ssh", ".git")
    )
    with pytest.raises(RuntimeError, match="unsupported"):
        _linux._find_protected_paths(Mock(), policy)


def test_mount_scan_pins_only_topmost_protected_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def open_path(libc: ctypes.CDLL, path: Path, *, directory: bool = False) -> int:
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW)

    monkeypatch.setattr(_linux, "_open_path", open_path)
    protected = tmp_path / "nested" / ".SSH"
    protected.mkdir(parents=True)
    (protected / "file").write_text("sentinel")
    (tmp_path / "ordinary").write_text("ordinary")
    policy = _linux.OverallsLandlockPolicy(
        (), (tmp_path, protected), False, protected_names=(".ssh",)
    )
    paths = _linux._find_protected_paths(Mock(), policy)
    try:
        assert [path for path, _ in paths] == [protected]
        assert os.fstat(paths[0][1]).st_ino == protected.stat().st_ino
    finally:
        for _, descriptor in paths:
            os.close(descriptor)


@pytest.mark.parametrize("network_access", [False, True])
def test_namespace_failure_stops_before_landlock_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, network_access: bool
) -> None:
    libc = Mock()
    libc.syscall.return_value = 6
    libc.unshare.return_value = -1
    monkeypatch.setattr("bub_overalls._linux.ctypes.CDLL", lambda *args, **kwargs: libc)
    monkeypatch.setattr("bub_overalls._linux.platform.machine", lambda: "aarch64")
    monkeypatch.setattr("bub_overalls._linux.os.getuid", lambda: 1000)
    ctypes.set_errno(errno.EPERM)
    policy = _linux.OverallsLandlockPolicy(
        (), (tmp_path,), network_access, temporary_path=tmp_path
    )
    with pytest.raises(PermissionError):
        _linux._restrict(policy)
    assert libc.syscall.call_count == 1
    libc.capset.assert_not_called()
    libc.prctl.assert_not_called()
