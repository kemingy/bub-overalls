# Repository instructions

- Do not use Homebrew. Build a private runtime with `make runtime`; keep downloads and binaries under ignored `build/`.
- Use the Makefile for formatting, lint, typing, tests, and VM checks.
- Keep runtime behavior, documentation, and tests aligned.
- Missing/failed sandbox initialization must raise; never fall back to host execution.
- Avoid host filesystem exports and implicit libkrun TSI networking.
- Distinguish unit tests, actual VM tests, and performance measurements. Do not claim Linux validation from macOS results.
- Keep commits focused and use Conventional Commit messages; explain non-obvious isolation and lifecycle choices.
