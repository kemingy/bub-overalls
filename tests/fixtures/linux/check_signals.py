"""Check host signal denial and asyncio; optionally wait for lifecycle cleanup."""

import argparse
import asyncio
import errno
import os
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host_pid", type=int)
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    # Signal 0 checks permission without delivering a signal to the host runner.
    try:
        os.kill(args.host_pid, 0)
    except OSError as error:
        assert error.errno in (errno.EPERM, errno.EACCES), error
    else:
        raise AssertionError("host signal allowed")
    asyncio.run(asyncio.sleep(0))
    if args.wait:
        time.sleep(60)


if __name__ == "__main__":
    main()
