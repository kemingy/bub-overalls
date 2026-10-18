"""Resolve the selected repository's metadata without running Git on the host."""

from dataclasses import dataclass
from pathlib import Path
from typing import Self


@dataclass(frozen=True, slots=True)
class OverallsGitPaths:
    """Metadata belonging to the repository rooted at the sandbox workspace."""

    marker: Path
    directory: Path
    common_directory: Path

    @classmethod
    def discover(cls, workspace: Path, *, strict: bool = True) -> Self | None:
        """Resolve known layouts; strict mode requires safe Git write grants."""
        marker = workspace / ".git"
        if not marker.exists():
            return None
        try:
            if marker.is_dir():
                directory = marker.resolve(strict=True)
            else:
                content = marker.read_text(encoding="utf-8").strip()
                if not content.startswith("gitdir: "):
                    raise ValueError("expected a gitdir: pointer")
                directory = (workspace / content.removeprefix("gitdir: ")).resolve(
                    strict=True
                )
            common_file = directory / "commondir"
            common = (
                (directory / common_file.read_text(encoding="utf-8").strip()).resolve(
                    strict=True
                )
                if common_file.exists()
                else directory
            )
            if not (
                (directory / "HEAD").is_file()
                and (common / "objects").is_dir()
                and (common / "refs").is_dir()
            ):
                raise ValueError(
                    "expected an initialized repository with file-based refs"
                )
            if common != directory:
                if not directory.is_relative_to(common / "worktrees"):
                    raise ValueError(
                        "worktree metadata must be inside common/worktrees"
                    )
                backlink = (directory / "gitdir").read_text(encoding="utf-8").strip()
                if (directory / backlink).resolve(strict=True) != marker.resolve(
                    strict=True
                ):
                    raise ValueError("worktree backlink does not match the workspace")
            return cls(marker, directory, common)
        except (OSError, UnicodeError, RuntimeError, ValueError) as error:
            if not strict:
                return None
            raise ValueError(
                f"cannot resolve Git metadata at {marker}: {error}"
            ) from error

    @property
    def write_paths(self) -> tuple[Path, ...]:
        """Grant the worktree's state and shared storage, not another index."""
        if self.directory == self.common_directory:
            return (self.directory,)
        return (
            self.directory,
            self.common_directory / "objects",
            self.common_directory / "refs",
            self.common_directory / "logs" / "refs",
            self.common_directory / "packed-refs",
            self.common_directory / "packed-refs.lock",
        )

    @property
    def protected_paths(self) -> tuple[Path, ...]:
        paths: set[Path] = set()
        for directory in (self.directory, self.common_directory):
            for name in (
                "hooks",
                "modules",
                "config",
                "config.lock",
                "config.worktree",
                "config.worktree.lock",
                "commondir",
                "gitdir",
            ):
                path = directory / name
                paths.update((path, path.resolve()))
        if self.marker.is_file():
            paths.update((self.marker, self.marker.resolve()))
        if self.directory == self.common_directory:
            paths.add(self.common_directory / "worktrees")
        return tuple(sorted(paths))
