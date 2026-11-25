---
title: Quickstart
description: Build a Gradescope autograder from a single marimo notebook.
---

`marimo-gradescope` turns one `marimo` notebook into a student notebook and a Gradescope autograder.
Solutions, public tests and hidden tests live in the same notebook.
Tests are plain `pytest` functions.

## Install

```bash
uv tool install marimo-gradescope
```

## Example

The config lives in the notebook's header:

```python
# [tool.marimo-gradescope.questions]
# q1 = { points = 4, title = "Average" }
# q2 = { points = 5, title = "Empty lists", manual = true }
#
# [tool.marimo.runtime]
# reactive_tests = false
```

A question is a function, its tests, and a check cell:

```python
@app.function
def mean(xs):
    # BEGIN SOLUTION
    return sum(xs) / len(xs)
    # END SOLUTION


@app.cell
def _():
    def test_q1_whole_numbers():
        assert mean([1, 2, 3]) == 2

    return


@app.cell
def _():
    # HIDDEN
    def test_q1_negative():
        assert mean([-4, 4, -6]) == -2

    return


@app.cell
def _():
    mgs.check("q1")
    return
```

## Build

Say the notebook is `examples/demo/hw.py`:

```console
$ marimo-gradescope build examples/demo
Student notebook:  examples/demo/dist/student/hw.py
Autograder zip:    examples/demo/dist/autograder/hw-autograder-2026_09_30T12_30_41_890583.zip
Solved notebook:   examples/demo/dist/autograder/hw.py
```

`dist/student/` holds the student notebook and its data files.
Give it to your students, e.g. as a zip, and point them to the [student guide](/students/).

Upload the zip in `dist/autograder/` to Gradescope.
`dist/autograder/hw.py` is the student notebook with the full solution.
Submit it on Gradescope to check the autograder.
The build fails unless it gets full marks.

[`examples/demo`](https://github.com/wiseodd/marimo-gradescope/tree/main/examples/demo) is the complete assignment.
