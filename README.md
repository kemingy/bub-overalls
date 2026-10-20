# Bub Overalls

A sandbox plugin for [Bub](https://github.com/bubbuild/bub), using macOS Seatbelt
or experimental Linux Landlock, seccomp, and private read-only mounts. Runs installed tools directly in
your workspace. Requires Python 3.12+. Linux selects its backend automatically and has
[different protections](docs/landlock.md), including a gap for protected names
created during a command.

## Why Overalls?

Both projects run installed tools through Seatbelt on macOS. Overalls focuses on
explicit access for local development. Compared with the reviewed
[bub-sandbox-env revision](https://github.com/bubbuild/bub-contrib/tree/182014614453068feddc3b8a908d9dc75e42a5d9/packages/bub-sandbox-env):

| Default policy | Overalls | bub-sandbox-env |
| --- | --- | --- |
| File reads | Workspace, system/Python runtime, explicit grants | Whole filesystem except a denylist |
| Git writes | Opt-in; hooks, config, submodule metadata stay protected | Writable with the workspace |
| Inherited environment | Small allowlist | Inherit except deny patterns |
| Platforms | macOS; experimental Linux | macOS and Linux |

On macOS, Overalls protects nested `.git` and listed agent/IDE config paths, and tells the
model its active grants and how to request more access.
Both default to network off. See the [sandbox limits](docs/sandbox.md) before use.

## Quick start

On Linux, read the [Landlock differences](docs/landlock.md). The backend is
selected automatically; missing kernel features raise an initialization error.
Existing Git metadata and protected
settings are mounted read-only at each command startup; names created during a
command remain writable until the next command. `GIT_ACCESS=true` is unsupported.
Requires Landlock ABI 6+, seccomp, unprivileged user/mount/IPC namespaces, the
modern mount API, and a non-root caller on native x86_64/aarch64. Offline commands
also use a private network namespace; enabling networking retains the caller's
network namespace with socket restrictions.

Bub is installed from PyPI (`bub>=0.5.1`):

```sh
cd bub-overalls
make install
uv run bub-overalls doctor
uv run bub chat
```

Installation enables `overalls` for all Bub sessions. Configure it through
`BUB_OVERALLS_*` environment variables. To run a command directly:

```sh
uv run bub-overalls exec -- /usr/bin/uname -s
```

Command networking is off by default, like ChatGPT's workspace sandbox; opt in
with `BUB_OVERALLS_NETWORK_ACCESS=true`. Model API calls and separate web/MCP tools
are outside Overalls. Reading a URL requires a fetch tool; the model alone cannot
retrieve it.

On macOS, enable local Git operations with `BUB_OVERALLS_GIT_ACCESS=true`; hooks and Git
configuration remain read-only. Linked worktrees also grant writes to shared
objects and refs.

Workspace writes persist, and workspace `.env` files remain readable. Only tools
using Bub's `Environment` are sandboxed. Plugin logs use standard Python logging;
the standalone CLI logs at INFO on stderr (see the configuration guide).

[Configuration and limits](docs/sandbox.md) · [Validation](docs/validation.md)
