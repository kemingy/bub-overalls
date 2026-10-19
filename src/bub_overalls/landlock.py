"""Build Linux sandbox grants on the host and snapshot the standalone launcher."""

import sys
from pathlib import Path

from bub_overalls._linux import OverallsLandlockPolicy
from bub_overalls.config import OverallsConfig
from bub_overalls.git import OverallsGitPaths
from bub_overalls.profile import OVERALLS_PROTECTED_NAMES

# Capture once on the trusted host. A command must not replace the next bootstrap.
_LAUNCHER_SOURCE = Path(__file__).with_name("_linux.py").read_text()


def build_overalls_landlock_policy(
    config: OverallsConfig,
    workspace: Path,
    temporary_path: Path,
    *,
    git_paths: OverallsGitPaths | None = None,
) -> OverallsLandlockPolicy:
    if config.git_access:
        raise ValueError(
            "experimental Landlock cannot enforce selective Git writes; "
            "git_access must be false (existing Git metadata is read-only)"
        )
    runtime = Path(sys.base_prefix).resolve()
    write_paths = tuple(sorted({workspace, temporary_path, *config.write_paths}))
    for writable in write_paths:
        if runtime.is_relative_to(writable) or writable.is_relative_to(runtime):
            raise ValueError("Linux launcher Python runtime must not be writable")
    read_paths = {
        workspace,
        temporary_path,
        runtime,
        Path(sys.prefix).resolve(),
        Path(sys.executable).resolve().parent.parent,
        *config.read_paths,
        *config.write_paths,
    }
    read_paths.update(
        path.resolve()
        for name in (
            "/usr",
            "/bin",
            "/sbin",
            "/lib",
            "/lib64",
            "/etc",
            "/dev/null",
            "/dev/zero",
            "/dev/random",
            "/dev/urandom",
        )
        if (path := Path(name)).exists()
    )
    git_paths = git_paths or OverallsGitPaths.discover(workspace, strict=False)
    return OverallsLandlockPolicy(
        read_paths=tuple(sorted(read_paths)),
        write_paths=write_paths,
        network_access=config.network_access,
        protected_names=(".git", *OVERALLS_PROTECTED_NAMES),
        protected_paths=(
            tuple(sorted({git_paths.directory, git_paths.common_directory}))
            if git_paths
            else ()
        ),
        git_marker=git_paths.marker if git_paths else None,
        temporary_path=temporary_path,
    )
