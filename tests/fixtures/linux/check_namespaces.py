"""Check namespace inheritance and queue isolation; no arguments reports identity."""

import argparse
import ctypes
import errno
import json
import os
import subprocess
import sys


def _read_namespaces() -> dict[str, str]:
    return {name: os.readlink(f"/proc/self/ns/{name}") for name in ("ipc", "net")}


def _check_message_queues(host_queue: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    message = (ctypes.c_long * 2)(1, 99)
    size = ctypes.sizeof(ctypes.c_long)
    assert libc.msgrcv(host_queue, ctypes.byref(message), size, 0, 0o4000) == -1
    assert ctypes.get_errno() == errno.EINVAL, ctypes.get_errno()
    local = libc.msgget(0, 0o600)  # IPC_PRIVATE
    assert local >= 0, ctypes.get_errno()
    try:
        assert libc.msgsnd(local, ctypes.byref(message), size, 0) == 0
        assert libc.msgrcv(local, ctypes.byref(message), size, 0, 0o4000) == size
        assert message[1] == 99
    finally:
        assert libc.msgctl(local, 0, None) == 0


def main() -> None:
    current = _read_namespaces()
    if len(sys.argv) == 1:
        print(json.dumps(current))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "host_namespaces", help="JSON object containing ipc/net identity"
    )
    parser.add_argument("host_queue", type=int)
    parser.add_argument("network_access", choices=("enabled", "disabled"))
    args = parser.parse_args()
    host = json.loads(args.host_namespaces)
    assert current["ipc"] != host["ipc"], current
    assert (current["net"] == host["net"]) == (args.network_access == "enabled"), (
        current
    )
    child = subprocess.check_output([sys.executable, "-I", __file__], timeout=10)
    assert json.loads(child) == current, child
    _check_message_queues(args.host_queue)


if __name__ == "__main__":
    main()
