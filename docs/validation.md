# Validation

Measured on 2026-10-16 on macOS 26.7.1 / Apple Silicon ARM64, using the private
source-built libkrun 1.19.6 runtime and libkrunfw 5.6.2. Linked-library inspection
confirmed the VMM uses Apple system libraries/frameworks and the project-local
firmware; there are no Homebrew runtime dependencies.

## Checks actually run

- `make check`: Ruff lint, formatting, and mypy pass.
- `make test`: 13 unit/integration tests pass without a VM.
- `make test-vm`: 4 real-VM tests pass: execution/isolation smoke checks,
  independent session disks, code timeout cleanup, and VM crash cleanup.
- `bub-sandbox doctor`: native helper and image metadata preflight pass.
- `bub hooks`: entry-point discovery reports `provide_environment: builtin, sandbox`.
- `make benchmark`: completed with five compute samples and twenty warm commands
  per configuration, plus five fresh-VM commands. VM tests were not running
  during the recorded benchmark.

The smoke test boots Linux AArch64, executes as UID 1000, imports a small
workspace while omitting `.env`, round-trips files without host writes, checks
exit codes/stdout/stderr/binary stdin, runs a Bub code-mode tool callback, kills a
detached descendant through its cgroup, and verifies external IP connection
failure. Readiness including the small workspace import was about 0.5 seconds.

Linux x86-64 and ARM64 paths are implemented but **not runtime-validated** here.
They still require testing on actual Linux KVM hosts. macOS Intel is unsupported.

## Measured execution ablations

Raw data: [benchmark-macos-arm64.json](benchmark-macos-arm64.json).

| vCPUs | RAM MiB | Empty guest ready s | Warm command median ms | Warm command p95 ms | Integer compute median ms | Four parallel crypto commands s |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 512 | 0.380 | 65.242 | 66.391 | 75.497 | 0.647 |
| 2 | 512 | 0.399 | 51.318 | 98.447 | 74.577 | 0.377 |
| 2 | 1024 | 0.374 | 41.509 | 90.852 | 76.629 | 0.352 |

The host integer workload took 64.701 ms median; the guest took 74.577–76.629 ms
across configurations, approximately 15–18% longer. The workload is a Python
integer arithmetic loop, not a matched native binary. This is promising but does
not establish a general near-native performance guarantee.

PBKDF2-HMAC-SHA256 took 52.786 ms on the host and roughly 109–111 ms in the guest.
The host uses Python 3.13.14 / OpenSSL 3.5.7; the guest uses Python 3.13.16 /
OpenSSL 3.0.22. The roughly 2.1x difference cannot be attributed to the VMM alone.
Matching cryptographic implementations and investigating CPU-feature exposure
would be necessary before drawing that conclusion.

A fresh VM for every `true` command took 481.617 ms median including teardown,
versus 41.509 ms for a reused 2-vCPU/1024-MiB session. Reusing the VM removes most
lifecycle overhead for small commands. Moving from one to two vCPUs improved the
four-command batch, while increasing RAM from 512 to 1024 MiB provided no clear
single-workload benefit in this small study. Warm command latency includes
starting the Python privilege-drop worker and process-lifecycle polling; those
are candidates for reducing overhead before changing the VMM.

## Interpretation and remaining comparisons

Guest-internal compute time is measured separately from host-observed command
roundtrip time and readiness. CPU results depend on OS, CPU frequency, interpreter,
and cryptographic library builds; they do not isolate virtualization alone.
Readiness includes disk cloning with cached local image files. Each configuration
has only one readiness measurement, the parameter sweep has fixed order, and the
sample counts are small. Tail latency estimates should be treated as exploratory.

The ablations vary VM reuse, vCPU allocation, RAM, and command concurrency.
Planning, model calls, and memory retrieval are bypassed to replay a fixed
execution workload. This evaluates execution plumbing, not those agent components
or end-to-end task quality.

Comparisons with vfkit/QEMU, virtio-fs, SSH, larger real builds, repeated/randomized
cold-start trials, Linux baselines, and separately ablated planning/retrieval
modules remain future work. No unsupported platform or general security claim is
implied by these results.
