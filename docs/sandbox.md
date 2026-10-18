# Configuration and sandbox boundaries

The `overalls` plugin provides Bub's `Environment` through macOS Seatbelt.
Initialization failures propagate; unsupported hosts never select host execution.

`exec` defaults to the current directory and a 60-second timeout. Pass
`--workspace /path/to/project` and `--timeout 30` before `--` to override them.
The CLI closes stdin; the environment API supports streamed input and output.
The CLI is also available as `python -m bub_overalls.main`. Diagnostics use
standard Python logging with module-named loggers. The package installs only a
`NullHandler`; imports and hooks leave handlers, formatting, levels, and exporters
to the host. The standalone `bub-overalls` CLI uses `basicConfig()` for INFO logging
on stderr, preserving any existing host configuration. Errors log at ERROR level.
The current upstream Bub uses Loguru and does not collect stdlib records into its
sinks. Hosted Overalls diagnostics remain silent unless the application installs
a standard handler or a bridge. A Bub-owned bridge can be proposed separately;
Overalls does not install one. Other hosts can use standard handlers directly.
Command stdout and stderr are forwarded unchanged, and benchmark stdout stays JSON.
CLI output uses nonblocking writes: a stalled consumer does not prevent timeout
or signal cleanup. Cancellation may discard pending output. The CLI's default
logging handler drops diagnostics when stderr cannot accept them, so it cannot
block exit. Existing host logging handlers remain owned by the host.

## Configuration and sessions

Overalls uses the standard-library `OverallsConfig` dataclass and `BUB_OVERALLS_*`
environment variables. It does not register a Bub YAML section or use Pydantic
settings. The plugin and standalone CLI share the same parsing and path validation.
Direct `OverallsConfig(...)` construction uses its explicit arguments and defaults.
Bub's own internal configuration dependencies remain owned by Bub.

All Bub sessions are sandboxed by default. To select sessions explicitly, set
case-sensitive shell globs as a JSON array:

```sh
export BUB_OVERALLS_SESSIONS='["cli_session", "cli:*", "telegram:*"]'
```

`cli_session` covers the default `bub chat`; `cli:*` covers `bub run`
(default `cli:local`). Custom session IDs need a matching pattern.
An empty array matches none. **Excluded sessions are not sandboxed by Overalls**:
another provider, including Bub's host environment, may handle them. Malformed
configuration and preflight failures propagate rather than selecting a host
environment. Restart Bub after changing environment variables; Bub keeps an
environment per session. The standalone CLI always runs sandboxed regardless of
session patterns.

To expand read access while keeping an existing conversation, stop Bub and
restart it with the same session ID, workspace, and Bub home:

```sh
BUB_OVERALLS_READ_PATHS='["../spectree"]' uv run bub chat --session-id cli_session
```

The interactive CLI defaults to `cli_session`; use your existing session ID if
you chose another. Bub keeps the conversation's
stored history; the new process creates a sandbox with the new grants. There is
currently no in-session command for expanding access. Existing children retain
the Seatbelt policy they started with.

After preflight, the plugin logs the session, network mode, and extra grant counts
at INFO through standard logging. Excluded sessions log at DEBUG. The application
still owns logging configuration.

The plugin's `before_llm_call` hook adds a system message describing the active
session's grants, key restrictions, and how to request more access. Detailed Git
setup and boundary explanations stay in this guide
rather than being repeated in every model call.
It uses the existing environment's configuration, not newly changed launch
variables. This message is added to outgoing model requests without changing
stored history; other environment providers receive no Overalls guidance. No
skill installation is needed. The model should ask for a minimal read grant for
references or a write grant for edits, preserving existing entries. Users must
change launch configuration and restart the same session. This guidance neither
grants permissions nor identifies every permission error as a sandbox denial.

## Permissions

The profile denies operations unless explicitly allowed:

- Reads: workspace, private session temporary directory, active Python runtime and
  virtualenv, and macOS system/tool directories. File metadata is readable globally;
  file contents elsewhere are denied.
- Writes: workspace and private session temporary directory. At every depth in
  writable paths, `.agents`, `.codex`, `.claude`, `.cursor`, `.vscode`, `.idea`,
  `.mcp.json`, `.aws`, and `.ssh` stay read-only. Git metadata is read-only by
  default; the Git opt-in below grants repository-specific writes.
- Network: disabled by default, including localhost and Unix-socket connections.
  Opt-in network access is described below. Mach service lookup stays denied.
- Processes: execution and forks are allowed; children inherit the sandbox.
  Provider credentials and startup hooks are not inherited from Bub's environment.
  Explicit `spawn(env=...)` values are passed through without changing permissions.

Workspace changes are **live host writes** and persist after close. Workspace
`.env` files are readable; this plugin does not detect or remove secrets.
Symlinks do not grant access to targets outside the allowed paths.

Protected names match whole path components, case-insensitively, using Seatbelt
regex rules anchored below each write root. Protected names in ancestors of a
root do not restrict that root: workspaces under `.claude/worktrees` or
`.codex/worktrees` and grants under `.cursor` remain usable. A broader overlapping
write root can still protect a nested path. This covers existing nested repositories,
new `.git` directories and gitfiles, and the listed nested agent/IDE configs,
including in the session temporary directory. `.github` and `.gitignore` do not
match. Extra write grants do not unlock these names. The Git opt-in exempts only
the selected repository's discovered writable metadata; its config, hooks,
submodule metadata, and layout protections still apply.

Name matching does not discover arbitrary external Git directories referenced by
nested gitfiles. Only the selected repository's resolved metadata gets explicit
path protection. Avoid broad write grants covering undiscovered external metadata.
Bare repositories (`HEAD`, `objects/`, `refs/`, and `config` without a `.git`
entry) also remain writable; host Git may consume their configuration outside
Seatbelt. On the host, use `git config --global safe.bareRepository explicit`
(Git 2.38+) to disable implicit bare-repository discovery. Explicitly selected
bare repositories still require trust. These protections are a curated list,
not a guarantee that workspace files are safe for host tools to consume.

Toolchains, SDKs, and package caches under your home directory may need explicit
grants. Set existing paths as JSON arrays:

```sh
export BUB_OVERALLS_READ_PATHS='["/Users/me/.cargo", "/Users/me/.rustup"]'
export BUB_OVERALLS_WRITE_PATHS='["/Users/me/.cache/my-project"]'
```

Read grants never imply writes; write grants include reads. Paths expand `~`,
resolve symlinks, and become absolute when the configuration is created. Relative
paths use the process's current directory at that time, rather than the sandbox
workspace; this also applies to `OverallsConfig(read_paths=(Path("./data"),))`.
Writable roots (workspace, extra write paths, session temp, and enabled Git
storage) cannot contain double quotes, backslashes, or nonprintable characters;
profile generation rejects them with a `ValueError` before running any command.
Other regex metacharacters are escaped literally. Read-only grants can still
contain quotes and backslashes.
Configuration values are validated during construction. Grants must exist; missing
paths and empty path entries are rejected. Malformed JSON reports the environment
variable name. Environment initialization rechecks grants and verifies macOS and
`sandbox-exec` availability. Avoid broad home-directory grants.

### Git operations

Git metadata is read-only by default. To enable local Git operations in the
repository rooted at the workspace:

```sh
BUB_OVERALLS_GIT_ACCESS=true uv run bub chat
```

In Python, use `OverallsConfig(git_access=True)`. The setting accepts the same
boolean spellings as network access and defaults to `false`; malformed values
fail initialization. Restart the same Bub session after changing it.

Initialize the repository or create a linked worktree on the host before starting
Bub. For agent work, use a dedicated branch and worktree. Overalls discovers the
workspace root's `.git` directory or gitfile, resolves linked-worktree `commondir`
and backlink files, and validates the metadata layout without running host Git.
With Git access enabled, malformed or unsupported layouts fail initialization.
With Git access disabled, discovery failure keeps the workspace usable and
deny-writes the root `.git` marker or directory; it grants no metadata access.
Unresolved external metadata receives no additional protection, so avoid write
grants covering it. A workspace without `.git` receives no
Git grant; parent repositories, submodules, bare repositories, and reftable
storage are not discovered automatically. Start at the desired repository root.

Sandboxed Git ignores user-level configuration by default
(`GIT_CONFIG_GLOBAL=/dev/null`) to avoid implicit home-directory reads. Configure
identity in the repository on the host before starting Bub:

```sh
git config --local user.name "Your Name"
git config --local user.email "you@example.com"
```

For a linked worktree these settings live in the shared repository config.
Alternatively, explicitly grant the required config file as a read path and set
`GIT_CONFIG_GLOBAL` for the Git command or through `spawn(env=...)`. Signing keys,
credentials, and other files referenced by config still need separate grants.

Enabled mode supports staging, commits, revert, and reset. For linked worktrees,
it grants the selected worktree's metadata plus shared objects, refs, branch
reflogs, and packed refs. The original checkout's index and HEAD, and sibling
worktree metadata, remain read-only even if the original checkout is an extra
write grant. Ordinary repositories and separate Git directories are also
supported. Existing standard `config`, `config.worktree`, their lock files,
`hooks/`, `modules/` (including all submodule config and hooks), and layout
pointers remain read-only. Submodule metadata writes are unsupported even with
Git access enabled. These file protections do not
forbid command-line Git options or execution of existing hooks under Seatbelt.

Git access is independent of command networking. Enabling commits does not
enable pushes over the network. Extra filesystem write grants do not substitute
for the Git opt-in. Grants are fixed at environment creation; restart after
moving a worktree or changing its metadata layout.

This is repository permission, not branch ownership enforcement. Shared objects
and refs are writable, so an agent can affect other branches or damage shared
history. Worktrees separate working files and indexes, not the object database.
Destructive Git operations still require appropriate user authorization. Use an
independent clone if the original repository's metadata must remain untouched.

[Codex's default workspace policy](https://learn.chatgpt.com/docs/agent-approvals-security#protected-paths-in-writable-roots)
protects `.git` and can route broader operations through approval.
[Claude Code](https://code.claude.com/docs/en/sandboxing#filesystem-isolation)
permits Git metadata writes while protecting hooks and configuration. Overalls
provides an explicit launch-time opt-in and has no live approval mechanism.

## Network access

The default matches the ChatGPT desktop app's [documented workspace sandbox](https://developers.openai.com/codex/agent-approvals-security/#network-access):
workspace edits are allowed and command networking is off. Bub's model requests
remain outside this sandbox. Overalls does not implement Codex's approval prompts
or automatic network escalation; denied commands fail inside the sandbox.

Enable command networking for a Bub session:

```sh
BUB_OVERALLS_NETWORK_ACCESS=true uv run bub chat
```

Or override it for one command or diagnostic:

```sh
uv run bub-overalls exec --network enabled -- curl https://example.com
uv run bub-overalls doctor --network disabled
```

`BUB_OVERALLS_NETWORK_ACCESS` accepts `true/false`, `1/0`, `yes/no`, or `on/off`,
ignoring case and surrounding whitespace. Unset means `false`; empty or unknown
values raise an error.
`--network enabled|disabled` overrides that setting. In Python, use
`OverallsConfig(network_access=True)`. Settings are fixed when the environment
is created; sandboxed children cannot change their permissions by setting an
environment variable.

Enabled mode permits outbound IPv4/IPv6 TCP and UDP to **all destinations**,
including localhost and private networks, plus the macOS DNS resolver socket.
It grants no domain filtering, inbound listeners, other Unix sockets, or Mach
service access. Filesystem restrictions and credential filtering still apply.
Commands can transmit any data they can read, including workspace `.env` files.
Domain allowlists need an enforcing proxy and are not supported yet. Leave the
setting off and prepare dependencies separately when networking is unnecessary.

## Python lifecycle

Use `async with` to initialize the sandbox and close it automatically:

```python
from pathlib import Path

from bub_overalls import OverallsConfig, OverallsEnvironment

async with OverallsEnvironment(OverallsConfig.from_env(), Path.cwd()) as environment:
    await environment.write_text("example.txt", "hello")
```

Entry verifies Seatbelt with a real preflight. Exit closes the environment on
success, exceptions, and cancellation without suppressing the body's exception.
`await OverallsEnvironment.create(config, workspace)` remains available for
Bub-managed lifetimes; its caller must close it or use `async with environment`.
Closed environments cannot be entered again.

Process ownership lives in `process.py`. A private frozen dataclass groups a
fixed snapshot of children with its signal, wait, and stop operations. It does
not own the live registry. `OverallsProcessManager` owns subprocess
startup, registration, cancellation, and shutdown; the environment supplies the
Seatbelt command and owns its session files. Children starting during shutdown
receive its current termination policy when they register. Forced shutdown kills
registered groups immediately, even while another child is still starting. This
avoids a separate startup watcher and a second snapshot of late-starting children.
File workers use the manager's stop operation after releasing their pipe readers.

### Cancellation and signals

Normal `close()` and successful context exit send `SIGTERM` to tracked process
groups, allowing up to 1 second for termination handlers to finish. All groups
share one monotonic deadline; remaining groups then receive `SIGKILL`. Group
liveness is checked even after its leader exits. This follows the staged shutdown
used by Mosec's runtime controller.

Cancellation of process startup, file operations, code execution, or the context
body uses immediate `SIGKILL`. Exceptional context exit also skips the grace period.
Cancelling an ongoing graceful close forces termination immediately, including
while another child is starting. `CancelledError` propagates after cleanup, and
repeated cancellation cannot interrupt it. If startup also fails, expected OS
errors and invalid subprocess arguments preserve the caller's cancellation;
unexpected startup errors propagate instead of being hidden.

Shutdown stops new spawns and waits for in-flight startup to register its child
before removing session files. The grace deadline includes this startup wait;
settling startup itself has no separate timeout, so an unregistered child cannot
be abandoned. Waiting for processes after signaling is bounded to three seconds
and logs a warning on timeout. Signal or wait failures are collected while other
groups are cleaned up; session files are removed before an `ExceptionGroup` reports
those failures.
`spawn()` transfers the returned process to its caller: cancelling `process.wait()`
alone does not kill it. The caller must signal the group or close its environment.
Bub's shell manager owns background-command lifetime and graceful termination;
`process.signal(kill=False)` sends `SIGTERM`, while `kill=True` sends `SIGKILL`.
Callers should consume stdout and stderr concurrently while waiting.

Only the standalone CLI installs signal handlers. `SIGINT` and `SIGTERM` cancel
its command, run cleanup, and exit with 130 and 143 respectively; command timeout
exits with 124. Repeated signals leave cleanup running, and prior signal handlers
are restored on exit. Hosted plugins leave Bub's signal handlers untouched.
A launcher killed with `SIGKILL` cannot run cleanup.

## Boundaries

Each command runs in a new process group. Cancellation and close stop ordinary
descendants; a process that deliberately detaches can outlive cleanup while
remaining sandboxed. Seatbelt provides neither a separate kernel nor aggregate
CPU, memory, or PID limits. Sessions sharing a workspace see each other's writes.

Only operations using Bub's `Environment` are confined. Code-mode tool callbacks,
other plugins, MCP servers, and model requests have their own boundaries. The
launcher closes unrelated descriptors; stdin/stdout/stderr remain intentional
communication channels. Deployments requiring confinement must also verify plugin
discovery, since Bub itself can tolerate discovery failures or use a local fallback
outside a framework turn.

Apple deprecates `sandbox-exec`, and custom profiles use private interfaces.
Preflight verifies actual sandbox initialization; test new macOS versions before
relying on them. This backend is intended for local development.

## Development

```sh
make format
make check
make test
make test-sandbox
make benchmark
make package
```

Bub is a released PyPI dependency (`bub>=0.5.1`). The lower bound supplies the
Environment and prompt-hook APIs used by Overalls. `uv.lock` records the exact
version for reproducible development installs; package users can resolve newer
compatible releases.

Code lives in `src/bub_overalls/`, following Bub's package layout.
Package versions come from Git tags through PDM, matching `../defspec`. Builds
generate `bub_overalls/__version__.py`; the generated source file is ignored by
Git. Builds without SCM metadata use the `0.0.0` fallback.
Formatting follows `../spectree`: 88 columns and absolute imports. Ruff also
checks naming and Python 3.12 conventions; mypy runs in strict mode and follows
upstream Bub annotations. The Makefile follows SpecTree: `make` installs dependencies,
`make lint` checks source files, and `make package` cleans build artifacts first.
Real sandbox tests require macOS and permission to launch Seatbelt
outside any enclosing restrictive sandbox. See [validation](validation.md)
for test coverage and execution measurements.

The Python API exports `OverallsConfig` and `OverallsEnvironment` from
`bub_overalls`. Seatbelt profile generation uses `build_overalls_profile`.
The environment exposes its session temporary path as `temporary_path`; the
profile and temporary-directory owner are private implementation details.
