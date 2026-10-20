"""Attempt a real IP connection and send; denied access exits with status 23."""

import argparse
import errno
import json
import socket


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("family", type=int)
    parser.add_argument("kind", type=int)
    parser.add_argument("address", help="JSON socket address")
    args = parser.parse_args()
    address = tuple(json.loads(args.address))
    try:
        with socket.socket(args.family, args.kind) as connection:
            connection.settimeout(2)
            connection.connect(address)
            connection.send(b"x")
    except OSError as error:
        assert error.errno in (errno.EPERM, errno.EACCES), error
        return 23
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
