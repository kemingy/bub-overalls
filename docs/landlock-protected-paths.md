# Protecting paths with Landlock

Research reviewed on 2026-10-19. The launcher now combines Landlock and seccomp
with private read-only mounts for existing protected entries. This document
explains the design alternatives and the remaining protected-name gap. See the
[implemented policy](landlock.md) and [runtime results](validation.md).

Landlock can protect a known directory by withholding access to it. It cannot
express our full Seatbelt policy: arbitrary editing beneath a root, except for
case-insensitive protected names at any depth, including names created later.
These are different requirements. Protecting `~/.ssh` outside every content grant
already works for file contents; protecting `.ssh` inside a writable workspace
requires a different grant layout or another enforcement mechanism.

## Why an extra ruleset does not supply exceptions

The kernel specifies OR between matching grants within a policy layer, and AND
between enforced layers. For example:

```text
layer 1: workspace writable
layer 2: workspace readable; workspace/.git/hooks readable
result:  workspace writes denied everywhere
```

Adding workspace write access to layer 2 also allows writes beneath hooks again.
The child read grant does not subtract inherited writes. A rule identifies a
filesystem object/subtree and allowed actions; it has no denied-name or regex
field. Stacking can narrow a policy only when the narrower policy is itself
expressible as positive grants. [1][2]

Enumerating existing files offers a narrower policy, with compatibility costs.
Granting WRITE_FILE and TRUNCATE to individual ordinary files can allow in-place
edits while omitting protected objects. Creation/removal rights belong to their
parent directories, however, and do not select a child's name. File grants alone
do not support normal new-file creation and editor atomic replacement. Granting
an entire clean subtree restores those operations but also permits future
protected names inside it. Denying REFER limits cross-directory moves/links;
it does not filter names or stop same-directory renames. [1][2]

Scanning on every command does not close mutations within a running command.
Further Landlock enforcement can only tighten an existing domain, so refreshing
an allowlist cannot restore rights denied by an earlier layer. Filesystem
watchers detect changes after they happen and cannot serve as enforcement. [1]

## A useful Landlock-only policy

Keep the workspace readable without broad write grants. Grant writes only to
explicit output directories that may contain arbitrary untrusted data, and
optionally to selected existing source files. Keep Git metadata, credentials,
and trusted settings outside every writable subtree. The kernel recommends
self-contained leaf grants. [1]

This protects content in the excluded objects, subject to Landlock's metadata
and preopened-descriptor limitations. It sacrifices unrestricted editing,
creation, and rename at the workspace root. Even an allowed `src/` subtree could
contain a newly created `src/.ssh`; it must not be presented as the existing
protected-name policy. Selective Git access also needs creation of lockfiles and
atomic rename; allowing `.git/objects` and `.git/refs` alone is not an equivalent
implementation of our Git mode. [1][2]

## What a mount namespace adds

A mount namespace gives a process its own view of filesystem mounts. For
example, a launcher can expose a writable workspace but mount its existing
`.git` read-only, or use more granular mounts for existing protected paths.
Other host processes retain their original mount view. Ordinary writable bind
mounts still modify host files; this does not copy the workspace. [3]

An unprivileged launcher can use a user namespace to obtain the capabilities
needed to configure its mount namespace where the host permits this. Python can
call the kernel/libc interfaces with `ctypes`; a Bubblewrap Python package is
not necessary. This is still a sandbox implementation to maintain and audit. [4]

Setup must precede filesystem Landlock enforcement, which prohibits mount
topology changes. It needs private mount propagation, safe path resolution,
read-only protection covering nested mounts, pinned metadata/layout paths,
removal of writable aliases and inherited descriptors, capability dropping, and
restrictions against subsequent namespace/mount manipulation. A read-only bind
mount protects access through that mount; another writable path to the same
inode is a separate concern. Host-side concurrent renames also need analysis.
The availability probe must exercise actual namespace and mount operations and
fail closed when the host disallows them. [1][3][4]

This can protect existing `.git/hooks` or `.ssh` while retaining most live
workspace editing. It does not automatically protect an absent `.ssh`, every
future nested repository, or every case variant created later. Read-only mounts
are concrete mountpoints, not filename predicates. It is a useful narrower
contract, not full Seatbelt equivalence.

## What a filesystem broker adds

A broker is a separate trusted process with authority that the command lacks.
The command requests an operation, and the broker validates and performs it.
Wrapping our Python file helpers alone does not cover shell commands or native
programs; their direct filesystem writes must be denied by the kernel too.

A practical variant gives commands a separate writable copy/staging tree while
keeping the host workspace read-only. A trusted apply operation publishes only
approved changes, rejecting protected path components. Ordinary shell tools can
work on the copy. They no longer edit host files live. Copy isolation, symlinks,
hardlinks, deletions, metadata, conflicts, and concurrent host edits all become
part of the broker's security and consistency design. Writable copies must not
share writable inodes with the host via hardlinks.

A transparent broker for arbitrary commands is substantially larger: it must
mediate file opens, creation, unlink, rename, links, metadata, relative paths,
directory descriptors, and descriptor lifetime. The seccomp notification manual
explicitly warns against inspecting pathname arguments and then using CONTINUE
as a security check: arguments can change before the syscall executes. A broker
must perform constrained operations itself rather than approve a racy syscall
based on a previously inspected string. [5]

## Recommendation and validation

The implemented backend retains Landlock for content grants and seccomp for
socket restrictions, and adds private read-only mounts for existing protected
entries. It preserves live workspace edits with an explicit weaker guarantee for
future names. A narrower output-only policy remains simpler when unrestricted
editing is unnecessary. A staging tree plus trusted apply operation is an
alternative for complete protected-name checks, but changes the Environment
contract. None is a silent fallback to weaker enforcement.

Real OrbStack Linux tests cover allowed edits/new files and denied protected-file
overwrite, truncate, unlink, rename, and metadata changes. They vary existing
versus absent entries, nested/case-varied names, symlink/hardlink and bind aliases,
linked worktrees, nested mounts, and writes across commands. Capability removal
and blocked mount syscalls are checked. Namespace setup failures are also covered
by portable tests. Host concurrency and aliases outside scanned trees remain
limitations, not established protections. See [validation](validation.md) for
counts, startup measurements against tree size/mount count, and untested hosts.

This mount design has established precedents: Bubblewrap provides `--ro-bind`,
and systemd provides `BindReadOnlyPaths=`. Their production implementations cover
more configurations and compatibility paths than this deliberately restricted
launcher. We require the modern mount API and fail closed if unavailable. [6][7]

## Sources

1. [Linux 6.12 Landlock documentation](https://docs.kernel.org/6.12/userspace-api/landlock.html): layers, good practices, inheritance, metadata gaps, descriptors, mount topology, and REFER.
2. [Current Landlock documentation](https://docs.kernel.org/userspace-api/landlock.html): path-beneath rule structure, filesystem rights, and newer ABI features. ABI 9 adds pathname Unix socket resolution; ABI 10 adds UDP port rights. Neither adds protected-name exceptions. Our launcher handles rights through ABI 6.
3. [mount_namespaces(7)](https://man7.org/linux/man-pages/man7/mount_namespaces.7.html): mount views, propagation, read-only mounts, and cross-namespace mutations.
4. [user_namespaces(7)](https://man7.org/linux/man-pages/man7/user_namespaces.7.html): namespace capabilities, bind mounts, UID/GID mapping, and availability.
5. [seccomp_unotify(2), NOTES](https://man7.org/linux/man-pages/man2/seccomp_unotify.2.html#NOTES): syscall argument races and the limits of userspace notification.

6. [Bubblewrap](https://github.com/containers/bubblewrap) and its [bind mount implementation](https://github.com/containers/bubblewrap/blob/main/bind-mount.c).
7. [systemd.exec: BindPaths and BindReadOnlyPaths](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#BindPaths=).
