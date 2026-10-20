# Repository instructions

- Use macOS `/usr/bin/sandbox-exec`, or experimental Linux Landlock plus seccomp
  selected automatically by platform. No Homebrew or native build. Use the existing OrbStack
  Ubuntu guest for Linux validation in a separate native-filesystem test copy.
- Linux requires Landlock ABI 6+, native x86_64/aarch64, seccomp, unprivileged
  user/mount/IPC namespaces, a network namespace when offline, and the modern
  mount API. Initialization
  failures must never select host execution. Document gaps in docs/landlock.md.
- Every Environment command and file operation must pass through its kernel sandbox.
- Keep networking disabled by default. Explicit opt-in permits outbound IP and
  the DNS resolver socket; verify actual allowed and denied operations. Do not
  imply domain filtering or approval/escalation support.
- Follow Bub's package layout: code in `src/bub_overalls/`, tests in `tests/`.
- Use Overalls names for project types and helpers: `OverallsEnvironment`,
  `OverallsConfig`, and `build_overalls_profile`. Keep Bub hook/API method names.
- Follow `../spectree` for 88 columns, absolute imports, and focused functions.
  Keep runtime dependencies limited to Bub; use the standard library for small
  utilities and the CLI.
- Keep configuration in standard-library dataclasses and environment variables.
  Do not add YAML or Pydantic settings adapters; prefer msgspec if structured
  validation becomes necessary.
- Use verbs for action helpers, `config` for configuration, `returncode`/`stdout`/
  `stderr` for process results. Mark implementation helpers private and use `Self`
  for subclass-preserving factories.
- Keep strict mypy and Ruff naming/modernization checks enabled. Use typed
  attributes directly instead of dynamic attribute lookup in runtime code.
- Python's minimum version must match the required Bub API, currently 3.12.
- Follow `../spectree` Makefile style: install by default, direct `uv` commands,
  quiet format/lint recipes, and clean before packaging. Use Makefile targets
  for format, lint/check, tests, and packaging.
- Update docs and tests when behavior changes. Keep unit and real sandbox
  test evidence distinct.
- Do not claim VM isolation, cgroup limits, or reliable detached-process cleanup.
- Use Conventional Commits and explain non-obvious isolation/lifecycle choices.
