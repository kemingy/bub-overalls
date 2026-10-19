"""Standalone Linux child launcher, using only the Python standard library.

The host snapshots this file before launching commands with Python -I -S -c.
Keep package imports and host policy discovery out of this execution boundary.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import platform
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Self

# Linux UAPI, identical syscall numbers on the two supported native ABIs.
_OPENAT2 = 437
_OPEN_TREE = 428
_MOVE_MOUNT = 429
_MOUNT_SETATTR = 442
_CREATE_RULESET = 444
_ADD_RULE = 445
_RESTRICT_SELF = 446

# openat2 and descriptor-based mount operations.
_AT_FDCWD = -100
_AT_EMPTY_PATH = 0x1000
_AT_RECURSIVE = 0x8000
_O_PATH = 0x200000  # os.O_PATH is unavailable when imported on macOS.
_RESOLVE_NO_SYMLINKS = 0x04
_OPEN_TREE_CLONE = 1
_MOUNT_ATTR_RDONLY = 1
_MOUNT_ATTR_NOSUID = 2
_MOUNT_ATTR_NODEV = 4
_MOVE_MOUNT_F_EMPTY_PATH = 0x04
_MOVE_MOUNT_T_EMPTY_PATH = 0x40

# Namespace setup and capability removal.
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNS = 0x00020000
_CLONE_NEWIPC = 0x08000000
_CLONE_NEWNET = 0x40000000
_MS_PRIVATE = 0x40000
_MS_REC = 0x4000
_CAPABILITY_VERSION_3 = 0x20080522
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2

# Landlock policy rights, through ABI 6.
_MIN_ABI = 6  # Linux 6.12: signal and abstract Unix socket scoping.
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_SCOPE_ABSTRACT_UNIX_SOCKET = 1
_SCOPE_SIGNAL = 2
_READ = (1 << 0) | (1 << 2) | (1 << 3)
_FILE_READ = (1 << 0) | (1 << 2)
_FILE_WRITE = (1 << 1) | (1 << 14)
_DEVICE_ACCESS = (1 << 1) | (1 << 2)
_WRITE = sum(1 << bit for bit in (1, 4, 5, 7, 8, 9, 10, 12, 13, 14))
_HANDLED_FS = (1 << 16) - 1  # Through IOCTL_DEV (ABI 5).


@dataclass(frozen=True)
class OverallsLandlockPolicy:
    """Positive content grants plus existing paths to mount read-only."""

    read_paths: tuple[Path, ...]
    write_paths: tuple[Path, ...]
    network_access: bool
    protected_names: tuple[str, ...] = ()
    protected_paths: tuple[Path, ...] = ()
    git_marker: Path | None = None
    temporary_path: Path | None = None

    def serialize(self) -> str:
        return json.dumps(
            {
                "read_paths": [str(path) for path in self.read_paths],
                "write_paths": [str(path) for path in self.write_paths],
                "network_access": self.network_access,
                "protected_names": self.protected_names,
                "protected_paths": [str(path) for path in self.protected_paths],
                "git_marker": str(self.git_marker) if self.git_marker else None,
                "temporary_path": str(self.temporary_path)
                if self.temporary_path
                else None,
            }
        )

    @classmethod
    def deserialize(cls, serialized: str) -> Self:
        data = json.loads(serialized)
        return cls(
            read_paths=tuple(Path(path) for path in data["read_paths"]),
            write_paths=tuple(Path(path) for path in data["write_paths"]),
            network_access=data["network_access"],
            protected_names=tuple(data["protected_names"]),
            protected_paths=tuple(Path(path) for path in data["protected_paths"]),
            git_marker=Path(data["git_marker"]) if data["git_marker"] else None,
            temporary_path=(
                Path(data["temporary_path"]) if data["temporary_path"] else None
            ),
        )


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


class _MountAttr(ctypes.Structure):
    _fields_ = [
        ("attr_set", ctypes.c_uint64),
        ("attr_clr", ctypes.c_uint64),
        ("propagation", ctypes.c_uint64),
        ("userns_fd", ctypes.c_uint64),
    ]


class _CapabilityHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapabilityData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _PathAttr(ctypes.Structure):
    _layout_ = "ms"  # Explicit packed layout for ctypes 3.14+; offsets match the UAPI.
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _Filter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _Program(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_Filter))]


@dataclass(frozen=True)
class _OverallsSyscalls:
    audit_arch: int
    socket: int
    blocked: tuple[int, ...]


def _get_syscalls(machine: str) -> _OverallsSyscalls:
    # Deny explicit listeners, cross-process FD acquisition, namespace changes,
    # and io_uring (its socket operations do not traverse seccomp syscall hooks).
    common = (
        425,  # io_uring_setup
        426,  # io_uring_enter
        427,  # io_uring_register
        428,  # open_tree
        429,  # move_mount
        430,  # fsopen
        431,  # fsconfig
        432,  # fsmount
        433,  # fspick
        438,  # pidfd_getfd
        442,  # mount_setattr
    )
    if machine == "x86_64":
        return _OverallsSyscalls(
            audit_arch=0xC000003E,
            socket=41,
            blocked=(
                43,  # accept
                49,  # bind
                50,  # listen
                288,  # accept4
                101,  # ptrace
                165,  # mount
                166,  # umount2
                272,  # unshare
                308,  # setns
                *common,
            ),
        )
    if machine == "aarch64":
        return _OverallsSyscalls(
            audit_arch=0xC00000B7,
            socket=198,
            blocked=(
                200,  # bind
                201,  # listen
                202,  # accept
                242,  # accept4
                117,  # ptrace
                40,  # mount
                39,  # umount2
                97,  # unshare
                268,  # setns
                *common,
            ),
        )
    raise RuntimeError(f"Landlock launcher does not support architecture {machine!r}")


def _build_seccomp_filter(
    syscalls: _OverallsSyscalls, *, network_access: bool
) -> list[tuple[int, int, int, int]]:
    # Classic BPF over struct seccomp_data. Reject compat and x32 ABIs before
    # interpreting syscall numbers so neither can evade the native filter.
    load, equal, greater_equal, ret = 0x20, 0x15, 0x35, 0x06
    deny = 0x00050000 | errno.EPERM
    rules = [
        (load, 0, 0, 4),
        (equal, 1, 0, syscalls.audit_arch),
        (ret, 0, 0, 0x80000000),  # SECCOMP_RET_KILL_PROCESS
        (load, 0, 0, 0),
        (greater_equal, 0, 1, 0x40000000),
        (ret, 0, 0, deny),
    ]
    for number in syscalls.blocked:
        rules.extend(((equal, 0, 1, number), (ret, 0, 0, deny)))
    if network_access:
        # Only IPv4/IPv6 stream/datagram sockets. Anonymous socketpair IPC remains available.
        rules.extend(
            [
                (equal, 0, 9, syscalls.socket),
                (load, 0, 0, 16),  # args[0], socket domain
                (equal, 2, 0, 2),  # AF_INET
                (equal, 1, 0, 10),  # AF_INET6
                (ret, 0, 0, deny),
                (load, 0, 0, 24),  # args[1], socket type
                (0x54, 0, 0, 0xF),  # BPF_AND: strip SOCK_CLOEXEC/NONBLOCK
                (equal, 2, 0, 1),  # SOCK_STREAM
                (equal, 1, 0, 2),  # SOCK_DGRAM
                (ret, 0, 0, deny),
            ]
        )
    else:
        rules.extend(((equal, 0, 1, syscalls.socket), (ret, 0, 0, deny)))
    rules.append((ret, 0, 0, 0x7FFF0000))  # SECCOMP_RET_ALLOW
    return rules


def _check_result(result: int) -> int:
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def _open_path(libc: ctypes.CDLL, path: Path, *, directory: bool = False) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY if directory else _O_PATH
    how = _OpenHow(flags | os.O_CLOEXEC, 0, _RESOLVE_NO_SYMLINKS)
    return _check_result(
        libc.syscall(
            _OPENAT2,
            _AT_FDCWD,
            ctypes.c_char_p(os.fsencode(path)),
            ctypes.byref(how),
            ctypes.sizeof(how),
        )
    )


def _find_protected_paths(
    libc: ctypes.CDLL, policy: OverallsLandlockPolicy
) -> list[tuple[Path, int]]:
    """Pin existing entries; fail closed on unsupported aliases and traversal errors."""
    protected: list[tuple[Path, int]] = []
    seen: set[tuple[int, int]] = set()
    names = {name.casefold() for name in policy.protected_names}
    roots = sorted(set((*policy.write_paths, *policy.protected_paths)))

    def visit(
        path: Path, *, protected_parent: bool = False, mount_root: bool = False
    ) -> None:
        descriptor = _open_path(libc, path)
        try:
            info = os.fstat(descriptor)
            identity = (info.st_dev, info.st_ino)
            if stat.S_ISDIR(info.st_mode):
                if identity in seen:
                    raise RuntimeError(
                        f"filesystem directory alias is unsupported: {path}"
                    )
                seen.add(identity)
                directory = _open_path(libc, path, directory=True)
                try:
                    if os.fstat(directory) != os.fstat(descriptor):
                        raise RuntimeError(f"directory changed while scanning: {path}")
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            child = path / entry.name
                            named = (
                                entry.name.casefold() in names
                                or child in policy.protected_paths
                            )
                            if entry.is_symlink():
                                if named or protected_parent:
                                    raise RuntimeError(
                                        f"protected symlink is unsupported: {child}"
                                    )
                                continue
                            visit(
                                child,
                                protected_parent=protected_parent or named,
                                mount_root=named and not protected_parent,
                            )
                finally:
                    os.close(directory)
            elif protected_parent:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RuntimeError(
                        f"protected special/hard-linked file is unsupported: {path}"
                    )
                if path.name.casefold() == ".git" and path != policy.git_marker:
                    raise RuntimeError(f"unresolved Git pointer is unsupported: {path}")
            if mount_root:
                protected.append((path, descriptor))
                descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    try:
        for root in roots:
            # A parent traversal already checks nested explicit grants.
            if any(root != parent and root.is_relative_to(parent) for parent in roots):
                continue
            visit(
                root,
                protected_parent=root in policy.protected_paths,
                mount_root=root in policy.protected_paths,
            )
        return protected
    except BaseException:
        for _, descriptor in protected:
            os.close(descriptor)
        raise


def _mount_readonly(libc: ctypes.CDLL, descriptor: int) -> None:
    # Clone and configure a detached mount, then attach using pinned descriptors.
    mount = _check_result(
        libc.syscall(
            _OPEN_TREE,
            descriptor,
            ctypes.c_char_p(b""),
            _OPEN_TREE_CLONE | os.O_CLOEXEC | _AT_EMPTY_PATH | _AT_RECURSIVE,
        )
    )
    try:
        attr = _MountAttr(
            _MOUNT_ATTR_RDONLY | _MOUNT_ATTR_NOSUID | _MOUNT_ATTR_NODEV, 0, 0, 0
        )
        _check_result(
            libc.syscall(
                _MOUNT_SETATTR,
                mount,
                ctypes.c_char_p(b""),
                _AT_EMPTY_PATH | _AT_RECURSIVE,
                ctypes.byref(attr),
                ctypes.sizeof(attr),
            )
        )
        _check_result(
            libc.syscall(
                _MOVE_MOUNT,
                mount,
                ctypes.c_char_p(b""),
                descriptor,
                ctypes.c_char_p(b""),
                _MOVE_MOUNT_F_EMPTY_PATH | _MOVE_MOUNT_T_EMPTY_PATH,
            )
        )
    finally:
        os.close(mount)


def _create_namespaces(libc: ctypes.CDLL, *, network_access: bool) -> None:
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        raise RuntimeError("Linux sandbox requires an unprivileged caller")
    # Offline commands get a fresh network stack; socketpair IPC remains usable.
    namespaces = _CLONE_NEWUSER | _CLONE_NEWNS | _CLONE_NEWIPC
    if not network_access:
        namespaces |= _CLONE_NEWNET
    _check_result(libc.unshare(namespaces))
    Path("/proc/self/setgroups").write_text("deny")
    Path("/proc/self/uid_map").write_text(f"{uid} {uid} 1")
    Path("/proc/self/gid_map").write_text(f"{gid} {gid} 1")
    # Private propagation prevents host mounts entering this namespace.
    _check_result(
        libc.mount(None, ctypes.c_char_p(b"/"), None, _MS_PRIVATE | _MS_REC, None)
    )


def _protect_path(libc: ctypes.CDLL, path: Path, descriptor: int) -> None:
    """Attach a read-only mount, checking its identity before and after setup."""
    current = _open_path(libc, path)
    try:
        if os.fstat(current) != os.fstat(descriptor):
            raise RuntimeError(f"protected path changed while scanning: {path}")
    finally:
        os.close(current)
    _mount_readonly(libc, descriptor)
    current = _open_path(libc, path)
    try:
        if (
            os.fstat(current) != os.fstat(descriptor)
            or not os.fstatvfs(current).f_flag & os.ST_RDONLY
        ):
            raise RuntimeError(f"read-only mount verification failed: {path}")
    finally:
        os.close(current)


def _protect_paths(libc: ctypes.CDLL, policy: OverallsLandlockPolicy) -> None:
    paths = _find_protected_paths(libc, policy)
    try:
        for path, descriptor in paths:
            _protect_path(libc, path, descriptor)
    finally:
        for _, descriptor in paths:
            os.close(descriptor)


def _prepare_mount_probe(libc: ctypes.CDLL, temporary_path: Path) -> Path:
    probe = Path(tempfile.mkdtemp(prefix="mount-probe-", dir=temporary_path))
    (probe / "protected").mkdir()
    (probe / "protected" / "sentinel").write_text("sentinel")
    descriptor = _open_path(libc, probe / "protected")
    try:
        _mount_readonly(libc, descriptor)
    finally:
        os.close(descriptor)
    return probe


def _drop_capabilities(libc: ctypes.CDLL) -> None:
    header = _CapabilityHeader(_CAPABILITY_VERSION_3, 0)
    capabilities = (_CapabilityData * 2)()
    _check_result(libc.capset(ctypes.byref(header), ctypes.byref(capabilities)))


def _prepare_isolation(libc: ctypes.CDLL, policy: OverallsLandlockPolicy) -> Path:
    if policy.temporary_path is None:
        raise RuntimeError("Linux mount policy requires a private temporary directory")
    _create_namespaces(libc, network_access=policy.network_access)
    _protect_paths(libc, policy)
    probe = _prepare_mount_probe(libc, policy.temporary_path)
    _drop_capabilities(libc)
    return probe


def _verify_mounts(probe: Path) -> None:
    (probe / "writable").write_text("allowed")
    sentinel = probe / "protected" / "sentinel"
    if sentinel.read_text() != "sentinel":
        raise RuntimeError("mount preflight sentinel changed")
    for operation in (
        lambda: sentinel.write_text("denied"),
        lambda: sentinel.chmod(0o600),
        sentinel.unlink,
        lambda: (probe / "protected" / "new").write_text("denied"),
    ):
        try:
            operation()
        except OSError as error:
            if error.errno != errno.EROFS:
                raise
        else:
            raise RuntimeError("read-only mount preflight unexpectedly allowed a write")


def _add_path_rule(libc: ctypes.CDLL, ruleset: int, path: Path, rights: int) -> None:
    # Canonical grants must not be redirected by symlinks between commands.
    descriptor = _open_path(libc, path)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            rights &= _FILE_READ | _FILE_WRITE
        rule = _PathAttr(rights, descriptor)
        _check_result(
            libc.syscall(_ADD_RULE, ruleset, _RULE_PATH_BENEATH, ctypes.byref(rule), 0)
        )
    finally:
        os.close(descriptor)


def _enforce_landlock(libc: ctypes.CDLL, policy: OverallsLandlockPolicy) -> None:
    attr = _RulesetAttr(_HANDLED_FS, 0, _SCOPE_SIGNAL | _SCOPE_ABSTRACT_UNIX_SOCKET)
    ruleset = _check_result(
        libc.syscall(_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    )
    try:
        for paths, rights in (
            (policy.read_paths, _READ),
            (policy.write_paths, _READ | _WRITE),
            ((Path("/dev/null"), Path("/dev/zero")), _DEVICE_ACCESS),
        ):
            for path in paths:
                _add_path_rule(libc, ruleset, path, rights)
        _check_result(libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0))
        _check_result(libc.syscall(_RESTRICT_SELF, ruleset, 0))
    finally:
        os.close(ruleset)


def _enforce_seccomp(
    libc: ctypes.CDLL, syscalls: _OverallsSyscalls, *, network_access: bool
) -> None:
    instructions = _build_seccomp_filter(syscalls, network_access=network_access)
    filters = (_Filter * len(instructions))(*(_Filter(*item) for item in instructions))
    program = _Program(len(filters), filters)
    _check_result(
        libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(program), 0, 0)
    )


def _restrict(policy: OverallsLandlockPolicy) -> None:
    syscalls = _get_syscalls(platform.machine())
    libc = ctypes.CDLL(None, use_errno=True)
    # Explicit result width matters for variadic syscall(2) on 64-bit hosts.
    libc.syscall.restype = ctypes.c_long
    abi = _check_result(libc.syscall(_CREATE_RULESET, 0, 0, _CREATE_RULESET_VERSION))
    if abi < _MIN_ABI:
        raise RuntimeError(f"Landlock ABI {_MIN_ABI}+ required; kernel offers {abi}")

    # Setup needs capabilities and mount access; verification runs fully confined.
    probe = _prepare_isolation(libc, policy)
    _enforce_landlock(libc, policy)
    _enforce_seccomp(libc, syscalls, network_access=policy.network_access)
    _verify_mounts(probe)


def _main() -> int:
    try:
        policy = OverallsLandlockPolicy.deserialize(sys.argv[1])
        _restrict(policy)
        target_env = json.loads(os.environ.pop("_BUB_OVERALLS_TARGET_ENV"))
        os.execvpe(sys.argv[2], sys.argv[2:], target_env)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Landlock/seccomp launcher failed: {error}", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(_main())
