# Gradescope-Compatible Marimo Autograder

<p align="center">
  <a href="https://marimo-gradescope.kristia.de"><img alt="Docs" src="https://img.shields.io/badge/docs-marimo--gradescope.kristia.de-8f55e0?style=for-the-badge"></a>
  <a href="https://marimo-gradescope.kristia.de/instructors/"><img alt="Instructors" src="https://img.shields.io/badge/instructors-8f55e0?style=for-the-badge"></a>
  <a href="https://marimo-gradescope.kristia.de/students/"><img alt="Students" src="https://img.shields.io/badge/students-8f55e0?style=for-the-badge"></a>
  <br>
  <img alt="Python 3.13+" src="https://img.shields.io/badge/python-3.13%2B-3776ab?style=for-the-badge&logo=python&logoColor=white">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-555555?style=for-the-badge"></a>
</p>

Like [`Otter-Grader`](https://otter-grader.readthedocs.io/en/latest/) but for [marimo](https://marimo.io/).
Compatible with Gradescope.

## Installation

```bash
uv tool install marimo-gradescope
```

## Usage

Prepare a marimo notebook (see docs; full example [here](https://github.com/wiseodd/marimo-gradescope/tree/main/examples/demo)).
Say, it's in `examples/demo` directory (relative to the current working directory) and named `hw.py`.
Then:

```bash
marimo-gradescope build examples/demo
```

In `examples/demo/dist/student`, it will output a student notebook (`hw.py`) and any data attached to the notebook (if any).
Distribute these to your students, e.g., by zipping them.
Refer your students to <https://marimo-gradescope.kristia.de/students/>.

In `examples/demo/dist/autograder`, it will output `hw-autograder-*.zip` and `hw.py`.
Upload the former to Gradescope.
The file `hw.py` here is like the one in the `student` dir, but with full solution.
It's useful to check the autograder on Gradescope.

More info: <https://marimo-gradescope.kristia.de>

## Development

```bash
make check   # ruff, ty, pytest
make docs    # build the docs site into docs/dist
```
