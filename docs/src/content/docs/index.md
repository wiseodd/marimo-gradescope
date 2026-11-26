---
title: Quickstart
description: Build a Gradescope autograder from a single marimo notebook.
---

`marimo-gradescope` turns a single `marimo` notebook into a student notebook and a Gradescope autograder.
The notebook contains the solutions and the tests, and the tests are plain `pytest` functions.

## Install

Install it as a `uv` tool:

```bash
uv tool install marimo-gradescope
```

## Start an assignment

```bash
marimo-gradescope init A1
marimo edit --sandbox A1/A1.py
```

`init` creates `A1/A1.py`, a notebook with a short guide and two example questions.
You write the whole assignment in marimo's editor.

## Example

The assignment's settings go in a cell of their own:

```python
@app.cell
def _():
    mogs.assignment(name="A1")
    return
```

A question consists of a function, its tests, and a grade cell with its points:

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
    mogs.grade("q1", points=4)
    return
```

The build removes the `# HIDDEN` cell and the `mogs.assignment` cell from the student notebook.
Only Gradescope runs the hidden tests.

## Build

Say the notebook is `examples/demo/hw.py`.
Then:

```bash
marimo-gradescope build examples/demo
```

The command prints what to do with the result:

```text
All done!
Upload to Gradescope  : examples/demo/dist/autograder/hw-autograder-2026_09_30T12_30_41_890583.zip
Distribute to students: examples/demo/dist/student/hw.zip
```

Upload the autograder zip to Gradescope.
Give `hw.zip` to your students and point them to the [student guide](/students/).
It contains the student notebook, its public tests and its data files.

`dist/autograder/hw.py` is the student notebook with the full solution.
Submit it on Gradescope to check the autograder.
It should get full marks.

See [`examples/demo`](https://github.com/wiseodd/marimo-gradescope/tree/main/examples/demo) for the complete assignment.
