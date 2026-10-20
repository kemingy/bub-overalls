"""Verify setup capabilities are gone and modern mount syscalls are blocked."""

import ctypes
import errno


def main() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    header = (ctypes.c_uint32 * 2)(0x20080522, 0)
    data = (ctypes.c_uint32 * 6)()
    assert libc.capget(header, data) == 0
    assert not any(data)
    for number in (428, 429, 430, 431, 432, 433, 442):
        assert libc.syscall(number, 0, 0, 0, 0, 0) == -1
        assert ctypes.get_errno() == errno.EPERM


if __name__ == "__main__":
    main()
