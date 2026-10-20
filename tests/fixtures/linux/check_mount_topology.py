"""Trusted setup harness: create test mounts before starting the actual sandbox."""

import argparse
import asyncio
import ctypes
import os
from pathlib import Path

from bub_overalls import OverallsConfig, OverallsEnvironment


def _prepare_mounts(workspace: Path, layout: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    uid, gid = os.getuid(), os.getgid()
    assert libc.unshare(0x10000000 | 0x20000) == 0, ctypes.get_errno()
    Path("/proc/self/setgroups").write_text("deny")
    Path("/proc/self/uid_map").write_text(f"{uid} {uid} 1")
    Path("/proc/self/gid_map").write_text(f"{gid} {gid} 1")
    assert libc.mount(None, b"/", None, 0x40000 | 0x4000, None) == 0
    protected = workspace / ".ssh"
    if layout == "nested-mount":
        target = protected / "nested"
        assert (
            libc.mount(b"tmpfs", os.fsencode(target), b"tmpfs", 0, b"size=1m") == 0
        ), ctypes.get_errno()
        (target / "sentinel").write_text("original")
    else:
        assert (
            libc.mount(
                os.fsencode(protected),
                os.fsencode(workspace / "alias"),
                None,
                4096,
                None,
            )
            == 0
        )


async def _check_sandbox(workspace: Path, layout: str) -> None:
    try:
        async with OverallsEnvironment(OverallsConfig(), workspace) as environment:
            assert layout == "nested-mount", "bind alias should fail preflight"
            assert await environment.read_text(".ssh/nested/sentinel") == "original"
            try:
                await environment.write_text(".ssh/nested/sentinel", "denied")
            except OSError:
                pass
            else:
                raise AssertionError("nested mount remained writable")
    except RuntimeError as error:
        assert layout == "bind-alias" and "directory alias" in str(error), str(error)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("layout", choices=("nested-mount", "bind-alias"))
    args = parser.parse_args()
    _prepare_mounts(args.workspace, args.layout)
    asyncio.run(_check_sandbox(args.workspace, args.layout))


if __name__ == "__main__":
    main()
