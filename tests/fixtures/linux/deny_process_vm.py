"""Verify cross-process memory syscalls are denied before address validation."""

import ctypes
import errno
import os
import platform


class _IOVec(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("length", ctypes.c_size_t)]


def main() -> None:
    numbers = {"x86_64": (310, 311), "aarch64": (270, 271)}
    try:
        read_number, write_number = numbers[platform.machine()]
    except KeyError as error:
        raise AssertionError("unsupported architecture") from error
    local_data = ctypes.create_string_buffer(1)
    local = _IOVec(ctypes.cast(local_data, ctypes.c_void_p), 1)
    remote = _IOVec(ctypes.c_void_p(1), 1)
    libc = ctypes.CDLL(None, use_errno=True)
    for number in (read_number, write_number):
        result = libc.syscall(
            number,
            os.getppid(),
            ctypes.byref(local),
            1,
            ctypes.byref(remote),
            1,
        )
        assert result == -1, result
        assert ctypes.get_errno() == errno.EPERM, ctypes.get_errno()


if __name__ == "__main__":
    main()
