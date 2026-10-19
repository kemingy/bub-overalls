# Experimental Linux backend and Seatbelt differences

Linux support is selected automatically and does **not** reproduce the macOS policy.
It requires native x86_64 or aarch64 Python, Landlock ABI 6 or newer (introduced
in Linux 6.12), enabled Landlock LSM, seccomp, unprivileged user/mount/IPC namespaces,
and the modern mount API (`open_tree`, `mount_setattr`, `move_mount`). Run as a
non-root user. Offline commands also require a network namespace. Namespace
policies imposed by the host must permit setup.
A kernel version alone is insufficient. Older kernels, disabled LSMs, unsupported
architectures, or enclosing sandboxes may prevent startup; there is no host fallback.
No compiler, native extension, Bubblewrap package, or container runtime is needed.
On macOS, an existing OrbStack Linux guest can run Linux tests; this is separate
from the native macOS Seatbelt backend.

```sh
uv run bub-overalls doctor
uv run bub-overalls exec -- make test
# Network remains off unless separately enabled:
uv run bub-overalls exec --network enabled -- curl https://example.com
```

No Linux enablement setting is required. Startup queries the actual Landlock ABI
and exercises namespace, mount and seccomp support; it raises an initialization
error if any requirement is missing. Kernel release strings alone do not prove
that these facilities are enabled or permitted. An excluded Bub session can still
use Bub's host environment; matching sessions fail if their sandbox cannot initialize.

## What matches, and what cannot match

| Operation or boundary | macOS Seatbelt | Experimental Linux |
| --- | --- | --- |
| Command and file-helper execution | Kernel policy before command starts | Landlock and seccomp before command starts; descendants inherit both |
| Reads and writes outside positive content grants | Denied | Opens/creation/removal/rename/truncate denied by Landlock, subject to the metadata gaps below |
| Workspace, temp, explicit write roots | Writable with named exceptions | Writable except existing protected entries mounted read-only before each command |
| `.git`, `.codex`, `.ssh`, other protected names at any depth, including future files | Case-insensitive deny rules under every write root | Case-insensitive discovery at command startup; existing paths read-only. New names within a running command remain writable until the next command |
| Selective Git writes, protected hooks/config/layout, linked-worktree metadata | Supported with `GIT_ACCESS=true` | `GIT_ACCESS=true` fails initialization. Existing `.git` and resolved selected-repository metadata roots are mounted read-only, including linked-worktree common metadata |
| Network disabled | Denies IP and named Unix sockets | Private network namespace with no configured connectivity, plus seccomp denial of `socket()` for every family, including UDP, Unix, netlink, packet sockets. Anonymous `socketpair()` IPC stays available |
| Network enabled | Outbound IP and macOS DNS resolver socket; listeners and other Unix sockets denied | Uses the caller's network namespace. IPv4/IPv6 stream/datagram sockets allowed; explicit bind/listen/accept denied; Unix sockets denied. UDP automatic binding still permits receipt on the assigned port, including unsolicited traffic |
| DNS | macOS resolver socket allowed on opt-in | Ordinary DNS over IP with `/etc` resolver configuration. nscd/systemd Unix resolver sockets remain denied; libc fallback depends on the distribution |
| File metadata | Read metadata globally; deny write operations outside write grants | Protected read-only mounts also deny metadata changes through those mounts. Landlock alone does not mediate chmod/chown, timestamps, or extended attributes elsewhere |
| Signals and tracing | Signals limited to same sandbox | ABI 6 signal scoping; ptrace denied by seccomp. Independent launches have independent Landlock scopes |
| Other IPC and kernel interfaces | Default deny, including Mach service lookup | Private IPC namespace isolates System V IPC and POSIX message queues per command. Anonymous pipes/socket pairs remain usable. No comprehensive default-deny equivalent for other kernel interfaces |
| TTY, devices, `/proc` | Selected device and runtime access | Only null/zero writable, random/urandom readable by default; device ioctls denied. `/proc`, `/sys`, TTY and `/dev/fd` are not broadly granted; some tools will fail |
| Cleanup, resource limits, kernel boundary | Best-effort process groups, no cgroups/VM | Same lifecycle; no cgroups, PID namespace, VM, or detached-process guarantee |

Use a disposable checkout when continuous protected-name enforcement is required.
The Linux policy protects entries present at command startup, not every future
name. Symlinks do not add Landlock content grants. The launcher opens canonical roots with `openat2(RESOLVE_NO_SYMLINKS)`
to prevent earlier commands from redirecting later grants; replacement with a
symlink fails the next launch. Landlock grants
attach to filesystem objects rather than Seatbelt path expressions; host-side
renames and filesystem aliases need care. Existing/inherited descriptors are not
revoked by Landlock. Overalls closes nonstandard inherited descriptors; standard
input/output/error intentionally remain usable (even if the caller supplies sockets).
Neither backend provides domain filtering, live approvals, or escalation.

## Existing-path mount protection

Each launcher creates fresh user, mount and IPC namespaces with the caller's
UID/GID mapped unchanged. When networking is disabled, it also creates a fresh
network namespace without configuring interfaces or routes. Enabled networking
uses the caller's network namespace and retains the existing seccomp restrictions.
Namespace creation failures prevent target execution. Anonymous socket pairs
remain available for Bub code execution and tool callbacks. System V IPC and
POSIX message queues are private to that invocation and its descendants; separate
Environment commands cannot share them. Filesystem-backed IPC, such as POSIX
shared-memory files, still depends on filesystem grants; explicit grants can
expose shared files or mounted IPC filesystems.

Mount propagation is recursively private. The launcher scans writable
trees and the selected repository's resolved metadata roots before executing
the command. Protected names match the macOS list, including `.git`, ignoring
ASCII case. Protected ancestors above a write root do not by themselves lock
that root. Explicit nested write grants cannot unlock a protected mount.

`open_tree` clones protected entries through pinned file descriptors;
`mount_setattr` applies read-only/nodev/nosuid recursively to existing submounts;
`move_mount` attaches the configured trees through pinned destination descriptors.
The launcher reopens and verifies the mounted paths, clears its capabilities,
then applies Landlock and seccomp. Seccomp blocks both legacy and modern mount
operations after setup. No mount changes are made in the host namespace.

Startup fails on traversal errors, protected symlinks (including symlinks inside
protected trees), protected files with multiple hard links, protected special
files, and duplicate directory identities such as bind aliases in scanned trees.
Nested `.git` pointer files are rejected unless they identify the selected
repository resolved at Environment creation. Ordinary nested `.git` directories
are supported. The selected repository's layout must remain stable during the
Environment lifetime. Concurrent host changes, aliases outside scanned trees,
and mutations by a previously launched command are not a complete isolated
filesystem snapshot. Serialize commands and avoid concurrent host mutations
when relying on the startup scan.

Every command, including doctor and Bub startup preflight, also mounts a scratch
sentinel read-only. After restriction, it verifies an ordinary write succeeds
and protected overwrite, creation, chmod, and unlink fail with EROFS. A failed
check prevents target execution. Scratch files are removed with the Environment's
temporary directory; mounts live only in the child namespace and its descendants.
Scanning is proportional to tree size and may fail on very deep trees or too
many protected entries. There is no weaker backend fallback.

## Why this design

Landlock restricts filesystem operations for unprivileged descendants without
changing the host process. A fresh isolated Python launcher uses standard-library
`ctypes` for the Linux UAPI; a `preexec_fn` would be unsafe in a threaded Bub host.
The host snapshots launcher source once, passes it using `-c`, resolves the real
interpreter, and starts with `-I -S`. `landlock.py` builds grants and captures the
standalone `_linux.py` source; the latter imports only the standard library.
Its setup phases create namespaces, protect existing paths, prepare the mount
probe, drop capabilities, enforce Landlock/seccomp, and verify the probe before
executing the target. Policy serialization and decoding share one dataclass.
Target loader/environment overrides are applied only after restriction, and are passed in an environment payload rather
than exposed in command-line arguments. Python's base runtime must not overlap
write grants: otherwise a command could replace code used by a later bootstrap.
As with any launcher, its installed runtime and host process must be trusted.

The handled filesystem mask includes REFER (ABI 2), TRUNCATE (ABI 3), and device
IOCTL (ABI 5). ABI 6 is required for signal and abstract Unix socket scoping;
older kernels are rejected rather than silently dropping these restrictions.
Future ABI additions are not automatically enforced: this implementation handles
only the documented rights through ABI 6 and must be reviewed when adding rights.

At the ABI 6 baseline, Landlock's network rules cover TCP ports, not UDP or
pathname Unix sockets. Newer documented ABIs add pathname Unix socket resolution
(ABI 9) and UDP ports (ABI 10); this launcher does not enable those rights.
A small seccomp classic-BPF filter supplies socket-family/type restrictions and
blocks explicit listeners. It rejects foreign/compat and x32 syscall ABIs, and blocks io_uring
interfaces, cross-process FD acquisition, ptrace, and selected mount/namespace
interfaces. It is a targeted filter, **not** a comprehensive syscall allowlist.

## Component comparison and sensitivity

The table compares component responsibilities and alternative designs. Runtime
enforcement tests and startup measurements are recorded in [validation](validation.md).
There are deliberately no runtime switches that disable individual protections.

| Design alternative | Contribution and failure without it |
| --- | --- |
| Landlock only at ABI 6 | Content grants and inherited restrictions; TCP-only network rules cannot deny UDP or named Unix sockets |
| Seccomp only | Socket restrictions; a syscall filter cannot resolve path strings and enforce filesystem subtree grants |
| Landlock + seccomp without mounts | Combines content and socket restrictions; cannot protect existing named exceptions under writable parents |
| Scan writable trees and omit protected children | Cannot safely grant a writable parent: new names, renames, and concurrent mutations defeat the intended exception list |
| Workspace without Landlock write grants | Protects content subject to metadata gaps; a separate output tree changes the Environment contract |
| Landlock + seccomp + mount/user namespaces (implemented) | Read-only mounts protect existing paths; do not express protected names created anywhere in future |
| Private IPC namespace (implemented) | Separates System V IPC and POSIX message queues from the caller and other launches; does not isolate shared filesystem objects |
| Private network namespace for offline commands (implemented) | Adds a separate network stack beneath the socket filter; enabled commands retain the caller's stack |
| Restricted writes through a broker | Can validate names when publishing changes; changes the execution contract and adds trusted code |

See [protected-path research](landlock-protected-paths.md) for why ruleset stacking
does not supply deny exceptions, what narrower Landlock policies can enforce, and
how namespaces and a filesystem broker differ. The broker and narrower output-only
designs remain alternatives, not implemented backends or automatic fallbacks.

Portable tests vary network opt-in, native architecture, socket family, compat/x32
ABI selection, and old/unavailable Landlock ABIs. Integration tests exercise both
network modes with real TCP/UDP IPv4/IPv6 listeners, named Unix sockets, explicit
bind denial, file grants, symlink denial, inherited restrictions, signal scoping,
and Bub's asynchronous code runner in both modes. Namespace tests verify
separation from the caller, inheritance by descendants, denied access to a live
host System V message queue and working private queues. Mount tests cover
allowed sibling writes,
protected content/metadata/rename denial, nested mounts, bind aliases, hardlinks,
symlinks, linked worktrees, and capability removal. They also demonstrate the writable named-path
gap using inert sentinel files. Broader write roots expand all descendant writes;
network opt-in enables all IP destinations. Neither is a domain or port allowlist.

## Validation status

Portable tests verify policy construction,
filter branches, startup failure handling and environment separation, but **do not
prove Linux kernel enforcement**. Linux integration tests run separately, marked
`landlock` and `sandbox`; run `make test-sandbox` on a suitable Linux machine.
Tests fail rather than skip when the kernel lacks required features on Linux.
See [validation](validation.md) for the latest local results and CI scope.
