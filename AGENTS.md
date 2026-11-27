# AGENTS.md

**mogs** turns one marimo source notebook into a student notebook
and a Gradescope autograder zip. Alpha: the API changes without deprecation.

## Where things are

- Design, plans and progress notes live outside this repo, in the author's
  private planning directory. Never put design notes, plans or local paths in
  the repo.
- `src/mogs/`: the Python package, imported and published on PyPI.
  `config.py` reads the config from the notebook's `mogs.assignment(...)` and
  `mogs.grade(...)` calls (statically, from the AST) and edits the PEP 723
  header as text. `scaffold.py` is `mogs init`.
- `src/mogs/_typst/`: the Typst packages cmarker and mitex,
  vendored for the PDF of written answers (`pdf.py`). They need the Typst
  version the `pdf` extra pins (`typst>=0.15,<0.16`): bump the pin and the
  vendored versions together.
- `crates/mogs/`: the `mogs` executable, in Rust. It runs
  `python -P -m mogs.cli` with the interpreter in its own folder, found after
  resolving symlinks. On Windows it then tries the folder above `Scripts`,
  where a global `pip install` puts `python.exe`. Last comes
  `$(uv tool dir)/mogs`, because uv copies tool executables on Windows.
  maturin builds it into platform wheels together with
  `src/mogs/`. The version is `[workspace.package] version` in `Cargo.toml`.
  `src/mogs/_binary.py` finds and runs the installed executable.
- `crates/mogs-schema/`: the JSON messages between the `mogs` executable and
  Python (job, events, bundle, inspect, `results.json`) as Rust types. Every
  message but `results.json` has `"schema": 1`: bump `SCHEMA` there and in
  `src/mogs/grader/wire.py` together. `schemas/` holds the JSON Schemas
  generated from the types. `MOGS_BLESS=1 cargo test` rewrites them, and
  `tests/test_schemas.py` checks what Python writes against them.
- `tests/`: pytest suite. `cargo test` runs the Rust tests.
- `tests/golden/`: calls to the Python functions the Rust port replaces, recorded from
  the test suite with their results, which the port must reproduce. Its README gives the
  JSON shapes; `tests/test_golden.py` replays them.
- `tests/e2e/`: end-to-end tests. The `docker` ones build the demo autograder
  into Gradescope's base image (`tests/e2e/Dockerfile`) and grade submissions
  through the real `/autograder/run_autograder`. They need docker or podman
  (`MOGS_CONTAINER=docker|podman` picks one) and network, and take several
  minutes. The wheel for the image comes from `maturin build --zig` for
  manylinux_2_17 x86_64, so it builds on any host. Without a runtime they skip; `MOGS_REQUIRE_CONTAINER=1` (set in CI)
  makes that a failure. The base image is amd64 only: on an arm64 host it runs
  under emulation, which is slow enough that grading timeouts may trip.
- `.github/workflows/release.yml`: on a published GitHub release (tags like
  `0.7`, no `v`), checks the tag against the `Cargo.toml` version, builds the
  wheels and the sdist, smoke-tests each wheel on a runner that can run it,
  then publishes them all to PyPI through
  trusted publishing.
- `docs/`: the docs site (Astro Starlight, built with Bun), deployed to
  https://mogs.kristia.de by `.github/workflows/docs.yml`. Pages
  are `docs/src/content/docs/` (`index.md`, `instructors.md`,
  `students.md`). The README stays short and links there.

## Commands

Always go through `uv` (never global Python). The Rust toolchain is
`rust-toolchain.toml` (stable, with clippy and rustfmt). `uv sync` and
`uv run` rebuild the executable when `Cargo.toml`, `Cargo.lock` or a `.rs`
file changes.

```bash
make sync        # uv sync
make check       # ruff, cargo fmt + clippy, ty, cargo test, pytest (the CI gate)
make docs        # build the docs site into docs/dist (needs Bun)
make docs-serve  # live preview at localhost:4321
make test TESTS=tests/test_config.py ARGS="-k header"
make test ARGS="-m slow"      # only the slow tests (marimo via `uv run --with`; included by default)
make test ARGS="-m network"   # only the tests that resolve packages through uv (included by default)
make test ARGS="-m 'not slow and not network'"   # offline
make test ARGS="-m docker"    # only the Docker end-to-end tests: deselected unless `-m` names `docker`
make fix         # ruff fixes, then ruff format and cargo fmt
make golden      # record tests/golden again from the suite (slow)
```

## Rules

- **No silent fallbacks.** Invalid input raises an error that names the key or
  thing at fault (`ConfigError` for the header). The `app.run()` loader
  fallback is designed behavior, and its docstring says so.
- **No string dispatch.** Use `match`, `StrEnum` and `isinstance`, never
  `hasattr` or `getattr` on names.
- **Immutable objects.** Frozen `slots=True` dataclasses; build new ones with
  `copy.replace`.
- **Private pytest.** Only `_assertrewrite.py` imports `_pytest`.
- **Cross-platform student code.** The student side (`grade`, export, PDF,
  self-check) uses no `signal.SIGALRM`, `resource` or setuid.
- **Typing.** `ty` must be clean on `src` and `tests`. Justify any
  `# ty: ignore[...]` in one line.
- **Tests first.** Every change comes with tests that fail before it.

## Style

- Code: generous blank lines between logical blocks inside functions (after
  the docstring, between setup, computation and return, around loops and
  if-blocks).
- Docstrings and comments: short, plain and impersonal. Google style. No
  marketing words.

## Git

- Never commit to `main`. Work on a branch off `main` and open one plain PR
  into `main`, one commit per logical change. Don't stack PRs. Use plain
  conventional commit messages.
- No design notes or plans in the repo.
