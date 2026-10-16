.DEFAULT_GOAL := help
KRUN_PREFIX ?= $(CURDIR)/build/runtime
CC ?= cc
UV ?= uv

.PHONY: help install runtime native image format check test test-vm smoke benchmark
help:
	@echo "Targets: install runtime native image format check test test-vm smoke benchmark"
install:
	$(UV) sync
runtime:
	$(UV) run python scripts/build_runtime.py
native:
	mkdir -p build
	$(CC) -std=c11 -Wall -Wextra -Werror -I$(KRUN_PREFIX)/include native/launcher.c -L$(KRUN_PREFIX)/lib -Wl,-rpath,$(KRUN_PREFIX)/lib -lkrun -o build/bub-krun
	@if [ "$$(uname -s)" = Darwin ]; then codesign --force --sign - --entitlements native/entitlements.plist build/bub-krun; fi
image:
	$(UV) run python scripts/build_image.py
format:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .
check:
	$(UV) lock --locked
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy src
test:
	$(UV) run pytest -q -m 'not vm'
test-vm:
	BUB_SANDBOX_VM_TEST=1 $(UV) run pytest -q -m vm
smoke:
	$(UV) run bub-sandbox smoke
benchmark:
	$(UV) run python scripts/benchmark.py
