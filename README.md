# bub-sandbox

Experimental [Bub](https://github.com/bubbuild/bub) execution environment using
[libkrun](https://github.com/libkrun/libkrun) 1.19.6. One session owns one Linux VM.
The host runs Bub; a small Python service inside the guest executes commands and
accesses files through vsock. Shell output, cancellation, background commands, and
code-mode callbacks use Bub's existing `Environment` and `Process` contracts.

Supported targets are macOS 14+ on Apple Silicon and Linux on x86-64/ARM64 with
read/write access to `/dev/kvm`. Intel Macs are unsupported. The initial prototype
is developed on macOS; see [validation](docs/validation.md) for actual coverage.

## Build without Homebrew

Prerequisites: Python 3.12+, `uv`, a Rust toolchain, a C compiler, Make, and a running
Docker engine. macOS needs Apple's command-line developer tools (`cc`,
`install_name_tool`, `codesign`). Linux needs its native compiler and linker.
Docker builds the small Linux init executable and guest filesystem; it is not
used to run sandbox sessions. No privileged containers or host directory mounts
are required by the build scripts.

Keep the current Bub checkout next to this repository, because its `Environment`
API may be newer than the published package:

```text
GitHub/
  bub/
  bub-sandbox/
```

```sh
cd ../bub-sandbox
make install
make runtime
make native
make image
uv run bub-sandbox doctor
make smoke
```

`make runtime` downloads checksum-pinned libkrun source and libkrunfw 5.6.2
artifacts under `build/sources`, compiles a headless VMM with block support, and
places the runtime under `build/runtime`. It does not install system packages.
GPU, sound, input, networking, and device passthrough are not enabled. The macOS
helper is ad-hoc signed with the Hypervisor entitlement.

The guest uses Python 3.13 on Debian Bookworm. Image tags and Debian package
indexes are mutable; the current image build is a development recipe, not a
reproducible supply-chain release. Internet access is needed during builds.

The private runtime includes dependencies with their own licenses, including the
Linux kernel. Keep upstream notices and satisfy their redistribution requirements
when distributing binaries. This repository does not commit runtime binaries.

## Try it without an LLM

```sh
uv run bub-sandbox exec -- uname -a
uv run bub-sandbox exec -- python3 -c 'import os; print(os.getuid())'
make test
make test-vm
make benchmark
```

`exec` creates a session, copies the selected host workspace into `/workspace`,
runs the command, and destroys the VM. Use `--workspace PATH` before `--` to pick
the source directory. The image has Python, bash, and git; add other development
dependencies to `guest/Dockerfile` and rebuild. Guests have no external network.

## Use with Bub

`uv sync` installs the `bub` entry-point plugin in this project's environment.
Installing the plugin selects the VM environment for framework sessions:

```sh
export BUB_SANDBOX_HELPER="$PWD/build/bub-krun"
export BUB_SANDBOX_IMAGE="$PWD/build/guest.raw"
uv run bub hooks
uv run bub --workspace /path/to/project chat
```

Use absolute helper/image paths when running from another directory. Existing
`BUB_MODEL` and provider settings still configure the host-side LLM. They are not
inherited by guest commands. Installing this plugin into another Bub environment
requires a compatible checkout exposing `bub.environment`; use the same virtual
environment as that Bub executable.

| Variable | Default | Meaning |
| --- | --- | --- |
| `BUB_SANDBOX_HELPER` | `build/bub-krun` | Signed/native helper executable |
| `BUB_SANDBOX_IMAGE` | `build/guest.raw` | Base ext4 image, with adjacent `.json` manifest |
| `BUB_SANDBOX_CPUS` | `2` | Guest vCPUs, 1–64 |
| `BUB_SANDBOX_MEMORY_MIB` | `1024` | Guest memory, 128–65536 MiB |
| `BUB_SANDBOX_BOOT_TIMEOUT` | `30` | Maximum seconds waiting for guest readiness |
| `BUB_SANDBOX_IMPORT_WORKSPACE` | `1` | Set `0` to start with an empty workspace |

## Files, isolation, and lifecycle

- Each session clones the base raw disk with APFS copy-on-write or Linux reflink
  where supported. Linux falls back to a normal copy if reflinks are unavailable.
  The clone and RPC socket live in a private mode-0700 temporary directory.
- The root filesystem uses a block device. libkrun's synthetic NullFs supplies its
  init executable; **no host directory is exported over virtio-fs**.
- Workspace import copies regular files and directories. It skips symlinks,
  `.git`, `.venv`, `.aws`, `.ssh`, `.codex`, `node_modules`, `__pycache__`, `build`, `dist`, common test/lint caches, and names
  starting with `.env`, at every level. This is a convenience filter, not secret
  detection: ordinary source files can contain credentials. Use a curated
  workspace or disable import for sensitive projects. Import is limited to
  128 MiB and 10,000 entries.
- Guest writes stay in the session disk. **Automatic export/synchronization back
  to the host is not implemented.** Read results through Bub's file tools or
  command output before ending a session. Session disks are disposable.
- Commands and filesystem workers run as guest UID/GID 1000, with
  `PR_SET_NO_NEW_PRIVS`. A root guest supervisor owns command cgroups. cgroup v2
  and `cgroup.kill` are required; startup fails if unavailable. The PID limit is
  256 per command, with at most 128 active commands per session.
- The vsock device is explicitly created with TSI disabled. No NIC or host IP
  socket proxy is configured. Private host sockets are not exposed to workloads.
- Protocol frames and unread process streams are bounded at 8 MiB. Excess unread
  output aborts the VM instead of dropping bytes silently. Consumers should drain
  stdout and stderr concurrently.
- `close()` destroys the VM and temporary disk. Abrupt host termination can leave
  temporary files or orphan a helper; crash recovery/reaping is not implemented.

This is a development prototype, not a hardened multi-tenant sandbox. The VMM
runs as the current host user; host OS confinement and a security review are
still needed for hostile workloads. VM CPU/memory settings do not provide a full
host resource accounting policy.

Only tools using Bub's `Environment` execute in the guest. Other plugin tools,
LLM calls, and code-mode tool callbacks run on the host. Bub subagents currently
inherit their parent's environment through copied state. Separate subagent IDs
therefore do not create separate VMs.

Initialization failures propagate and do not return `None` or select a local
environment. However, Bub itself tolerates plugin import/registration failures
and SDK tools can use its local-environment fallback. A deployment requiring
isolation must verify plugin discovery and environment selection at its outer
entrypoint; this plugin cannot enforce that when it was not loaded at all.

## Verification and performance

The Makefile owns formatting, lint, typing, tests, image/runtime builds, and VM
checks. `make test` does not require libkrun. `make test-vm` runs the actual guest
smoke test; skipped VM tests are not evidence of virtualization support.

`make benchmark` records deterministic execution ablations under
`build/benchmark.json`: warm session commands versus one VM per command, guest
compute versus host compute, 1/2 vCPUs, 512/1024 MiB RAM, and four concurrent
commands. Planning, model calls, and memory retrieval are bypassed so their
variance does not obscure execution costs. See [validation](docs/validation.md)
for measurements and limitations.
