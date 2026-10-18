# Validation

Checked on 2026-10-18 with Python 3.13.14 on macOS ARM64 (Darwin 25.6.0).
Bub 0.5.1 is installed from the package index, satisfying `bub>=0.5.1`.
The checks below pass against that released version.
Linux is unsupported; Intel Macs and other macOS/Python versions are not tested here.

## Verification

- `make check`: lock validation, Ruff format/lint, and mypy across package, tests,
  and scripts pass. Formatting follows `../spectree`, with Ruff naming and
  modernization rules added. Mypy uses strict mode and follows upstream Bub
  annotations rather than treating its environment API as `Any`.
- `make test`: 158 unit tests pass.
- `make test-sandbox`: 79 real Seatbelt tests pass.
- `make package`: wheel and source distribution build under `bub-overalls`.
- Wheel inspection verifies the package, typed marker, CLI/plugin entry points,
  Python minimum, and Bub as the only direct runtime dependency. The extracted
  wheel imports successfully outside the editable package.
- Bub discovers `provide_environment: builtin, overalls`. Environment-variable
  configuration is exercised through the plugin against real Seatbelt.

Real sandbox tests cover native commands, streams, live file writes, denied
reads/writes outside grants, symlinks, protected metadata, explicit path grants,
TCP/Unix-socket denial against working listeners, IPv4/IPv6 TCP and UDP opt-in,
inbound listener denial in both modes, retained filesystem and Unix-socket
restrictions with networking enabled, code callbacks, cancellation,
process-group cleanup, credential filtering, Unicode paths, CLI cleanup, and
async context cleanup on success, exceptions, and cancellation. Context tests
cover direct construction and environments returned by `create()`. Signal tests
verify group-wide SIGTERM/SIGKILL, cleanup after the leader exits, cancellation of
raw waiters, and standalone CLI SIGINT/SIGTERM/timeout exits with descendants stopped,
including full stdout/stderr pipes whose consumers do not read. Git tests exercise
staging, commits, revert, reset, and status in regular repositories and linked
worktrees, an initial commit with a separate Git directory, repository-local author identity
without home-config access, read-only defaults, protected config/hooks/layout pointers, and
unchanged original/sibling worktree metadata even with overlapping write grants.
Additional regressions verify that submodule config, hooks, and other metadata
remain read-only in ordinary and linked worktrees with Git access on or off;
nested Git and agent/IDE paths stay read-only in workspace, extra write roots,
and session temporary directories. Recursive tests cover mixed case, gitfiles,
creation, deletion, renaming, symlink aliases, overlapping direct grants,
regex metacharacters and Unicode in roots, and writable lookalike names such as
`.github`. Workspaces under `.claude/worktrees`, `.codex/worktrees`, and `.git`,
plus grants under `.cursor`, remain writable while their nested metadata stays
protected. Linked-worktree commits under both agent directories and root case
aliases on a case-insensitive filesystem are exercised.
Malformed Git layouts permit workspace edits only in read-only Git mode. These
tests use inert writes and do not execute host-side hook or fsmonitor payloads.
Staged shutdown tests verify a real termination handler completes, ignored SIGTERM
escalates with both zero and 0.05-second grace periods, and a descendant ignoring
SIGTERM is killed even after its leader exits. Normal operation uses a shared
1-second grace period; cancellation bypasses it.
Unit tests cover argument parsing, exit codes, errors, configuration, initialization
failure, standard host logging and standalone CLI log levels, file-operation
routing, nonblocking output cancellation, descriptor flag restoration for merged
streams, byte-preserving partial writes, environment-variable grants, explicit
session selection including the documented default channel IDs, rejected
malformed session arrays and blank entries, startup diagnostics, plugin preflight failures,
model guidance through Bub's hook dispatcher, active grants despite changed launch variables,
unchanged request history, duplicate guidance prevention, other environment providers,
Git opt-in parsing, directory/gitfile/worktree metadata discovery, rejected
malformed metadata and worktree backlinks,
unsupported write-root characters (quotes, backslashes, and control characters),
immediate removal of real temporary directories after workspace, write-grant,
or temporary-root rejection while their owners remain referenced,
network configuration validation,
CLI network overrides, common boolean
spellings, malformed and empty path entries, relative
and home-relative grants, symlink normalization, deleted grants, failed context
entry cleanup, rejection of context entry after close, repeated cancellation during
startup/file/code/close cleanup, expected startup failures preserving cancellation,
unexpected startup errors remaining visible, startup racing close, idempotent hard termination, bounded cleanup waits, graceful
shutdown and escalation, forced shutdown during startup, and continued cleanup
after signal or wait failures. No
model/provider credentials are needed.

The process-manager refactor preserves the same lifecycle suite. The environment
now delegates process ownership to `process.py`; tests still exercise repeated
cancellation, late startup, forced shutdown during startup, group escalation,
and cleanup after signal or wait failures. Session files remain until owned
startup and process cleanup finish.

A separate live smoke check through `bub-overalls exec --network enabled` resolved
`example.com` and completed a verified HTTPS request with `/usr/bin/curl` (HTTP
200). The automated suite uses local listeners and requires no external service.

## Execution comparison

Raw data: [benchmark-seatbelt-macos-arm64.json](benchmark-seatbelt-macos-arm64.json).
These timings were collected before switching Bub's dependency source.
Reproduce with `make benchmark`, or save the output using:

```sh
uv run python scripts/benchmark.py --output docs/benchmark-seatbelt-macos-arm64.json
```

| Measurement | Direct native | Seatbelt |
| --- | ---: | ---: |
| `true` median, 30 samples | 4.392 ms | 16.499 ms |
| `true` p95 | 6.701 ms | 21.213 ms |
| Integer loop median, 5 samples | 67.229 ms | 65.773 ms |

Creation including actual preflight took 21.368 ms. Paired runs alternate
order after warmup and use the same Python binary. Command timing includes startup
and pipes; computation is timed inside the process. No sandbox tests ran alongside
this benchmark. Small samples and host scheduling limit conclusions; this is not
a general throughput guarantee. CPU/RAM tuning, large builds, file-heavy workloads,
concurrency, and grant-size sensitivity are not evaluated.

This isolates execution from the model, planning, and retrieval by replaying fixed
workloads. It does not evaluate those agent components or end-to-end task quality.
Permission and lifecycle limits are described in the [sandbox guide](sandbox.md).
