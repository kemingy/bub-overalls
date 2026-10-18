.DEFAULT_GOAL:=install

SOURCE_FILES=src tests scripts

install:
	uv sync --all-groups

test:
	uv run -- pytest tests -vv -rs -m 'not sandbox'

test-sandbox:
	BUB_OVERALLS_TEST=1 uv run -- pytest tests -vv -rs -m sandbox

benchmark:
	uv run -- python scripts/benchmark.py

clean:
	@rm -rf build/ dist/ .pdm-build/

package: clean
	@uv build

format:
	@uv run -- ruff format ${SOURCE_FILES}
	@uv run -- ruff check --fix ${SOURCE_FILES}

lint:
	@uv run -- ruff format --check ${SOURCE_FILES}
	@uv run -- ruff check ${SOURCE_FILES}
	@uv run -- mypy ${SOURCE_FILES}

check: lint
	@uv lock --locked

.PHONY: install test test-sandbox benchmark clean package format lint check
