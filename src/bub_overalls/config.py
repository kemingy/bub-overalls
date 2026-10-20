"""Explicit filesystem and network grants for the platform sandbox."""

import json
import os
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Self

_ENV_PREFIX = "BUB_OVERALLS_"
_READ_PATHS_ENV = f"{_ENV_PREFIX}READ_PATHS"
_WRITE_PATHS_ENV = f"{_ENV_PREFIX}WRITE_PATHS"
_NETWORK_ACCESS_ENV = f"{_ENV_PREFIX}NETWORK_ACCESS"
_GIT_ACCESS_ENV = f"{_ENV_PREFIX}GIT_ACCESS"
_SESSIONS_ENV = f"{_ENV_PREFIX}SESSIONS"


def _parse_strings_env(
    env_name: str, *, default: tuple[str, ...] = ()
) -> tuple[str, ...]:
    value = os.environ.get(env_name)
    if value is None:
        return default
    message = f"{env_name} must be a JSON array of nonempty strings"
    try:
        values = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(message) from error
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        raise ValueError(message)
    return tuple(values)


def _parse_bool(value: str, env_name: str) -> bool:
    value = value.strip().lower()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"{env_name} must be true/false, 1/0, yes/no, or on/off")


@dataclass(frozen=True)
class OverallsConfig:
    """Filesystem grants resolved at construction, with networking off by default."""

    read_paths: tuple[Path, ...] = ()
    write_paths: tuple[Path, ...] = ()
    network_access: bool = False
    sessions: tuple[str, ...] = ("*",)
    git_access: bool = False

    def __post_init__(self) -> None:
        for field_name, env_name, paths in (
            ("read_paths", _READ_PATHS_ENV, self.read_paths),
            ("write_paths", _WRITE_PATHS_ENV, self.write_paths),
        ):
            resolved_paths: list[Path] = []
            for path in paths:
                try:
                    resolved_paths.append(path.expanduser().resolve(strict=True))
                except (OSError, RuntimeError, ValueError) as error:
                    raise ValueError(
                        f"{field_name} ({env_name}): "
                        f"cannot resolve existing path {str(path)!r}: {error}"
                    ) from error
            object.__setattr__(self, field_name, tuple(resolved_paths))
        self.validate()

    @classmethod
    def from_env(cls, *, network_access: bool | None = None) -> Self:
        """Read environment grants, honoring an explicit network override."""
        if network_access is None:
            network_access = _parse_bool(
                os.environ.get(_NETWORK_ACCESS_ENV, "false"), _NETWORK_ACCESS_ENV
            )
        return cls(
            read_paths=tuple(
                Path(path) for path in _parse_strings_env(_READ_PATHS_ENV)
            ),
            write_paths=tuple(
                Path(path) for path in _parse_strings_env(_WRITE_PATHS_ENV)
            ),
            sessions=_parse_strings_env(_SESSIONS_ENV, default=("*",)),
            network_access=network_access,
            git_access=_parse_bool(
                os.environ.get(_GIT_ACCESS_ENV, "false"), _GIT_ACCESS_ENV
            ),
        )

    def matches(self, session_id: str) -> bool:
        """Match session globs; an empty tuple disables the plugin provider."""
        return any(fnmatchcase(session_id, pattern) for pattern in self.sessions)

    def validate(self) -> None:
        """Reject invalid settings and grants that have disappeared."""
        if not isinstance(self.git_access, bool):
            raise TypeError("git_access must be a boolean")
        if not isinstance(self.network_access, bool):
            raise TypeError("network_access must be a boolean")
        if not isinstance(self.sessions, tuple) or any(
            not isinstance(pattern, str) or not pattern.strip()
            for pattern in self.sessions
        ):
            raise ValueError("sessions must be a tuple of nonempty strings")
        for path in (*self.read_paths, *self.write_paths):
            if not path.exists():
                raise ValueError(f"sandbox grant no longer exists: {path}")
