import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from bub.builtin.environment import LocalEnvironment
from bub.environment import ENVIRONMENT_STATE_KEY
from bub.framework import BubFramework
from bub.hooks.interception import LlmCallRequest

from bub_overalls import OverallsConfig, OverallsEnvironment, plugin


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for suffix in (
        "SESSIONS",
        "READ_PATHS",
        "WRITE_PATHS",
        "NETWORK_ACCESS",
        "GIT_ACCESS",
    ):
        monkeypatch.delenv(f"BUB_OVERALLS_{suffix}", raising=False)


@pytest.mark.parametrize(
    "patterns, session_id, expected",
    [
        (("*",), "cli_session", True),
        (("*",), "cli:local", True),
        (("telegram:*",), "telegram:123", True),
        (("telegram:*",), "cli:default", False),
        (("cli:*", "telegram:*"), "telegram:123", True),
        (("cli:dev?",), "cli:dev1", True),
        (("CLI:*",), "cli:default", False),
        ((), "cli:default", False),
    ],
)
def test_session_patterns(
    patterns: tuple[str, ...], session_id: str, expected: bool
) -> None:
    assert OverallsConfig(sessions=patterns).matches(session_id) is expected


def test_defaults_sandbox_every_session_without_network() -> None:
    config = OverallsConfig.from_env()
    assert config.matches("cli_session")
    assert config.matches("cli:local")
    assert config.matches("telegram:123")
    assert config == OverallsConfig()


def test_plugin_uses_environment_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant = tmp_path / "grant"
    grant.mkdir()
    (tmp_path / "link").symlink_to(grant, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BUB_OVERALLS_SESSIONS", '["cli:*"]')
    monkeypatch.setenv("BUB_OVERALLS_READ_PATHS", '["./link"]')
    monkeypatch.setenv("BUB_OVERALLS_WRITE_PATHS", '["~/grant"]')
    monkeypatch.setenv("BUB_OVERALLS_NETWORK_ACCESS", " TRUE ")
    assert OverallsConfig.from_env() == OverallsConfig(
        read_paths=(grant,),
        write_paths=(grant,),
        network_access=True,
        sessions=("cli:*",),
    )


@pytest.mark.parametrize(
    "value", ['"cli:*"', "{}", "[1]", "not json", "", '[""]', '["  "]']
)
def test_session_environment_requires_nonempty_string_arrays(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUB_OVERALLS_SESSIONS", value)
    with pytest.raises(ValueError, match="BUB_OVERALLS_SESSIONS must be a JSON array"):
        OverallsConfig.from_env()


def test_python_session_patterns_are_validated() -> None:
    with pytest.raises(
        ValueError, match="sessions must be a tuple of nonempty strings"
    ):
        OverallsConfig(sessions=("",))


async def test_plugin_preflights_selected_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("BUB_OVERALLS_NETWORK_ACCESS", "true")
    environment = Mock(spec=OverallsEnvironment)
    create = AsyncMock(return_value=environment)
    monkeypatch.setattr(OverallsEnvironment, "create", create)
    caplog.set_level(logging.INFO, logger=plugin.__name__)
    assert await plugin.provide_environment("cli:default", tmp_path) is environment
    create.assert_awaited_once_with(OverallsConfig(network_access=True), tmp_path)
    assert caplog.record_tuples == [
        (
            plugin.__name__,
            logging.INFO,
            "Seatbelt initialized for session cli:default "
            "(network enabled; 0 read grants, 0 write grants).",
        )
    ]


@pytest.mark.parametrize("patterns", ["[]", '["telegram:*"]'])
async def test_explicitly_excluded_sessions_do_not_create_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patterns: str
) -> None:
    monkeypatch.setenv("BUB_OVERALLS_SESSIONS", patterns)
    create = AsyncMock()
    monkeypatch.setattr(OverallsEnvironment, "create", create)
    assert await plugin.provide_environment("cli:default", tmp_path) is None
    create.assert_not_awaited()


async def test_plugin_preflight_failure_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create = AsyncMock(side_effect=PermissionError("Seatbelt unavailable"))
    monkeypatch.setattr(OverallsEnvironment, "create", create)
    with pytest.raises(PermissionError, match="Seatbelt unavailable"):
        await plugin.provide_environment("cli:default", tmp_path)


async def test_plugin_missing_grant_fails_before_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BUB_OVERALLS_READ_PATHS", '["./missing"]')
    create = AsyncMock()
    monkeypatch.setattr(OverallsEnvironment, "create", create)
    with pytest.raises(ValueError, match=r"read_paths.*missing"):
        await plugin.provide_environment("cli:default", tmp_path)
    create.assert_not_awaited()


async def test_plugin_invalid_network_setting_fails_before_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUB_OVERALLS_NETWORK_ACCESS", "enabled")
    create = AsyncMock()
    monkeypatch.setattr(OverallsEnvironment, "create", create)
    with pytest.raises(ValueError, match="BUB_OVERALLS_NETWORK_ACCESS must be"):
        await plugin.provide_environment("cli:default", tmp_path)
    create.assert_not_awaited()


@pytest.mark.sandbox
async def test_plugin_environment_grants_use_real_seatbelt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    grant = tmp_path / "grant"
    grant.mkdir()
    monkeypatch.setenv("BUB_OVERALLS_SESSIONS", '["cli:*"]')
    monkeypatch.setenv("BUB_OVERALLS_WRITE_PATHS", json.dumps([str(grant)]))
    environment = await plugin.provide_environment("cli:default", workspace)
    assert environment is not None
    try:
        await environment.write_text(str(grant / "allowed.txt"), "allowed")
        assert await environment.read_text(str(grant / "allowed.txt")) == "allowed"
        with pytest.raises(OSError, match="Operation not permitted"):
            await environment.write_text(str(tmp_path / "denied.txt"), "denied")
        assert not (tmp_path / "denied.txt").exists()
    finally:
        await environment.close()
    assert not environment.temporary_path.exists()


@pytest.mark.parametrize("network_access", [False, True])
@pytest.mark.parametrize("git_access", [False, True])
async def test_prompt_hook_describes_active_grants_without_changing_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network_access: bool,
    git_access: bool,
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    reference = tmp_path / 'reference "資料"'
    output = tmp_path / "output"
    reference.mkdir()
    output.mkdir()
    config = OverallsConfig(
        read_paths=(reference,),
        write_paths=(output,),
        network_access=network_access,
        git_access=git_access,
    )
    if git_access:
        (tmp_path / ".git" / "objects").mkdir(parents=True)
        (tmp_path / ".git" / "refs").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/agent\n")
    environment = OverallsEnvironment(config, tmp_path)
    # Launch variables may change, but the running environment's grants do not.
    monkeypatch.setenv("BUB_OVERALLS_READ_PATHS", '["/different"]')
    monkeypatch.setenv("BUB_OVERALLS_NETWORK_ACCESS", str(not network_access))
    framework = BubFramework(config_file=tmp_path / "missing.toml")
    framework.plugin_manager.register(plugin, name="overalls")
    messages = [
        {"role": "system", "content": "Host instructions"},
        {"role": "user", "content": "Read the reference"},
    ]
    request = LlmCallRequest(run_id="test", model="test:model", messages=messages)
    state = {ENVIRONMENT_STATE_KEY: environment}
    try:
        updated, decision = await framework.get_agent_hooks().before_llm_call(
            request, state
        )
        assert decision is None
        assert updated is not request
        assert updated.messages[1:] == messages
        assert len(messages) == 2
        assert request.messages is messages
        policy = updated.messages[0]
        assert policy["role"] == "system"
        content = policy["content"]
        grants = json.loads(content.splitlines()[1])
        assert grants == {
            "workspace": str(tmp_path.resolve()),
            "additional_read_paths": [str(reference.resolve())],
            "additional_write_paths": [str(output.resolve())],
            "temporary_directory": str(environment.temporary_path),
            "network_access": network_access,
            "git_access": git_access,
            "git_directory": str(tmp_path.resolve() / ".git") if git_access else None,
            "git_common_directory": str(tmp_path.resolve() / ".git")
            if git_access
            else None,
        }
        assert "BUB_OVERALLS_READ_PATHS" in content
        assert "BUB_OVERALLS_WRITE_PATHS" in content
        assert "BUB_OVERALLS_GIT_ACCESS" in content
        assert "same session ID" in content
        assert "a write grant does not unlock" in content
        assert "Linked worktrees" in content
        assert "config, layout pointers, and modules/ remain read-only" in content
        assert "Protected names below write roots" in content
        assert "including nested .git entries" in content
        assert "do not bypass the sandbox" in content
        assert "child environment variables cannot expand access" in content
        for protected in (".claude", ".cursor", ".vscode", ".idea", ".mcp.json"):
            assert protected in content
        # Passing through the hook twice must not accumulate policy messages.
        repeated, _ = await framework.get_agent_hooks().before_llm_call(updated, state)
        assert repeated is updated
    finally:
        await environment.close()


@pytest.mark.parametrize("environment", [None, LocalEnvironment()])
def test_prompt_hook_leaves_other_environments_unchanged(
    environment: LocalEnvironment | None,
) -> None:
    request = LlmCallRequest(
        run_id="test", model="test:model", messages=[{"role": "user", "content": "Hi"}]
    )
    assert plugin.before_llm_call(request, {ENVIRONMENT_STATE_KEY: environment}) is None
    assert request.messages == [{"role": "user", "content": "Hi"}]


@pytest.mark.parametrize("session_id", ["cli_session", "cli:local"])
async def test_documented_session_selection_sandboxes_cli_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_id: str
) -> None:
    guide = (Path(__file__).parents[1] / "docs" / "sandbox.md").read_text()
    setting = guide.split("BUB_OVERALLS_SESSIONS='", 1)[1].split("'", 1)[0]
    monkeypatch.setenv("BUB_OVERALLS_SESSIONS", setting)
    environment = Mock(spec=OverallsEnvironment)
    create = AsyncMock(return_value=environment)
    monkeypatch.setattr(OverallsEnvironment, "create", create)
    assert await plugin.provide_environment(session_id, tmp_path) is environment
    create.assert_awaited_once()
