<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://marimo-gradescope.kristia.de/mogs-dark.svg">
    <img alt="mogs" src="https://marimo-gradescope.kristia.de/mogs-light.svg" height="64">
  </picture>
</p>

<p align="center"><strong>Marimo-Gradescope</strong></p>

<p align="center">Gradescope-compatible Marimo autograder. Alternative to Otter-Grader</p>

<p align="center">
  <a href="https://marimo-gradescope.kristia.de"><img alt="Docs: Quickstart" src="https://img.shields.io/badge/docs-quickstart-8f55e0?style=for-the-badge"></a>
  <a href="https://marimo-gradescope.kristia.de/instructors/"><img alt="Instructors" src="https://img.shields.io/badge/instructors-8f55e0?style=for-the-badge"></a>
  <a href="https://marimo-gradescope.kristia.de/students/"><img alt="Students" src="https://img.shields.io/badge/students-8f55e0?style=for-the-badge"></a>
  <br>
  <img alt="Python 3.13+" src="https://img.shields.io/badge/python-3.13%2B-3776ab?style=for-the-badge&logo=python&logoColor=white">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-555555?style=for-the-badge"></a>
</p>

## Installation

Install [`uv`](https://docs.astral.sh/uv/#installation). Then:

```bash
uv tool install marimo-gradescope
```

The command is `marimogs`.

## Usage

Create an assignment (replace `<name`> with your assignment name) and edit it in marimo's editor:

```bash
marimogs init <name>
marimogs edit <name>
```

This will create a directory called `<name>` with `<name>.py` inside.
You can change `<name>.py` if you want.
This Python file is a marimo notebook and it has a short guide and two example questions (see docs; full example [here](https://github.com/wiseodd/marimo-gradescope/tree/main/examples/demo)).

Say, the assignment name is `demo` directory (relative to the current working directory) and named `hw.py`.
Then:

```bash
marimogs build demo
```

In `emo/dist/student`, it will output `hw.zip`.
It contains the student notebook (`hw.py`), its public tests (`tests/`), and any data attached to the notebook (if any).
Distribute it to your students.
Refer your students to <https://marimo-gradescope.kristia.de/students/>.

In `demo/dist/autograder`, it will output `hw-autograder-*.zip` and `hw.py`.
Upload the former to Gradescope.
The file `hw.py` here is like the one in the student zip, but with full solution.
It's useful to check the autograder on Gradescope.

More info: <https://marimo-gradescope.kristia.de>

## Development

```bash
make check   # ruff, ty, pytest
make docs    # build the docs site into docs/dist
```
