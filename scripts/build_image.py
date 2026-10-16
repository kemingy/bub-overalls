"""Build a native-architecture ext4 guest with Docker; no privileged container or host mount."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=int, default=2048)
    parser.add_argument("--output", type=Path, default=ROOT / "build/guest.raw")
    args = parser.parse_args()
    if args.size_mib < 512:
        parser.error("--size-mib must be at least 512")
    architecture = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    if architecture is None:
        parser.error("only ARM64 and x86-64 hosts are supported")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    run("docker", "build", "--platform", f"linux/{architecture}", "-t", "bub-sandbox-guest:dev", str(ROOT / "guest"))
    # mkfs and ownership-sensitive extraction happen inside Linux, not on APFS.
    # docker cp streams the completed image out without granting host filesystem access.
    script = """set -eu
apt-get update >&2
apt-get install -y --no-install-recommends e2fsprogs >&2
mkdir /rootfs
tar -xpf /tmp/rootfs.tar -C /rootfs
truncate -s "$IMAGE_MIB"M /guest.raw
mkfs.ext4 -q -F -d /rootfs /guest.raw
"""
    with tempfile.TemporaryDirectory(prefix="bub-image-") as temp:
        export_id = subprocess.check_output(["docker", "create", "bub-sandbox-guest:dev"], text=True).strip()
        builder_id = ""
        try:
            archive = Path(temp) / "rootfs.tar"
            run("docker", "export", "--output", str(archive), export_id)
            builder_id = subprocess.check_output(
                ["docker", "create", "-e", f"IMAGE_MIB={args.size_mib}", "debian:bookworm-slim", "sh", "-c", script],
                text=True,
            ).strip()
            run("docker", "cp", str(archive), f"{builder_id}:/tmp/rootfs.tar")
            run("docker", "start", "--attach", builder_id)
            status = subprocess.check_output(
                ["docker", "inspect", "--format", "{{.State.ExitCode}}", builder_id], text=True
            )
            if int(status) != 0:
                raise RuntimeError("guest filesystem builder failed")
            temporary_image = args.output.with_suffix(".raw.tmp")
            run("docker", "cp", f"{builder_id}:/guest.raw", str(temporary_image))
            temporary_image.chmod(0o600)
            temporary_image.replace(args.output)
        finally:
            for container_id in (builder_id, export_id):
                if container_id:
                    run("docker", "rm", "-f", container_id)
    manifest = {"architecture": architecture, "libkrun": "1.19.6", "protocol": 1}
    args.output.with_suffix(".json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
