# Configuration and sandbox boundaries

Overalls runs Bub commands and file operations through macOS Seatbelt, using
installed tools directly. Linux support will use Landlock; it is not implemented
yet. Initialization and preflight failures never select host execution.

## Configuration

Use environment variables or the standard-library `OverallsConfig` dataclass.
There is no separate YAML configuration.

| Variable (`BUB_OVERALLS_` prefix) | Default | Purpose |
| --- | --- | --- |
| `READ_PATHS` | `[]` | Extra readable paths, as a JSON array |
| `WRITE_PATHS` | `[]` | Extra writable paths, also readable |
| `NETWORK_ACCESS` | `false` | Allow outbound IP traffic |
| `GIT_ACCESS` | `false` | Allow selected repository metadata writes |
| `SESSIONS` | All sessions | Case-sensitive session globs, as a JSON array |

```sh
BUB_OVERALLS_READ_PATHS='["../spectree"]' uv run bub chat
BUB_OVERALLS_GIT_ACCESS=true uv run bub chat
uv run bub-overalls exec --workspace /path/to/project --timeout 30 -- make test
uv run bub-overalls doctor
```

Paths must exist. They expand `~`, resolve symlinks, and become absolute relative
to the launch directory, not the workspace. Read grants never imply writes.
Writable roots reject double quotes, backslashes, and nonprintable characters;
other regex metacharacters are escaped. Read-only grants allow quotes and
backslashes. Invalid paths, JSON, and configuration fail initialization.

Booleans accept `true/false`, `1/0`, `yes/no`, and `on/off`, ignoring case and
surrounding whitespace. Empty or unknown values are errors.

Grants are fixed at environment creation. Restart Bub with the same session ID,
workspace, and Bub home to change access while retaining conversation history.
The prompt hook tells the model its active grants and how to request more;
it changes neither permissions nor stored history. No skill installation is needed.

To select sessions, use e.g. `BUB_OVERALLS_SESSIONS='["cli_session", "cli:*"]'`.
`cli_session` is the default for `bub chat`; `cli:*` covers `bub run`.
An empty array matches none. **Excluded sessions may use Bub's host environment.**
The standalone CLI always uses Seatbelt, regardless of session patterns.

## Filesystem policy

- Read: workspace, private session temp directory, Python runtime/virtualenv,
  macOS system/tool directories, and explicit grants. File metadata is globally
  readable; other file contents are denied.
- Write: workspace, session temp directory, and explicit write grants.
- Protected at every depth: `.git`, `.agents`, `.codex`, `.claude`, `.cursor`,
  `.vscode`, `.idea`, `.mcp.json`, `.aws`, and `.ssh`. Git opt-in grants limited
  exceptions below. Extra write grants do not unlock protected names.

Protected names match whole components case-insensitively, anchored below each
write root. Ancestors do not block agent workspaces under `.claude/worktrees`
or `.codex/worktrees`; broader overlapping roots may still protect nested paths.
Lookalikes such as `.github` remain writable. Symlinks do not expand access.

**Writes affect the host immediately and persist.** Workspace `.env` files are
readable. Avoid broad home-directory grants; grant toolchains and caches explicitly.
Child environments inherit a small allowlist, excluding provider credentials and
startup hooks. Explicit `spawn(env=...)` values pass through without expanding
sandbox permissions.

The name list cannot protect every file consumed by host tools. Nested gitfiles
may reference undiscovered external Git directories; avoid grants covering them.
Bare repositories without `.git` are also writable. On the host, use
`git config --global safe.bareRepository explicit` (Git 2.38+) to prevent implicit
bare-repository discovery. Explicitly selected bare repositories still require trust.

## Git writes

Git metadata is read-only unless `BUB_OVERALLS_GIT_ACCESS=true`. Initialize the
repository or worktree on the host, then start Bub at that repository's root.
Regular repositories, separate Git directories, and linked worktrees are supported;
parent repositories, submodules, bare repositories, and reftable are not discovered
as writable repositories. Unsupported layouts fail with Git access enabled;
otherwise workspace edits remain available with the root `.git` protected.

Enabled mode permits staging, commits, revert, and reset. Config, config locks,
hooks, layout pointers, and `modules/` (including submodule metadata) stay read-only.
Existing hooks still run under Seatbelt. Moving a worktree or changing its Git
layout requires a restart.

For agent work, prefer a dedicated branch and worktree. A worktree separates
working files and its index, but commits require shared objects and refs to be
writable. Original and sibling worktree metadata stay protected; shared branch
history can still be changed or damaged. Use an independent clone when the
original repository's metadata must remain untouched. Git access does not grant
networking or authorize destructive commands.

Sandboxed Git defaults to `GIT_CONFIG_GLOBAL=/dev/null`. Set identity on the host:

```sh
git config --local user.name "Your Name"
git config --local user.email "you@example.com"
```

Alternatively, grant a config file read access and set `GIT_CONFIG_GLOBAL` for the
command. Signing keys and credentials need their own grants.

## Network

Command networking is off by default, including localhost and Unix sockets,
following Codex's [documented workspace sandbox](https://developers.openai.com/codex/agent-approvals-security/#network-access).
Bub's model requests and separate web/MCP tools run outside Overalls. A URL needs
a fetch tool; the model alone cannot retrieve it. There is no live approval or
automatic network escalation.

Enable with `BUB_OVERALLS_NETWORK_ACCESS=true`, or override for one CLI invocation:

```sh
uv run bub-overalls exec --network enabled -- curl https://example.com
```

Enabled mode allows outbound IPv4/IPv6 TCP and UDP to **all destinations**, including
localhost/private networks, plus the DNS resolver socket. Inbound listeners, other
Unix sockets, and Mach service lookup remain denied. There is no domain allowlist.
Filesystem and inherited-credential restrictions remain, but commands can transmit
anything they can read, including workspace secrets.

## Lifecycle and integration

```python
from pathlib import Path
from bub_overalls import OverallsConfig, OverallsEnvironment

async with OverallsEnvironment(OverallsConfig.from_env(), Path.cwd()) as environment:
    await environment.write_text("example.txt", "hello")
```

Context entry verifies Seatbelt. Normal close sends process groups `SIGTERM`,
shares a 1-second grace period, then sends `SIGKILL`. Cancellation or exceptional
exit kills immediately; cleanup completes before cancellation propagates.
In-flight startup settles before session files are removed; process reaping is
bounded to three seconds. Cancelling a raw `process.wait()` does not kill the
process: its caller must signal it or close the environment. Consume both output
streams concurrently. `create()` supports Bub-managed lifetimes; callers own close.

Only the CLI installs signal handlers: SIGINT/SIGTERM exit with 130/143, timeout
with 124. It defaults to the current directory and 60 seconds, closes stdin, and
forwards stdout/stderr. Blocked output does not prevent cleanup; cancellation may
discard buffered output. The environment API supports streamed input.

Logs use standard Python logging; hosts own handlers and exporters. The CLI sets
INFO on stderr while respecting existing configuration. Bub currently uses Loguru,
so hosted logs need a standard handler or host-owned bridge to become visible.
Overalls installs neither global logging configuration nor signal handlers in Bub.

## Limits and development

Only Bub `Environment` operations are confined. Other plugins, code-mode tool
callbacks, MCP servers, and model requests have separate boundaries. Verify plugin
discovery: Bub may tolerate discovery failures outside a framework turn.
Children inherit Seatbelt, but deliberately detached processes can outlive cleanup;
SIGKILL of the launcher prevents cleanup. There are no aggregate CPU/memory/PID
limits or separate kernel. Sessions sharing a workspace see each other's writes.
Apple deprecates `sandbox-exec`; profiles use private interfaces, so test macOS
upgrades before relying on them.

Use `make install`, `make format`, `make check`, `make test`, `make test-sandbox`,
and `make package`. Python 3.12+ and released Bub (`bub>=0.5.1`) are required;
`uv.lock` pins development dependencies. Source lives in `src/bub_overalls/`,
uses strict typing, and follows SpecTree's 88-column style. PDM reads versions
from Git tags, falling back to `0.0.0` without SCM metadata.
See [validation](validation.md) for coverage and CI.
