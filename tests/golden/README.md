# Golden corpus

Calls to the functions in `record.py`'s `FUNCTIONS`, recorded from the v0.7 Python code,
with their results or errors. `tests/test_golden.py` replays them in Python, and the Rust
port must give the same output.

To record it again, run `make golden`. It runs the test suite with `record.py` as a plugin
(Python 3.13, docker tests left out) and rewrites `cases/` and `blobs/`. The plugin fails if
a test fails, a function gets no case, or a line holds an absolute path.

## Shapes

Each line of `cases/<module>.jsonl` is
`{"function": ..., "args": {...}, "ok": <value>}`, or the same with
`"error": {"type": <exception class name>, "message": <str(exc)>}` in place of `ok`.
`args` holds every parameter by name, defaults included. Lines are sorted, and each
(function, args) pair appears once.

- Strings, ints, bools, `None`, lists and string-keyed dicts are themselves. Tuples are
  lists, sets are sorted lists, enums are their value, and paths are POSIX strings.
- A string over 1 KiB of UTF-8 is `{"blob": "<name>"}`, the text of
  `blobs/<name>.txt`, where the name is the first 16 hex digits of its SHA-256.
- A string with a lone surrogate is `{"str_utf16": [<UTF-16 code units>]}`.
- A float that JSON lacks is `{"float": "inf" | "-inf" | "nan" | "-0.0"}`.
- Absolute paths are rewritten in every string: a path in the repo becomes relative to
  it, the repo itself `<repo>`, and a pytest temporary folder `<tmp>`.
  `_mogs_submission_<digest>` stands for a loaded submission's module name, which hashes
  its path.
- `Notebook` is `{"cells": [<Cell>, ...], "src": ...}`, and `Cell` has every field:
  `kind`, `name`, `start`, `end`, `tests`, `hidden`, `grade_qid`, `assignment`,
  `late_hidden`.
- `AssignmentConfig` is the `config` of a bundle (`schemas/bundle.schema.json`) plus
  `requirements`, a path or `null`.
- `GradingBundle` and `Outcome` are their `to_dict()` JSON, and results are the
  `results.json` content. An outcome's `seconds` is always 0.0, since wall time differs
  between runs.
- An exception argument (`failure_results`) is `{"type": ..., "message": ...}`.
- Other dataclasses (`pdf.Question`, `pdf.Part`, `pdf.Formula`) have every field.

`pyfmt.jsonl` holds `repr` and `format_g` (`format(value, "g")`, numbers only) on a fixed
sample of values, which messages format with `!r` and `:g`. In its `args`, a tuple is
`{"tuple": [...]}`, so it reads apart from a list.
