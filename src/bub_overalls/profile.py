"""Generate an SBPL profile; never interpolate paths as executable profile syntax."""

import json
import subprocess
import sys
from pathlib import Path

from bub_overalls.config import OverallsConfig
from bub_overalls.git import OverallsGitPaths

OVERALLS_PROTECTED_NAMES = (
    ".agents",
    ".codex",
    ".claude",
    ".cursor",
    ".vscode",
    ".idea",
    ".mcp.json",
    ".aws",
    ".ssh",
)


def _active_developer_path() -> Path | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/xcode-select", "-p"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        return None
    if path.suffix == ".app":
        path = path / "Contents" / "Developer"
    try:
        path = path.resolve(strict=True)
    except OSError:
        return None
    return path if path.is_dir() else None


def _xcode_shared_frameworks_path(developer_path: Path) -> Path | None:
    contents = developer_path.parent
    if contents.name != "Contents" or contents.parent.suffix != ".app":
        return None
    shared_frameworks = contents / "SharedFrameworks"
    return shared_frameworks.resolve() if shared_frameworks.is_dir() else None


def _quote_path(path: Path) -> str:
    return json.dumps(str(path), ensure_ascii=False)


def _deny_git_writes(path: Path, writable_paths: tuple[Path, ...]) -> str | None:
    if any(path.is_relative_to(writable) for writable in writable_paths):
        return None
    exceptions = [
        writable for writable in writable_paths if writable.is_relative_to(path)
    ]
    if not exceptions:
        return f"(deny file-write* (subpath {_quote_path(path)}))"
    allowed = " ".join(f"(subpath {_quote_path(item)})" for item in exceptions)
    return (
        f"(deny file-write* (require-all (subpath {_quote_path(path)}) "
        f"(require-not (require-any {allowed}))))"
    )


def _escape_regex_path(path: Path) -> str:
    """Encode a literal root in Seatbelt's regex syntax, not Python's regex syntax."""
    text = str(path)
    if '"' in text or "\\" in text or not text.isprintable():
        raise ValueError(f"unsupported character in write root: {text!r}")
    return "".join(
        character
        if character.isalnum() or character in "/_-"
        else r"\^"
        if character == "^"
        else f"[{character}]"
        for character in text
    )


def _deny_named_writes(
    root: Path, names: tuple[str, ...], writable_paths: tuple[Path, ...] = ()
) -> str:
    # Anchoring below the root keeps .codex/.claude ancestors writable for
    # agent-managed worktrees. Only fixed ASCII policy names enter alternatives.
    alternatives = "|".join(
        "".join(
            f"[{character}{character.upper()}]"
            if character.isalpha()
            else f"[{character}]"
            for character in name
        )
        for name in names
    )
    escaped_root = _escape_regex_path(root).rstrip("/")
    matcher = f'(regex #"^{escaped_root}/(.*/)?({alternatives})(/|$)")'
    if writable_paths:
        allowed = " ".join(f"(subpath {_quote_path(path)})" for path in writable_paths)
        matcher = f"(require-all {matcher} (require-not (require-any {allowed})))"
    return f"(deny file-write* {matcher})"


def build_overalls_profile(
    config: OverallsConfig,
    workspace: Path,
    temporary_path: Path,
    *,
    git_paths: OverallsGitPaths | None = None,
) -> str:
    # Follow interpreter symlinks so managed Python distributions work outside /usr.
    read_paths = {
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/Library"),
        Path("/private/etc"),
        Path("/private/var/db/dyld"),
        Path(sys.base_prefix).resolve(),
        Path(sys.prefix).resolve(),
        Path(sys.executable).resolve().parent.parent,
        workspace,
        temporary_path,
        *config.read_paths,
        *config.write_paths,
    }
    if developer_path := _active_developer_path():
        read_paths.add(developer_path)
        if shared_frameworks := _xcode_shared_frameworks_path(developer_path):
            read_paths.add(shared_frameworks)
    write_paths = {workspace, temporary_path, *config.write_paths}
    git_write_paths: tuple[Path, ...] = ()
    git_paths = git_paths or OverallsGitPaths.discover(
        workspace, strict=config.git_access
    )
    if config.git_access and git_paths is not None:
        read_paths.update((git_paths.directory, git_paths.common_directory))
        git_write_paths = git_paths.write_paths
        write_paths.update(git_write_paths)
    rules = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec process-fork)",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow file-read-metadata)",
        '(allow file-read* (literal "/"))',
        '(allow file-read* file-write* (literal "/dev/null") (literal "/dev/zero"))',
        '(allow file-read* (literal "/dev/random") (literal "/dev/urandom"))',
        '(allow file-read* file-write* (literal "/dev/tty") (literal "/dev/fd") (subpath "/dev/fd"))',
    ]
    rules.extend(
        f"(allow file-read* (subpath {_quote_path(path)}))"
        for path in sorted(read_paths)
    )
    rules.extend(
        f"(allow file-write* (subpath {_quote_path(path)}))"
        for path in sorted(write_paths)
    )
    for write_root in sorted(write_paths):
        rules.append(_deny_named_writes(write_root, OVERALLS_PROTECTED_NAMES))
        rules.append(_deny_named_writes(write_root, (".git",), git_write_paths))
    if git_paths is not None:
        for directory in sorted({git_paths.directory, git_paths.common_directory}):
            if rule := _deny_git_writes(directory, git_write_paths):
                rules.append(rule)
            # Keep the metadata roots in place, including a symlinked .git.
            rules.append(f"(deny file-write-unlink (literal {_quote_path(directory)}))")
        rules.append(
            f"(deny file-write-unlink (literal {_quote_path(git_paths.marker)}))"
        )
        for path in git_paths.protected_paths:
            rules.append(f"(deny file-write* (subpath {_quote_path(path)}))")
    if config.network_access:
        rules.extend(
            [
                '(allow network-outbound (remote ip "*:*"))',
                # getaddrinfo uses this socket on macOS; other Unix sockets stay denied.
                '(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))',
            ]
        )
    # Domain filtering needs an enforcing proxy, not hostname-to-IP profile rules.
    return "\n".join(rules) + "\n"
