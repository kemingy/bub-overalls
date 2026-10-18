# Bub Overalls

A macOS sandbox plugin for [Bub](https://github.com/bubbuild/bub), using Seatbelt.
Runs your installed tools directly in your workspace. Requires Python 3.12+ and
macOS; Linux is not supported.

## Why Overalls?

Both projects run installed tools through Seatbelt on macOS. Overalls focuses on
explicit access for local development. Compared with the reviewed
[bub-sandbox-env revision](https://github.com/bubbuild/bub-contrib/tree/182014614453068feddc3b8a908d9dc75e42a5d9/packages/bub-sandbox-env):

| Default policy | Overalls | bub-sandbox-env |
| --- | --- | --- |
| File reads | Workspace, system/Python runtime, explicit grants | Whole filesystem except a denylist |
| Git writes | Opt-in; hooks, config, submodule metadata stay protected | Writable with the workspace |
| Inherited environment | Small allowlist | Inherit except deny patterns |
| Platforms | macOS | macOS and Linux |

Overalls protects nested `.git` and listed agent/IDE config paths, and tells the
model its active grants and how to request more access.
Both default to network off. See the [sandbox limits](docs/sandbox.md) before use.

## Quick start

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

Enable local Git operations with `BUB_OVERALLS_GIT_ACCESS=true`; hooks and Git
configuration remain read-only. Linked worktrees also grant writes to shared
objects and refs.

Workspace writes persist, and workspace `.env` files remain readable. Only tools
using Bub's `Environment` are sandboxed. Plugin logs use standard Python logging;
the standalone CLI logs at INFO on stderr (see the configuration guide).

[Configuration and limits](docs/sandbox.md) · [Validation](docs/validation.md)
