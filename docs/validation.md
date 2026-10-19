# Validation

Local results recorded on 2026-10-20: macOS ARM64 (Darwin 25.6.0), Python 3.14.3,
and released Bub 0.5.1. These results do not establish support for other hosts.

| Command | Result |
| --- | --- |
| `make check` | Lockfile, Ruff format/lint, and strict mypy pass |
| `make test` | 189 unit tests pass, including portable Linux policy/launcher checks |
| `make test-sandbox` | 79 real Seatbelt tests pass; 33 Linux tests skipped |
| `make package` | Wheel and source distribution build |

Earlier package inspection (2026-10-18) also verified CLI/plugin entry points, the typed marker,
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

Tests need no model credentials or external services. Real Seatbelt tests require
macOS and permission to launch Seatbelt outside any enclosing restrictive sandbox.
Linux tests additionally require Landlock ABI 6+, native x86_64/aarch64, seccomp,
unprivileged user/mount/IPC namespaces (plus a network namespace when offline),
the modern mount API, and a non-root caller.
Portable checks and skipped tests do not establish kernel enforcement. See the
[Linux comparison](landlock.md).
Security regressions use inert writes rather than host-executed payloads.
A separate manual check verified DNS and HTTPS with networking enabled.

## Earlier OrbStack Linux validation

Executed for an earlier revision in an OrbStack 2.2.4 Ubuntu guest on this Mac,
using a separate checkout on the guest's native `/tmp` filesystem. No Docker
container was needed.
Guest: Ubuntu resolute, aarch64, Python 3.14.4, Bub 0.5.1; kernel
`7.2.9-orbstack-00391-g2b298d05fbd4`, Landlock ABI 10. The launcher still handles
only rights through ABI 6. These results do not establish x86_64 Linux, other
kernel configurations, or macOS-shared-filesystem compatibility.

| Command | Result |
| --- | --- |
| `make test` | 185 portable unit tests pass |
| `make test-sandbox` | 29 real Linux tests pass; 79 macOS tests skipped |
| `bub-overalls doctor` | Actual mount/Landlock/seccomp preflight passes, network disabled |

The current results use automatic platform selection with the former Linux
enablement environment variable unset. Missing/old Landlock ABIs and namespace
setup failures remain covered by portable fail-closed tests. The current suite
also covers cross-process memory/comparison syscall denial and real Linux CLI
`doctor`, `exec`, and stalled-stderr signal cleanup.

Real Linux tests cover content grants, networking off/on (TCP/UDP IPv4/IPv6),
Unix-socket denial, explicit listener denial, signals, symlink grant replacement,
and Bub code/tool execution in both network modes. Namespace tests check a private
IPC namespace in both modes, a separate network namespace only when offline,
and inheritance by descendants. A real host System V message queue is inaccessible
while a private queue works; the host message remains intact. Mount tests check
existing `.git`, `.ssh`, `.codex`
and nested mixed-case entries; denied overwrite, truncate, unlink, rename,
creation, chmod and timestamps; allowed ordinary edits; selected linked-worktree
metadata; recursive protection across a nested tmpfs mount; bind-alias rejection;
protected symlink/hardlink rejection; capability removal and blocked mount APIs.
An explicit nested write grant cannot unlock a protected mount. The host remains
writable. A separate test confirms new protected names remain writable within the
creating command and become read-only on the next command.

Linux probe programs live in `tests/fixtures/linux/` as normal Python files,
covered by the same Ruff and mypy checks as the test suite. Tests copy probes
into their existing workspace grants and execute them with `python -I`, passing
parameters as arguments. The mount-topology harness runs in a separate trusted
process to prepare test mounts before launching the actual sandbox.

### Startup sensitivity

Disposable native-guest directories; three sequential runs per case, reporting
median wall time for Environment initialization/preflight and close. File creation
is excluded. Each protected root is a separate `group-N/.ssh` with one inert file.
The baseline predates IPC/network namespace isolation; the current run includes
both with networking disabled. All protections present in each version remain
enabled. Runs were taken at different times. The small sample, warm filesystem
cache, and guest scheduling make small differences noise; these are observations,
not performance guarantees or isolated component-overhead measurements.

| Ordinary files | Protected roots | Earlier baseline | With IPC/offline network namespaces |
| --- | --- | --- | --- |
| 0 | 0 | 21.07 ms | 25.76 ms |
| 0 | 10 | 21.63 ms | 23.64 ms |
| 1,000 | 0 | 24.31 ms | 39.11 ms |
| 1,000 | 10 | 22.91 ms | 59.80 ms |
| 10,000 | 0 | 60.42 ms | 78.55 ms |
| 10,000 | 10 | 59.55 ms | 93.57 ms |

Tree size increased cost in both samples. The current measurements ranged from
23.61 to 145.38 ms across individual runs. Scheduling variation and the small
sample prevent attributing the before/after differences to either namespace
component alone. Directory depth, large mount counts, cold caches, filesystem
aliases and concurrent host mutations need separate study.
The [component comparison](landlock.md#component-comparison-and-sensitivity)
explains each enforcement component's role and the alternative designs.

## CI

The [workflow](../.github/workflows/check.yml) runs shared lint, type checks,
lockfile validation, and package builds once on Ubuntu 26.04 with Python 3.12.
Unit and real sandbox tests run across Python 3.12–3.14 on all four runners:
macOS 15 Intel (`macos-15-intel`) and ARM64 (`macos-15`), and Ubuntu 26.04
x86_64 (`ubuntu-26.04`) and aarch64 (`ubuntu-26.04-arm`). These twelve jobs
verify their Python architecture and run Seatbelt or Landlock tests according
to the platform markers.
Triggers: pushes to `main`, PRs targeting `main`, and manual dispatch.
Consult CI runs for hosted results; the local counts above are separate evidence.
The experimental Linux jobs run real Landlock/seccomp/private-mount tests.
Their host kernels must expose ABI 6+ and permit the required unprivileged
namespaces: Linux tests fail if required features are unavailable.
See [sandbox boundaries](sandbox.md) and the [Linux comparison](landlock.md)
for remaining limits.
