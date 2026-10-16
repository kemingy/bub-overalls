from __future__ import annotations

import json
from pathlib import Path

import pytest

from bub_sandbox.settings import SandboxSettings


def test_rejects_cross_architecture_image_before_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = tmp_path / "helper"
    helper.write_text("unused")
    helper.chmod(0o700)
    image = tmp_path / "guest.raw"
    image.touch()
    image.with_suffix(".json").write_text(json.dumps({"architecture": "amd64", "protocol": 1}))
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    with pytest.raises(ValueError, match="architecture"):
        SandboxSettings(helper, image).validate()


def test_rejects_intel_macs_before_loading_libkrun(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    with pytest.raises(RuntimeError, match="Intel"):
        SandboxSettings(Path("unused"), Path("unused")).validate()


@pytest.mark.parametrize("cpus,memory", [(0, 1024), (256, 1024), (2, 0), (2, 65537)])
def test_rejects_invalid_resource_limits(cpus: int, memory: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    with pytest.raises(ValueError, match="CPUs"):
        SandboxSettings(Path("unused"), Path("unused"), cpus, memory).validate()
