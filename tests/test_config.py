import json
import subprocess
from pathlib import Path

import pytest

from bub_overalls.config import OverallsConfig
from bub_overalls.profile import build_overalls_profile


@pytest.mark.parametrize(
    "value", ['"/tmp"', "{}", "[1]", "not json", "", '[""]', '["  "]']
)
def test_grants_require_json_path_arrays(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUB_OVERALLS_READ_PATHS", value)
    with pytest.raises(ValueError, match="JSON array"):
        OverallsConfig.from_env()


def test_explicit_grants_resolve_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setenv("BUB_OVERALLS_READ_PATHS", json.dumps([str(link)]))
    assert OverallsConfig.from_env().read_paths == (tmp_path.resolve(),)


def test_profile_escapes_read_paths_and_keeps_network_denied(tmp_path: Path) -> None:
    reference = tmp_path / 'reference"name'
    reference.mkdir()
    profile = build_overalls_profile(
        OverallsConfig(read_paths=(reference,)),
        tmp_path / "workspace",
        tmp_path / "temporary",
    )
    assert f"(subpath {json.dumps(str(reference.resolve()))})" in profile
    assert "(deny default)" in profile
    assert "allow network" not in profile
    assert "(allow signal (target same-sandbox))" in profile


def test_profile_reads_only_active_xcode_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "Relocated Xcode.app"
    developer = app / "Contents" / "Developer"
    shared_frameworks = app / "Contents" / "SharedFrameworks"
    frameworks = app / "Contents" / "Frameworks"
    developer.mkdir(parents=True)
    shared_frameworks.mkdir()
    frameworks.mkdir()

    def select(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"{app}\n", stderr=""
        )

    monkeypatch.setattr("bub_overalls.profile.subprocess.run", select)
    monkeypatch.setattr("bub_overalls.profile.sys.platform", "darwin")
    profile = build_overalls_profile(
        OverallsConfig(), tmp_path / "workspace", tmp_path / "temporary"
    )
    for path in (developer, shared_frameworks, frameworks):
        expected = f"(allow file-read* (subpath {json.dumps(str(path.resolve()))}))"
        assert expected in profile
    assert '(allow file-read* (subpath "/Applications"))' not in profile
    assert "Xcode[^/]" not in profile


@pytest.mark.parametrize("character", ['"', "\\", "\n", "\r", "\t", "\x1b"])
@pytest.mark.parametrize("root_kind", ["workspace", "write_grant", "temporary"])
def test_profile_rejects_unsupported_write_root_characters(
    tmp_path: Path, character: str, root_kind: str
) -> None:
    unsupported = tmp_path / f"unsupported{character}name"
    unsupported.mkdir()
    config = OverallsConfig(
        write_paths=(unsupported,) if root_kind == "write_grant" else ()
    )
    with pytest.raises(ValueError, match="unsupported character in write root"):
        build_overalls_profile(
            config,
            unsupported if root_kind == "workspace" else tmp_path / "workspace",
            unsupported if root_kind == "temporary" else tmp_path / "temporary",
        )


def test_network_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUB_OVERALLS_NETWORK_ACCESS", raising=False)
    assert not OverallsConfig().network_access
    assert not OverallsConfig.from_env().network_access


@pytest.mark.parametrize(
    "value, expected",
    [
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        (" TRUE ", True),
        (" Yes ", True),
        (" FALSE ", False),
        (" Off ", False),
    ],
)
def test_network_setting_from_environment(
    value: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUB_OVERALLS_NETWORK_ACCESS", value)
    assert OverallsConfig.from_env().network_access is expected
    assert (
        OverallsConfig.from_env(network_access=not expected).network_access
        is not expected
    )


@pytest.mark.parametrize("value", ["", "   ", "enabled", "typo", "2"])
def test_invalid_network_setting_fails_closed(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUB_OVERALLS_NETWORK_ACCESS", value)
    with pytest.raises(ValueError, match="BUB_OVERALLS_NETWORK_ACCESS must be"):
        OverallsConfig.from_env()


def test_network_requires_boolean_in_python() -> None:
    with pytest.raises(TypeError, match="must be a boolean"):
        OverallsConfig(network_access="false")  # type: ignore[arg-type]


def test_network_opt_in_preserves_default_deny() -> None:
    profile = build_overalls_profile(
        OverallsConfig(network_access=True),
        Path("/workspace"),
        Path("/private/tmp/session"),
    )
    assert "(deny default)" in profile
    assert '(allow network-outbound (remote ip "*:*"))' in profile
    assert 'path-literal "/private/var/run/mDNSResponder"' in profile
    assert "allow network-inbound" not in profile
    assert "allow mach-lookup" not in profile


@pytest.mark.parametrize("field", ["read_paths", "write_paths"])
def test_relative_grants_are_resolved_once(
    field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "grants"
    directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    paths = (Path("link"),)
    config = (
        OverallsConfig(read_paths=paths)
        if field == "read_paths"
        else OverallsConfig(write_paths=paths)
    )
    monkeypatch.setenv(f"BUB_OVERALLS_{field.upper()}", '["./link"]')
    from_env = OverallsConfig.from_env()
    monkeypatch.chdir(directory)
    assert getattr(config, field) == (directory.resolve(),)
    assert getattr(from_env, field) == (directory.resolve(),)


@pytest.mark.parametrize("field", ["read_paths", "write_paths"])
def test_home_relative_grants(
    field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "cache"
    directory.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(f"BUB_OVERALLS_{field.upper()}", '["~/cache"]')
    paths = (Path("~/cache"),)
    config = (
        OverallsConfig(read_paths=paths)
        if field == "read_paths"
        else OverallsConfig(write_paths=paths)
    )
    assert getattr(config, field) == (directory.resolve(),)
    assert getattr(OverallsConfig.from_env(), field) == (directory.resolve(),)


@pytest.mark.parametrize("field", ["read_paths", "write_paths"])
def test_missing_grant_reports_setting_and_path(
    field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(f"BUB_OVERALLS_{field.upper()}", '["missing"]')
    with pytest.raises(ValueError, match=f"BUB_OVERALLS_{field.upper()}.*missing"):
        OverallsConfig.from_env()


def test_deleted_grant_is_rejected_before_initialization(tmp_path: Path) -> None:
    directory = tmp_path / "grant"
    directory.mkdir()
    config = OverallsConfig(read_paths=(directory,))
    directory.rmdir()
    with pytest.raises(ValueError, match="no longer exists"):
        config.validate()


@pytest.mark.parametrize(
    "value, expected",
    [("true", True), (" FALSE ", False), (" YES ", True), ("0", False)],
)
def test_git_access_environment(
    value: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUB_OVERALLS_GIT_ACCESS", value)
    assert OverallsConfig.from_env().git_access is expected
    assert not OverallsConfig.from_env().network_access


@pytest.mark.parametrize("value", ["", "   ", "enabled", "typo", "2"])
def test_invalid_git_access_is_rejected(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUB_OVERALLS_GIT_ACCESS", value)
    with pytest.raises(ValueError, match="BUB_OVERALLS_GIT_ACCESS must be"):
        OverallsConfig.from_env()


def test_git_access_defaults_to_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUB_OVERALLS_GIT_ACCESS", raising=False)
    assert not OverallsConfig().git_access
    assert not OverallsConfig.from_env().git_access
    with pytest.raises(TypeError, match="git_access must be a boolean"):
        OverallsConfig(git_access="true")  # type: ignore[arg-type]
