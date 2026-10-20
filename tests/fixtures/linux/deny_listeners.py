"""Verify Unix connections and explicit IP listeners are denied."""

import argparse
import errno
import socket


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unix_address")
    args = parser.parse_args()
    for family in (socket.AF_UNIX, socket.AF_INET):
        try:
            with socket.socket(family) as connection:
                if family == socket.AF_UNIX:
                    connection.connect(args.unix_address)
                else:
                    connection.bind(("127.0.0.1", 0))
        except OSError as error:
            assert error.errno in (errno.EPERM, errno.EACCES), error
        else:
            raise AssertionError("operation unexpectedly allowed")


if __name__ == "__main__":
    main()
