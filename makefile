# Everything runs through uv, so the tools come from the project's venv. Cargo too: PyO3
# builds against the venv's Python, as `abi3-py311` needs 3.11 or newer.
UV ?= uv run

# The word after `make version` is the new version, not a target.
ifeq (version,$(firstword $(MAKECMDGOALS)))
NEW_VERSION := $(word 2,$(MAKECMDGOALS))
$(eval $(NEW_VERSION):;@:)
endif

TESTS ?= tests
ARGS ?=
# `OFFLINE=1` leaves out every test that needs network: the Rust tests ignored for it, and
# the pytest tests marked `slow` or `network`.
OFFLINE ?=
CARGO_TEST_ARGS = $(if $(OFFLINE),,-- --include-ignored)
PYTEST_MARKS = $(if $(OFFLINE),-m 'not slow and not network',)

.PHONY: sync version test test-rust test-python lint fix typecheck check docs docs-serve

sync:
	uv sync

# `make version` prints the version; `make version 1.0.0` sets it in Cargo.toml and
# Cargo.lock, as `uv version` does.
version:
ifeq ($(NEW_VERSION),)
	@sed -n 's/^version = "\(.*\)"$$/\1/p' Cargo.toml
else
	@echo '$(NEW_VERSION)' | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$' || { echo 'usage: make version [x.y.z]'; exit 2; }
	sed -i.bak 's/^version = ".*"$$/version = "$(NEW_VERSION)"/' Cargo.toml && rm Cargo.toml.bak
	cargo update --workspace --quiet
endif

# The docker tests run only when `-m` names them (tests/conftest.py).
# Examples:
#   make test
#   make test TESTS=tests/test_config.py ARGS='-k header'
#   make test ARGS='-m docker'
#   make test OFFLINE=1
# Runs the Rust tests, then pytest. Never both at once: the sandbox tests have time limits.
# CI runs each half in its own job.
test:
	@$(MAKE) --no-print-directory test-rust
	@$(MAKE) --no-print-directory test-python

test-rust:
	$(UV) cargo test --workspace $(CARGO_TEST_ARGS)

test-python:
	$(UV) pytest $(TESTS) -q $(PYTEST_MARKS) $(ARGS)

lint:
	$(UV) ruff check
	$(UV) ruff format --check
	cargo fmt --check
	$(UV) cargo clippy --workspace --all-targets -- -D warnings

# Fix lint warnings, then format (order matters).
fix:
	-$(UV) ruff check --fix
	$(UV) ruff format
	cargo fmt

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
