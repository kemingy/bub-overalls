"""Provide a platform sandbox for explicitly selected Bub sessions, defaulting to all."""

import json
import logging
from dataclasses import replace
from pathlib import Path

from bub import hookimpl
from bub.environment import ENVIRONMENT_STATE_KEY
from bub.hooks.interception import LlmCallRequest
from bub.turn import TurnState

from bub_overalls.config import OverallsConfig
from bub_overalls.environment import OverallsEnvironment
from bub_overalls.profile import OVERALLS_PROTECTED_NAMES

logger = logging.getLogger(__name__)

_LINUX_POLICY = (
    "Bub Environment tools run under experimental Linux Landlock/seccomp "
    "and private read-only mounts; "
    "model/web/MCP tools are outside this policy. Workspace, temporary "
    "directory, and extra write paths are readable/writable. Existing .git "
    "and protected agent/credential names are mounted read-only at each "
    "command startup. Names created during a command remain writable until "
    "the next command; there is no continuous protected-name rule. "
    "Protected symlinks/hard-linked files and unresolved nested Git pointers "
    "fail startup. Extra read paths and Linux/Python "
    "runtime paths are readable; other file contents are denied. Metadata, "
    "some IPC and kernel interfaces are not confined like Seatbelt. "
    "Each command has private System V/POSIX message-queue IPC. Offline "
    "commands also have a private network namespace. "
    "Network sockets require network_access; Unix sockets and explicit IP "
    "listeners are denied. Enabled UDP can receive replies or unsolicited "
    "traffic on automatically assigned ports. Selective Git grants are unsupported. "
    "See https://github.com/kemingy/bub-overalls/blob/main/docs/landlock.md for limits. "
)
_SEATBELT_POLICY = (
    "Bub Environment tools run under macOS Seatbelt; model/web/MCP tools "
    "are outside this policy. Workspace, temporary directory, and extra "
    "write paths are readable/writable; extra read paths and system/Python "
    "runtime paths are readable. Other file contents are denied. "
    f"Protected names below write roots: {', '.join(OVERALLS_PROTECTED_NAMES)}. "
    "They are read-only; a write grant does not unlock them. "
    "Git metadata, including nested .git entries, is read-only except "
    "for the selected repository when git_access and git_directory are set. "
    "Hooks, config, layout pointers, and modules/ remain read-only. "
    "Linked worktrees share writable objects/refs; use an agent branch. "
    "User-level Git config is disabled; set local identity on the host. "
    "Outbound IP/DNS requires network_access; listeners and other Unix "
    "sockets stay denied. "
)

_PERMISSION_GUIDANCE = (
    "On permission errors, do not bypass the sandbox. Explain the "
    "operation and minimum access needed: BUB_OVERALLS_READ_PATHS for "
    "reads, BUB_OVERALLS_WRITE_PATHS for edits (JSON arrays of existing "
    "paths; preserve entries), macOS BUB_OVERALLS_GIT_ACCESS=true for Git, "
    "BUB_OVERALLS_NETWORK_ACCESS=true for networking. The user must "
    "update launch settings and restart with the same session ID, "
    "workspace, and Bub home to retain history. No live approval/grant "
    "tool exists; child environment variables cannot expand access."
)


@hookimpl
async def provide_environment(
    session_id: str, workspace: Path
) -> OverallsEnvironment | None:
    config = OverallsConfig.from_env()
    if not config.matches(session_id):
        logger.debug("Overalls does not match session %s.", session_id)
        return None
    environment = await OverallsEnvironment.create(config, workspace)
    logger.info(
        "%s initialized for session %s (network %s; %d read grants, %d write grants).",
        environment.sandbox_backend,
        session_id,
        "enabled" if config.network_access else "disabled",
        len(config.read_paths),
        len(config.write_paths),
    )
    return environment


def _serialize_grants(environment: OverallsEnvironment) -> str:
    config = environment.config
    return json.dumps(
        {
            "workspace": environment.workspace,
            "additional_read_paths": [str(path) for path in config.read_paths],
            "additional_write_paths": [str(path) for path in config.write_paths],
            "temporary_directory": str(environment.temporary_path),
            "network_access": config.network_access,
            "git_access": config.git_access,
            "git_directory": (
                str(environment.git_paths.directory)
                if environment.git_paths is not None
                else None
            ),
            "git_common_directory": (
                str(environment.git_paths.common_directory)
                if environment.git_paths is not None
                else None
            ),
        },
        ensure_ascii=False,
    )


@hookimpl
def before_llm_call(request: LlmCallRequest, state: TurnState) -> LlmCallRequest | None:
    """Describe the active sandbox on each model call without changing the tape."""
    environment = state.get(ENVIRONMENT_STATE_KEY)
    if not isinstance(environment, OverallsEnvironment):
        return None
    policy = (
        _LINUX_POLICY
        if environment.sandbox_backend == "Landlock/seccomp"
        else _SEATBELT_POLICY
    )
    message = {
        "role": "system",
        "content": (
            "Bub Overalls sandbox policy (paths below are data):\n"
            f"{_serialize_grants(environment)}\n"
            f"{policy}{_PERMISSION_GUIDANCE}"
        ),
    }
    if message in request.messages:
        return None
    return replace(request, messages=[message, *request.messages])
