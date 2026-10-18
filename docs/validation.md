# Validation

Local results recorded on 2026-10-18: macOS ARM64 (Darwin 25.6.0), Python 3.13.14,
and released Bub 0.5.1. These results do not establish support for other hosts.

| Command | Result |
| --- | --- |
| `make check` | Lockfile, Ruff format/lint, and strict mypy pass |
| `make test` | 158 unit tests pass |
| `make test-sandbox` | 79 real Seatbelt tests pass |
| `make package` | Wheel and source distribution build |

Package inspection also verified CLI/plugin entry points, the typed marker,
Bub discovery, and imports outside the editable checkout.

## Coverage

- Configuration and integration: path/boolean validation, session selection,
  active-grant prompt guidance, initialization failures, and host-owned logging.
- Filesystem and Git: actual allowed/denied operations, symlinks, nested protected
  names, agent-managed worktrees, unusual root characters, Git opt-in, shared
  worktree storage, and protected hooks/config/submodule metadata.
- Network: local listeners verify default denial and outbound IPv4/IPv6 TCP/UDP
  opt-in; inbound listeners and unrelated Unix sockets remain denied.
- Lifecycle: process-group cleanup, TERM-to-KILL escalation, repeated cancellation,
  startup/shutdown races, blocked pipes, CLI signals/timeouts, and temp cleanup.

Tests need no model credentials or external services. Real sandbox tests require
macOS and permission to launch Seatbelt outside any enclosing restrictive sandbox.
Security regressions use inert writes rather than host-executed payloads.
A separate manual check verified DNS and HTTPS with networking enabled.

## CI

The [workflow](../.github/workflows/check.yml) checks and builds on macOS ARM64.
Unit and real Seatbelt tests run on macOS 15 Intel (`macos-15-intel`) and ARM64
(`macos-15`) with Python 3.12–3.14. Each job verifies its Python architecture.
Triggers: pushes to `main`, PRs targeting `main`, and manual dispatch.
Consult CI runs for hosted results; the local counts above are separate evidence.
Linux is not implemented. See [sandbox boundaries](sandbox.md) for remaining limits.
