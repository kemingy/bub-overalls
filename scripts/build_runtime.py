"""Build a private, headless libkrun 1.19.6 runtime without a system package manager."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "build/sources"
PREFIX = ROOT / "build/runtime"
VERSION = "1.19.6"
SOURCE_URL = f"https://github.com/libkrun/libkrun/archive/refs/tags/v{VERSION}.tar.gz"
SOURCE_SHA = "7025d72208172dc06f791ad5af7ccaafeabdac9869f74ccb45fb6d4f6e5991cd"
FW_URL = "https://github.com/containers/libkrunfw/releases/download/v5.6.2/libkrunfw-prebuilt-aarch64.tgz"
FW_SHA = "adef6739cc3bda2a1b57f04c08ccbfdf206a6c831d76b32de1beddc0e7984e86"


def fetch(url: str, checksum: str, name: str) -> Path:
    destination = SOURCES / name
    if not destination.exists():
        partial = destination.with_suffix(".download")
        with urllib.request.urlopen(url, timeout=90) as response, partial.open("wb") as target:
            shutil.copyfileobj(response, target)
        partial.replace(destination)
    with destination.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != checksum:
            raise RuntimeError(f"checksum mismatch: {destination}")
    return destination


def run(*args: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


def main() -> None:
    system, machine = platform.system(), platform.machine()
    if (
        system not in ("Darwin", "Linux")
        or machine not in ("arm64", "aarch64", "x86_64")
        or (system == "Darwin" and machine != "arm64")
    ):
        raise RuntimeError("supported hosts: macOS Apple Silicon and Linux x86-64/ARM64")
    SOURCES.mkdir(parents=True, exist_ok=True)
    (PREFIX / "lib").mkdir(parents=True, exist_ok=True)
    (PREFIX / "include").mkdir(parents=True, exist_ok=True)
    source = SOURCES / f"libkrun-{VERSION}"
    if not source.exists():
        with tarfile.open(fetch(SOURCE_URL, SOURCE_SHA, "libkrun.tar.gz")) as archive:
            archive.extractall(SOURCES, filter="data")

    if system == "Darwin":
        firmware = SOURCES / "libkrunfw"
        if not firmware.exists():
            with tarfile.open(fetch(FW_URL, FW_SHA, "libkrunfw.tgz")) as archive:
                archive.extractall(SOURCES, filter="data")
        # The archive contains the bundled Linux kernel as C data, compiled by Apple cc.
        # Building the kernel itself or installing LLVM on macOS is unnecessary.
        run(
            "cc",
            "-fPIC",
            "-DABI_VERSION=5",
            "-shared",
            "-o",
            str(PREFIX / "lib/libkrunfw.5.dylib"),
            str(firmware / "kernel.c"),
        )
        run("install_name_tool", "-id", "@rpath/libkrunfw.5.dylib", str(PREFIX / "lib/libkrunfw.5.dylib"))
    else:
        arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
        checksums = {
            "aarch64": "dea7905a167eee17d482200ea2fe15871aceb293b0674ac825ed7ae69759399f",
            "x86_64": "016c32ddb2a28aa300382cab33352d63a41a901eb36ee8d1ca24b389791c8b91",
        }
        url = f"https://github.com/libkrun/libkrunfw/releases/download/v5.6.2/libkrunfw-{arch}.tgz"
        firmware = SOURCES / f"firmware-linux-{arch}"
        firmware.mkdir(exist_ok=True)
        with tarfile.open(fetch(url, checksums[arch], f"libkrunfw-linux-{arch}.tgz")) as archive:
            archive.extractall(firmware, filter="data")
        shutil.copy2(next(firmware.rglob("libkrunfw.so.5.6.2")), PREFIX / "lib/libkrunfw.so.5")

    # Compile the tiny Linux init using Docker, avoiding a macOS cross compiler.
    init_context = SOURCES / "init-build"
    init_context.mkdir(exist_ok=True)
    for path in (source / "src/init_blob/init").iterdir():
        if path.is_file():
            shutil.copy2(path, init_context / path.name)
    (init_context / "Dockerfile").write_text(
        "FROM debian:bookworm-slim\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev linux-libc-dev\n"
        "COPY . /src\nRUN cc -O2 -static -Wall -o /init /src/init.c /src/dhcp.c\n"
    )
    architecture = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    run("docker", "build", "--platform", f"linux/{architecture}", "-t", "bub-sandbox-init:dev", str(init_context))
    container = subprocess.check_output(["docker", "create", "bub-sandbox-init:dev"], text=True).strip()
    init_binary = SOURCES / "init"
    try:
        run("docker", "cp", f"{container}:/init", str(init_binary))
    finally:
        run("docker", "rm", "-f", container)
    environment = os.environ | {"KRUN_INIT_BINARY_PATH": str(init_binary)}
    if system == "Linux":
        # dlopen() originates in libkrun, so the library needs its own firmware search path.
        environment["RUSTFLAGS"] = environment.get("RUSTFLAGS", "") + " -C link-arg=-Wl,-rpath,$ORIGIN"
    run("cargo", "build", "--locked", "--release", "-p", "libkrun", "--features", "blk", cwd=source, env=environment)
    library = "libkrun.dylib" if system == "Darwin" else "libkrun.so"
    shutil.copy2(source / "target/release" / library, PREFIX / "lib" / library)
    shutil.copy2(source / "include/libkrun.h", PREFIX / "include/libkrun.h")
    if system == "Darwin":
        run("install_name_tool", "-id", "@rpath/libkrun.dylib", str(PREFIX / "lib/libkrun.dylib"))
        run("install_name_tool", "-add_rpath", "@loader_path", str(PREFIX / "lib/libkrun.dylib"))
        for path in (PREFIX / "lib").glob("*.dylib"):
            run("codesign", "--force", "--sign", "-", str(path))
    else:
        link = PREFIX / "lib/libkrun.so.1"
        link.unlink(missing_ok=True)
        link.symlink_to("libkrun.so")
    print(f"Private libkrun runtime: {PREFIX}")


if __name__ == "__main__":
    main()
