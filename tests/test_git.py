import os
import subprocess
from pathlib import Path

import pytest

from bub_overalls import OverallsConfig, OverallsEnvironment
from bub_overalls.git import OverallsGitPaths
from bub_overalls.profile import build_overalls_profile

_GIT_ENV = {
    "PATH": os.defpath,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.excludesFile",
    "GIT_CONFIG_VALUE_0": os.devnull,
    "GIT_AUTHOR_NAME": "Overalls Test",
    "GIT_AUTHOR_EMAIL": "overalls@example.invalid",
    "GIT_COMMITTER_NAME": "Overalls Test",
    "GIT_COMMITTER_EMAIL": "overalls@example.invalid",
}


def _create_metadata(directory: Path) -> None:
    (directory / "objects").mkdir(parents=True)
    (directory / "refs").mkdir()
    (directory / "HEAD").write_text("ref: refs/heads/main\n")


def test_no_repository_has_no_git_grants(tmp_path: Path) -> None:
    assert OverallsGitPaths.discover(tmp_path) is None
    profile = build_overalls_profile(
        OverallsConfig(git_access=True), tmp_path, tmp_path / "temporary"
    )
    assert "([.][gG][iI][tT])(/|$)" in profile
    assert "allow network" not in profile


@pytest.mark.parametrize("separate", [False, True])
def test_git_directory_and_relative_gitfile(tmp_path: Path, separate: bool) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = tmp_path / 'metadata "資料"' if separate else workspace / ".git"
    _create_metadata(metadata)
    if separate:
        (workspace / ".git").write_text(f"gitdir: ../{metadata.name}\n")
    paths = OverallsGitPaths.discover(workspace)
    assert paths is not None
    assert paths.directory == paths.common_directory == metadata.resolve()
    assert paths.write_paths == (metadata.resolve(),)
    assert metadata / "config.lock" in paths.protected_paths
    assert metadata / "hooks" in paths.protected_paths
    assert metadata / "modules" in paths.protected_paths


def test_linked_worktree_resolves_common_directory(tmp_path: Path) -> None:
    common = tmp_path / "main" / ".git"
    _create_metadata(common)
    directory = common / "worktrees" / "agent"
    directory.mkdir(parents=True)
    workspace = tmp_path / "agent"
    workspace.mkdir()
    marker = workspace / ".git"
    marker.write_text(f"gitdir: {directory}\n")
    (directory / "HEAD").write_text("ref: refs/heads/agent\n")
    (directory / "commondir").write_text("../..\n")
    (directory / "gitdir").write_text(f"{marker}\n")
    paths = OverallsGitPaths.discover(workspace)
    assert paths is not None
    assert paths.directory == directory
    assert paths.common_directory == common
    assert common / "objects" in paths.write_paths
    assert common / "index" not in paths.write_paths
    assert marker in paths.protected_paths
    (directory / "gitdir").write_text(str(common / "HEAD"))
    with pytest.raises(ValueError, match="backlink"):
        OverallsGitPaths.discover(workspace)


@pytest.mark.parametrize("content", ["invalid", "gitdir: missing", "gitdir: "])
def test_invalid_gitfile_is_rejected(tmp_path: Path, content: str) -> None:
    (tmp_path / ".git").write_text(content)
    with pytest.raises(ValueError, match="cannot resolve Git metadata"):
        OverallsGitPaths.discover(tmp_path)
    assert OverallsGitPaths.discover(tmp_path, strict=False) is None


def test_metadata_must_be_an_initialized_repository(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="initialized repository"):
        OverallsGitPaths.discover(tmp_path)
    assert OverallsGitPaths.discover(tmp_path, strict=False) is None


def _host_git(workspace: Path, *arguments: str) -> str:
    return subprocess.run(
        ["/usr/bin/git", *arguments],
        cwd=workspace,
        env=_GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


async def _sandbox_git(
    environment: OverallsEnvironment, *arguments: str
) -> tuple[int, str]:
    process = await environment.spawn(["/usr/bin/git", *arguments], env=_GIT_ENV)
    stdout, stderr = await process.process.communicate()
    assert process.returncode is not None
    return process.returncode, (stdout + stderr).decode()


@pytest.mark.sandbox
@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("git_access", [False, True])
async def test_git_operations_respect_access_mode(
    tmp_path: Path, linked: bool, git_access: bool
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _host_git(repository, "init", "-b", "main")
    (repository / "example.txt").write_text("initial\n")
    _host_git(repository, "add", "example.txt")
    _host_git(repository, "commit", "-m", "Initial")
    workspace = repository
    if linked:
        workspace = tmp_path / "agent (資料)"
        _host_git(repository, "worktree", "add", "-b", "agent", str(workspace))
    main_head = (repository / ".git" / "HEAD").read_bytes()
    main_index = (repository / ".git" / "index").read_bytes()
    async with OverallsEnvironment(
        OverallsConfig(git_access=git_access), workspace
    ) as environment:
        await environment.write_text("example.txt", "edited\n")
        returncode, output = await _sandbox_git(environment, "add", "example.txt")
        if not git_access:
            assert returncode != 0
            return
        assert returncode == 0, output
        for arguments in (
            ("commit", "-m", "Agent change"),
            ("revert", "--no-edit", "HEAD"),
            ("reset", "--hard", "HEAD~1"),
        ):
            returncode, output = await _sandbox_git(environment, *arguments)
            assert returncode == 0, output
        assert await environment.read_text("example.txt") == "edited\n"
        returncode, output = await _sandbox_git(environment, "status", "--porcelain")
        assert (returncode, output) == (0, "")
        assert not environment.config.network_access
    if linked:
        assert (repository / "example.txt").read_text() == "initial\n"
        assert (repository / ".git" / "HEAD").read_bytes() == main_head
        assert (repository / ".git" / "index").read_bytes() == main_index


@pytest.mark.sandbox
@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("git_access", [False, True])
async def test_git_mode_preserves_configuration_and_other_worktree_metadata(
    tmp_path: Path, linked: bool, git_access: bool
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _host_git(repository, "init", "-b", "main")
    _host_git(repository, "commit", "--allow-empty", "-m", "Initial")
    sibling = tmp_path / "sibling"
    _host_git(repository, "worktree", "add", "-b", "sibling", str(sibling))
    sibling_paths = OverallsGitPaths.discover(sibling)
    assert sibling_paths is not None
    workspace = repository
    if linked:
        workspace = tmp_path / "agent"
        _host_git(repository, "worktree", "add", "-b", "agent", str(workspace))
    async with OverallsEnvironment(
        OverallsConfig(git_access=git_access, write_paths=(repository,)), workspace
    ) as environment:
        paths = environment.git_paths
        assert paths is not None
        protected = [
            paths.common_directory / "config",
            paths.common_directory / "config.lock",
            paths.common_directory / "hooks" / "pre-commit",
            paths.directory / "config.worktree",
            sibling_paths.directory / "index",
            sibling_paths.directory / "HEAD",
        ]
        if linked:
            protected.extend(
                [
                    paths.marker,
                    paths.directory / "commondir",
                    paths.directory / "gitdir",
                    paths.common_directory / "index",
                    paths.common_directory / "HEAD",
                ]
            )
        for path in protected:
            with pytest.raises(OSError):
                await environment.write_text(str(path), "must stay protected")
        returncode, output = await _sandbox_git(
            environment, "config", "example.key", "value"
        )
        assert returncode != 0, output
        # Granting the original checkout as writable must not prevent commits in
        # the selected worktree or unlock that checkout's metadata.
        returncode, output = await _sandbox_git(
            environment, "commit", "--allow-empty", "-m", "Agent"
        )
        if git_access:
            assert returncode == 0, output
        else:
            assert returncode != 0, output


@pytest.mark.sandbox
async def test_separate_git_directory_allows_initial_commit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = tmp_path / "separate metadata"
    _host_git(workspace, "init", "-b", "main", f"--separate-git-dir={metadata}")
    async with OverallsEnvironment(
        OverallsConfig(git_access=True), workspace
    ) as environment:
        assert environment.git_paths is not None
        assert environment.git_paths.directory == metadata
        await environment.write_text("example.txt", "initial\n")
        for arguments in (("add", "example.txt"), ("commit", "-m", "Initial")):
            returncode, output = await _sandbox_git(environment, *arguments)
            assert returncode == 0, output
        for path in (
            workspace / ".git",
            metadata / "config",
            metadata / "hooks" / "pre-commit",
        ):
            with pytest.raises(OSError):
                await environment.write_text(str(path), "must stay protected")
    assert _host_git(workspace, "log", "-1", "--format=%s") == "Initial"


@pytest.mark.sandbox
async def test_git_mode_uses_repository_identity_without_home_config_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _host_git(workspace, "init", "-b", "main")
    _host_git(workspace, "config", "user.name", "Overalls User")
    _host_git(workspace, "config", "user.email", "user@example.invalid")
    _host_git(workspace, "config", "commit.gpgsign", "false")
    git_home = tmp_path / "user-home"
    git_home.mkdir()
    configuration = git_home / ".gitconfig"
    configuration.write_text("[user]\n name = Must Not Be Used\n")
    monkeypatch.setenv("HOME", str(git_home))
    async with OverallsEnvironment(
        OverallsConfig(git_access=True), workspace
    ) as environment:
        await environment.write_text("example.txt", "initial\n")
        # No injected author or configuration overrides: exercise real defaults.
        for arguments in (("add", "example.txt"), ("commit", "-m", "Local identity")):
            process = await environment.spawn(["/usr/bin/git", *arguments])
            _, stderr = await process.process.communicate()
            assert process.returncode == 0, stderr
        with pytest.raises(OSError):
            await environment.read_text(str(configuration))
    assert _host_git(workspace, "log", "-1", "--format=%an <%ae>") == (
        "Overalls User <user@example.invalid>"
    )


@pytest.mark.parametrize("git_access", [False, True])
def test_profile_handles_unsupported_metadata_by_access_mode(
    tmp_path: Path, git_access: bool
) -> None:
    (tmp_path / ".git").write_text("unsupported layout")
    config = OverallsConfig(git_access=git_access)
    if git_access:
        with pytest.raises(ValueError, match="cannot resolve Git metadata"):
            build_overalls_profile(config, tmp_path, tmp_path / "temporary")
    else:
        profile = build_overalls_profile(config, tmp_path, tmp_path / "temporary")
        assert "([.][gG][iI][tT])(/|$)" in profile


def test_submodule_metadata_symlink_target_is_protected(tmp_path: Path) -> None:
    metadata = tmp_path / ".git"
    _create_metadata(metadata)
    modules = tmp_path / "external-modules"
    modules.mkdir()
    (metadata / "modules").symlink_to(modules, target_is_directory=True)
    paths = OverallsGitPaths.discover(tmp_path)
    assert paths is not None
    assert metadata / "modules" in paths.protected_paths
    assert modules in paths.protected_paths


@pytest.mark.sandbox
@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("git_access", [False, True])
async def test_submodule_configuration_and_hooks_stay_read_only(
    tmp_path: Path, linked: bool, git_access: bool
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _host_git(source, "init", "-b", "main")
    _host_git(source, "commit", "--allow-empty", "-m", "Initial submodule")
    repository = tmp_path / "repository"
    repository.mkdir()
    _host_git(repository, "init", "-b", "main")
    _host_git(
        repository,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(source),
        "lib",
    )
    _host_git(repository, "commit", "-m", "Add submodule")
    workspace = repository
    if linked:
        workspace = tmp_path / "agent"
        _host_git(repository, "worktree", "add", "-b", "agent", str(workspace))
    metadata = repository / ".git" / "modules" / "lib"
    original_config = (metadata / "config").read_bytes()
    async with OverallsEnvironment(
        OverallsConfig(git_access=git_access, write_paths=(repository,)), workspace
    ) as environment:
        assert environment.git_paths is not None
        for path in (
            metadata / "config",
            metadata / "config.lock",
            metadata / "hooks" / "pre-commit",
            metadata / "HEAD",
            environment.git_paths.directory / "modules" / "new" / "config",
        ):
            with pytest.raises(OSError):
                await environment.write_text(str(path), "inert test content")
        await environment.write_text("ordinary.txt", "workspace remains writable")
    assert (metadata / "config").read_bytes() == original_config
    assert not (metadata / "config.lock").exists()
    assert not (metadata / "hooks" / "pre-commit").exists()


@pytest.mark.sandbox
@pytest.mark.parametrize("layout", ["gitfile", "directory", "backlink"])
async def test_read_only_mode_keeps_unsupported_workspaces_usable(
    tmp_path: Path, layout: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / ".git"
    if layout == "gitfile":
        marker.write_text("unsupported layout")
    elif layout == "directory":
        marker.mkdir()
        (marker / "config").write_text("inert test content")
    else:
        repository = tmp_path / "repository"
        repository.mkdir()
        _host_git(repository, "init", "-b", "main")
        _host_git(repository, "commit", "--allow-empty", "-m", "Initial")
        _host_git(repository, "worktree", "add", "-b", "agent", str(workspace))
        paths = OverallsGitPaths.discover(workspace)
        assert paths is not None
        (paths.directory / "gitdir").write_text("missing backlink")
    async with OverallsEnvironment(OverallsConfig(), workspace) as environment:
        assert environment.git_paths is None
        await environment.write_text("ordinary.txt", "works")
        assert await environment.read_text("ordinary.txt") == "works"
        with pytest.raises(OSError):
            await environment.write_text(
                str(marker / "config" if marker.is_dir() else marker), "changed"
            )
    with pytest.raises(ValueError, match="cannot resolve Git metadata"):
        OverallsEnvironment(OverallsConfig(git_access=True), workspace)


@pytest.mark.sandbox
@pytest.mark.parametrize("parent", [".claude/worktrees", ".codex/worktrees/abc"])
async def test_agent_managed_worktrees_allow_git_writes_with_opt_in(
    tmp_path: Path, parent: str
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _host_git(repository, "init", "-b", "main")
    _host_git(repository, "commit", "--allow-empty", "-m", "Initial")
    workspace = tmp_path / parent / "project"
    _host_git(repository, "worktree", "add", "-b", "agent", str(workspace))
    async with OverallsEnvironment(
        OverallsConfig(git_access=True), workspace
    ) as environment:
        await environment.write_text("file.txt", "ok")
        for arguments in (("add", "file.txt"), ("commit", "-m", "Agent change")):
            returncode, output = await _sandbox_git(environment, *arguments)
            assert returncode == 0, output
        with pytest.raises(OSError):
            await environment.write_text("nested/.git/config", "denied")
        assert environment.git_paths is not None
        with pytest.raises(OSError):
            await environment.write_text(
                str(environment.git_paths.common_directory / "config"), "denied"
            )
    assert _host_git(workspace, "log", "-1", "--format=%s") == "Agent change"
