# Everything runs through uv, so the tools come from the project's venv.
UV ?= uv run

TESTS ?= tests
ARGS ?=

.PHONY: sync test lint fix typecheck check docs docs-serve

sync:
	uv sync

# The docker tests run only when `-m` names them (tests/conftest.py).
# Examples:
#   make test
#   make test TESTS=tests/test_config.py ARGS='-k header'
#   make test ARGS='-m docker'
test:
	$(UV) pytest $(TESTS) -q $(ARGS)

lint:
	$(UV) ruff check
	$(UV) ruff format --check

# Fix lint warnings, then format (order matters).
fix:
	-$(UV) ruff check --fix
	$(UV) ruff format

typecheck:
	$(UV) ty check

# The same gate CI runs.
check: lint typecheck test

# The docs site (Astro Starlight) lives in docs/ and builds with Bun.
docs:
	cd docs && bun install --frozen-lockfile && bun run build

# Live preview at http://localhost:4321.
docs-serve:
	cd docs && bun install --frozen-lockfile && bun run dev
