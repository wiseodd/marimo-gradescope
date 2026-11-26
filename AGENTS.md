# AGENTS.md

**mogs** turns one marimo source notebook into a student notebook
and a Gradescope autograder zip. Alpha: the API changes without deprecation.

## Where things are

- Design, plans and progress notes live outside this repo, in the author's
  private planning directory. Never put design notes, plans or local paths in
  the repo.
- `src/mogs/`: the package, imported as `mogs`, published as
  `mogs`, and run as the `mogs` command. `config.py` reads
  the config from the notebook's `mogs.assignment(...)` and `mogs.grade(...)`
  calls (statically, from the AST) and edits the PEP 723 header as text.
  `scaffold.py` is `mogs init`.
- `src/mogs/_typst/`: the Typst packages cmarker and mitex,
  vendored for the PDF of written answers (`pdf.py`). They need the Typst
  version the `pdf` extra pins (`typst>=0.15,<0.16`): bump the pin and the
  vendored versions together.
- `tests/`: pytest suite.
- `tests/e2e/`: end-to-end tests. The `docker` ones build the demo autograder
  into Gradescope's base image (`tests/e2e/Dockerfile`) and grade submissions
  through the real `/autograder/run_autograder`. They need docker or podman
  (`MGS_CONTAINER=docker|podman` picks one) and network, and take several
  minutes. Without a runtime they skip; `MGS_REQUIRE_CONTAINER=1` (set in CI)
  makes that a failure. The base image is amd64 only: on an arm64 host it runs
  under emulation, which is slow enough that grading timeouts may trip.
- `docs/`: the docs site (Astro Starlight, built with Bun), deployed to
  https://mogs.kristia.de by `.github/workflows/docs.yml`. Pages
  are `docs/src/content/docs/` (`index.md`, `instructors.md`,
  `students.md`). The README stays short and links there.

## Commands

Always go through `uv` (never global Python).

```bash
make sync        # uv sync
make check       # ruff check + format check, ty, pytest (the CI gate)
make docs        # build the docs site into docs/dist (needs Bun)
make docs-serve  # live preview at localhost:4321
make test TESTS=tests/test_config.py ARGS="-k header"
make test ARGS="-m slow"      # only the slow tests (marimo via `uv run --with`; included by default)
make test ARGS="-m network"   # only the tests that resolve packages through uv (included by default)
make test ARGS="-m 'not slow and not network'"   # offline
make test ARGS="-m docker"    # only the Docker end-to-end tests: deselected unless `-m` names `docker`
make fix         # ruff fixes, then format
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
