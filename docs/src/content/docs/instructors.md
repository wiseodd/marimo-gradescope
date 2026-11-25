---
title: Instructors
description: Write the source notebook, build, and set up Gradescope.
---

An assignment is a folder with one source notebook and its data files.
`marimo-gradescope build examples/demo` turns it into a student notebook and an autograder zip.
[`examples/demo`](https://github.com/wiseodd/marimo-gradescope/tree/main/examples/demo) is a complete one.

## Header

```python
# /// script
# requires-python = ">=3.13"
# dependencies = ["marimo", "marimo-gradescope", "numpy"]
#
# [tool.marimo-gradescope]
# name = "HW1"
# files = ["data/"]
#
# [tool.marimo-gradescope.questions]
# q1 = 25
# q2 = { points = 25, title = "Gradient descent" }
# q3 = { points = 10, title = "Discussion", manual = true }
#
# [tool.marimo.runtime]
# reactive_tests = false
# ///
```

| Key | Default | |
| --- | --- | --- |
| `questions` | required | Points per question, or a table with `points`, `title`, `manual`. |
| `name` | file name | The zip is `<name>-autograder-<timestamp>.zip`. |
| `files` | `[]` | Data files and folders, relative to the notebook. |
| `timeout` | `60` | Seconds per test. |
| `budget` | `540` | Seconds for the whole grading run. |
| `memory` | `4096` | MB per grading process, at least 2048. Linux only. |
| `python` | `"3.13"` | Python on Gradescope. |
| `requirements` | none | A requirements file to use on Gradescope instead of `dependencies`. |

Question ids are letters and digits, like `q1` or `bonus`.
`reactive_tests = false` is required: `marimo` 0.25 crashes when it runs tests in the notebook by itself.

## Notebook

Imports go in the setup cell, including `import marimo_gradescope as mgs` and whatever the tests need.

Functions students write are `@app.function` cells.
The grader imports them without running the rest of the notebook.
A function that reads another cell's variable can only be graded by running the whole notebook, so avoid it.

### Solutions

```python
@app.function
def step(x, lr):
    # BEGIN SOLUTION
    # PROMPT: return ...
    return x - lr * 2 * x
    # END SOLUTION
```

Students get the `# PROMPT:` lines, or `...` if there are none.
A single line works too: `lr = 0.1  # SOLUTION` becomes `lr = ...`.
A misspelled marker is a build error, so no solution leaks by accident.

### Tests

Tests are functions named `test_<qid>_<name>`, one cell per question, and nothing else in that cell.
`pytest notebook.py` runs them.

A cell with a `# HIDDEN` line is removed from the student copy.
Hidden cells hold hidden tests, and may hold data or helpers for them.

Each question with public tests gets a check cell:

```python
@app.cell
def _():
    mgs.check("q1")
    return
```

Students see the question's points and a pass or fail per public test, updated as they edit.

### Written answers

A question with `manual = true` has no tests.
It needs a Markdown cell named `answer_<qid>`, and optionally `prompt_<qid>` with the question.
The student's export button puts these cells into a PDF, with LaTeX math rendered.
LaTeX the PDF cannot render, such as TikZ, shows as source.
Images show as their alt text, and a prompt cell that isn't plain `mo.md` shows as source.

## Build

```console
$ marimo-gradescope build examples/demo
```

This writes two folders, as Otter does:

- `dist/student/`: the student notebook and its data files, for students.
- `dist/autograder/`: `<name>-autograder-<timestamp>.zip`, for Gradescope, and the solved notebook: the student notebook with the solutions, next to its data files and `requirements.txt`.

Each build replaces both folders.
Before that, the build grades the solved and the blank student notebook with the real grader.
The solved notebook must get full marks.
A test that passes on the blank copy is reported, since it gives free points.

`build` runs on Linux and macOS.

## Requirements

Gradescope installs the header's `dependencies`, pinned at build time.
`[[tool.uv.index]]` entries carry over, so CPU-only `torch` works:

```python
# dependencies = ["marimo", "marimo-gradescope", "torch"]
#
# [tool.uv.sources]
# torch = [{ index = "pytorch-cpu", marker = "sys_platform != 'darwin'" }]
#
# [[tool.uv.index]]
# name = "pytorch-cpu"
# url = "https://download.pytorch.org/whl/cpu"
# explicit = true
```

Data files in `files` sit next to the notebook on Gradescope too, at the same relative paths.

## Gradescope

1. Create a programming assignment.
2. Pick an Ubuntu 22.04 base image and upload the zip.
3. Pick a container with more memory than `memory`, and a timeout longer than `budget`.
4. For written answers, add manual-graded questions to the same assignment.

Then upload `dist/autograder/hw.py` as a test submission; it should get full marks.

Each question gets one row.
It shows the public tests with their messages and only the count of hidden tests that passed.
Points are split evenly over a question's tests.

## Security

Student code runs as an unprivileged user, with public and hidden tests in separate processes.
It can't read the hidden tests from disk or write the results file.
A crash or an endless loop fails only the test it happens in.

A determined student can still fake a pass from inside their own code, and the container has internet access.
Otter Grader has the same limits, and runs student code as root.
