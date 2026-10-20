"""Verify content, metadata and directory mutations of protected paths fail."""

import argparse
import errno
import os
from collections.abc import Callable
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sentinel", type=Path)
    args = parser.parse_args()
    path: Path = args.sentinel
    directory = path.parent
    operations: tuple[Callable[[], object], ...] = (
        lambda: path.write_text("denied"),
        lambda: os.truncate(path, 0),
        path.unlink,
        lambda: path.rename(directory / "renamed"),
        lambda: path.chmod(0o600),
        lambda: os.utime(path, None),
        lambda: (directory / "new").write_text("denied"),
        (directory / "newdir").mkdir,
        lambda: directory.rename(directory.with_name("moved")),
    )
    for operation in operations:
        try:
            operation()
        except OSError as error:
            assert error.errno in (
                errno.EROFS,
                errno.EACCES,
                errno.EPERM,
                errno.EBUSY,
            ), error
        else:
            raise AssertionError("protected operation allowed")


if __name__ == "__main__":
    main()
