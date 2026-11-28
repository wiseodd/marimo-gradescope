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
  header as text. `_analysis.py` reads the notebook into plain data for the
  Rust side: the student notebook, the bundle, the requirements, the data files
  and the warnings for `build` and `grade`, and the source notebook for `edit`.
  `assignment.py` uses it to check the data files.
- `crates/mogs-pdf/`: the PDF of written answers, plain Rust with cargo tests.
  `src/mogs/_answers.py` builds the questions from the notebook; this crate
  finds the formulas, runs Typst and falls back to LaTeX source or plain text.
  The Typst packages cmarker and mitex are downloaded on first use into
  Typst's package cache. They need the pinned typst crate version (`=0.15.x`):
  bump the crates and the package versions in `src/preamble.typ` together.
- `crates/mogs/`: the extension module `mogs._native`, in Rust (PyO3, abi3
  for Python 3.11 and newer). The `mogs` command is the console script
  `mogs._native:main`, which parses the command line with clap. maturin builds
  it into one wheel per platform together with `src/mogs/`, and
  `src/mogs/_native.pyi` types it. The version is `[workspace.package] version`
  in `Cargo.toml`.
- `crates/mogs-cli/`: the clap definition of the command line and
  `mogs init`, plain Rust with cargo tests. Python's text, checks and file
  operations come in through its `Interpreter` trait. `diagnostic.rs` draws a
  `UsageError` whose `span` Python filled in (`mogs.config.Span`) on the
  notebook's lines, with `annotate-snippets`.
- `crates/mogs-build/`: `mogs build` and `mogs grade`, plain Rust with cargo
  tests. It writes the zips and grades the solved and blank copies. The
  notebook's analysis and Python's text and exceptions come in through its
  `Interpreter` trait. `templates/` holds `setup.sh` and `run_autograder`,
  which runs the hidden command `mogs autograde`.
- `crates/mogs-grader/`: the grading supervisor, plain Rust with cargo tests.
  It runs the Python grading child (`python -m mogs.grader.child <fd>`),
  enforces its deadlines and builds `results.json`. `crates/mogs/src/grader/`
  only converts values to and from Python. Text with Python's rules (`repr`,
  `:g`, `round`) comes from Python through the `PyText` trait. Its sandbox
  tests run the real child on `.venv/bin/python`, which `.cargo/config.toml`
  names, so a plain `cargo test` works after `uv sync`.
- `crates/mogs-schema/`: the JSON messages between Rust and
  Python (job, events, bundle, `results.json`) as Rust types. Every
  message but `results.json` has `"schema": 1`: bump `SCHEMA` there and in
  `src/mogs/grader/wire.py` together. `schemas/` holds the JSON Schemas
  generated from the types. `MOGS_BLESS=1 cargo test` rewrites them, and
  `tests/test_schemas.py` checks what Python writes against them.
- `tests/`: pytest suite. `cargo test` runs the Rust tests. The ones that need
  network are `#[ignore = "needs network: ..."]`; `make test` runs them with
  `-- --include-ignored` unless `OFFLINE=1`.
  CI runs every test on Linux, and the Rust tests and the `platform` modules
  on macOS and Windows.
- `tests/e2e/`: end-to-end tests. The `docker` ones build the demo autograder
  into Gradescope's base image (`tests/e2e/Dockerfile`) and grade submissions
  through the real `/autograder/run_autograder`. They need docker or podman
  (`MOGS_CONTAINER=docker|podman` picks one) and network, and take several
  minutes. The wheel for the image comes from `maturin build --zig` for
  manylinux_2_17 x86_64, so it builds on any host. The test puts that wheel in
  the unpacked autograder's `requirements.txt`. Without a runtime they skip; `MOGS_REQUIRE_CONTAINER=1` (set in CI) makes that a failure. The base image is amd64 only: on an arm64 host it runs
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
`uv run` rebuild the extension when a file in `cache-keys` of
`pyproject.toml` changes: the Cargo files, the `.rs` and `.typ` files and the
templates.

```bash
make sync        # uv sync
make check       # ruff, cargo fmt + clippy, ty, cargo test, pytest (the CI gate)
make docs        # build the docs site into docs/dist (needs Bun)
make docs-serve  # live preview at localhost:4321
make test TESTS=tests/test_config.py ARGS="-k header"
make test-rust               # only `cargo test`; `make test-python` runs only pytest
make test ARGS="-m slow"      # only the slow tests (marimo via `uv run --with`; included by default)
make test ARGS="-m network"   # only the tests that resolve packages through uv or render a PDF (included by default)
make test OFFLINE=1           # offline: no Rust tests ignored for network, no `slow` or `network` pytest tests
make test ARGS="-m docker"    # only the Docker end-to-end tests: deselected unless `-m` names `docker`
make test ARGS="-m platform"  # the Rust tests and the OS-dependent modules (`PLATFORM` in tests/conftest.py), which CI runs on macOS
make fix         # ruff fixes, then ruff format and cargo fmt
```

## Rules

- **No silent fallbacks.** Invalid input raises an error that names the key or
  thing at fault (`ConfigError` for the header). The `app.run()` loader
  fallback is designed behavior, and its docstring says so.
- **No string dispatch.** Use `match`, `StrEnum` and `isinstance`, never
  `hasattr` or `getattr` on names.
- **Immutable objects.** Frozen `slots=True` dataclasses; build new ones with
  `dataclasses.replace`.
- **Private pytest.** Only `_assertrewrite.py` imports `_pytest`.
- **Cross-platform student code.** The student side (`grade`, export, PDF,
  self-check) uses no `signal.SIGALRM`, `resource` or setuid.
- **Typing.** `ty` must be clean on `src` and `tests`. Justify any
  `# ty: ignore[...]` in one line.
- **Tests first.** Every change comes with tests that fail before it.

### Rust

- No CPython emulation. When Rust needs Python's answer (`str()` of a path,
  `repr`, `round`), it asks Python through the glue's trait.
- Logic lives in plain crates that `cargo test` covers. Only `crates/mogs`
  uses pyo3.
- Long work releases the GIL and polls for signals.
- A panic becomes a Python exception and never aborts. `autograde` turns it
  into failure results instead.
- `unsafe` appears only in `pre_exec`, with a `SAFETY:` comment.

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
