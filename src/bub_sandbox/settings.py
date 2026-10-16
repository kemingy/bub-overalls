"""Configuration is validated at environment creation, not plugin import."""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SandboxSettings:
    helper: Path
    image: Path
    cpus: int = 2
    memory_mib: int = 1024
    boot_timeout: float = 30.0
    import_workspace: bool = True

    @classmethod
    def from_env(cls) -> SandboxSettings:
        return cls(
            helper=Path(os.environ.get("BUB_SANDBOX_HELPER", "build/bub-krun")).resolve(),
            image=Path(os.environ.get("BUB_SANDBOX_IMAGE", "build/guest.raw")).resolve(),
            cpus=int(os.environ.get("BUB_SANDBOX_CPUS", "2")),
            memory_mib=int(os.environ.get("BUB_SANDBOX_MEMORY_MIB", "1024")),
            boot_timeout=float(os.environ.get("BUB_SANDBOX_BOOT_TIMEOUT", "30")),
            import_workspace=os.environ.get("BUB_SANDBOX_IMPORT_WORKSPACE", "1") == "1",
        )

    def validate(self) -> None:
        system, machine = platform.system(), platform.machine()
        if system not in ("Darwin", "Linux") or machine not in ("arm64", "aarch64", "x86_64"):
            raise RuntimeError("libkrun needs Linux x86-64/ARM64 or macOS Apple Silicon")
        if system == "Darwin" and machine != "arm64":
            raise RuntimeError("libkrun does not support Intel Macs")
        if not 1 <= self.cpus <= 64 or not 128 <= self.memory_mib <= 65536 or not 0 < self.boot_timeout <= 300:
            raise ValueError("expected CPUs 1..64, memory 128..65536 MiB, boot timeout (0, 300] seconds")
        if not self.helper.is_file() or not os.access(self.helper, os.X_OK):
            raise FileNotFoundError(f"libkrun helper missing or not executable: {self.helper}; run make native")
        if not self.image.is_file():
            raise FileNotFoundError(f"guest image missing: {self.image}; run make image")
        manifest = json.loads(self.image.with_suffix(".json").read_text())
        expected = "arm64" if machine in ("arm64", "aarch64") else "amd64"
        if manifest.get("architecture") != expected or manifest.get("protocol") != 1:
            raise ValueError("guest image architecture or protocol does not match this host")
        if system == "Linux" and not os.access("/dev/kvm", os.R_OK | os.W_OK):
            raise RuntimeError("read/write access to /dev/kvm is required")
