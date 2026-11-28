---
title: Instructors
description: Write the source notebook, build, and set up Gradescope.
---

An assignment is a directory with one source notebook and its data files, for example:

```text
hw1/
├── hw1.py
├── requirements.txt
└── data/
    └── scores.csv
```

`mogs build <directory name>` turns it into a student notebook and an autograder zip.
[`examples/demo`](https://github.com/wiseodd/mogs/tree/main/examples/demo) is a complete example.
See also [marimo's official tutorial](https://docs.marimo.io/getting_started/quickstart/).
To port an Otter Assign notebook, see [Porting from Otter](/porting/).

## Start

```bash
mogs init hw1
mogs edit hw1
```

`init` creates `hw1/hw1.py` with the cells every assignment needs, a guide, and two example questions.
It refuses a directory that already has files in it.
The notebook builds as it is, so you can run `mogs build hw1` before changing anything.
`edit` runs `marimo edit --sandbox` on the notebook with the marimo installed alongside `mogs`.
You don't need to install marimo separately.

Everything below can be written in marimo's editor.

## Settings

The assignment's settings go in a cell with a single `mogs.assignment(...)` call:

```python
@app.cell
def _():
    mogs.assignment(name="HW1", files=["data/"])
    return
```

In the editor, this cell shows the questions, their points and tests, and any problem the build would report.
It reads the saved notebook, so run it again after you save changes.
The build removes the cell from the student notebook.

Available options are:

| Argument       | Default   | Example                   | Description                                                                                                                          |
| -------------- | --------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `name`         | file name | `"HW1"`                   | Names the student zip `<name>.zip` and the autograder zip `<name>-autograder-<timestamp>.zip`. Must be a valid file name on Windows. |
| `files`        | `[]`      | `["data/", "labels.csv"]` | Data files and directories, relative to the notebook. `tests` is reserved for the public tests.                                      |
| `timeout`      | `60`      | `120`                     | Seconds per test.                                                                                                                    |
| `budget`       | `540`     | `900`                     | Seconds for the whole grading run.                                                                                                   |
| `memory`       | `4096`    | `8192`                    | MB per grading process, at least 2048. Linux only.                                                                                   |
| `python`       | `"3.13"`  | `"3.14"`                  | Python version on Gradescope.                                                                                                        |
| `requirements` | `"requirements.txt"` | `"../requirements.txt"` | The requirements file, relative to the notebook. See [Requirements](#requirements). |

Each question ends with a grade cell, `mogs.grade("q1", points=25)`.
The questions appear on Gradescope in the order of these cells.
`mogs.grade` takes:

| Argument    | Default     | Example              | Description                                                                                              |
| ----------- | ----------- | -------------------- | -------------------------------------------------------------------------------------------------------- |
| question id | required    | `"q1"`               | The first argument.                                                                                      |
| `points`    | required    | `25`                 | Points for the question. A written question can be worth 0.                                              |
| `title`     | from the id | `"Gradient descent"` | Name on Gradescope and in the PDF. `q1` and `question1` become `Question 1`; other ids stay as they are. |

Question ids are letters and digits, and start with a letter, e.g., `q1` or `bonus`.
They can't contain `_`: test names use `_` to separate the id from the rest of the name.
Two ids can't differ only in case, like `q1` and `Q1`, and an id can't be a name Windows reserves, like `con` or `nul`.
Each id names a file in the student zip, `tests/<id>.py`.

## Notebook

The source notebook is a regular marimo notebook with a few conventions.

Put all imports in the setup cell (`with app.setup:`).
This includes `import mogs` and any library the tests need.
Tests don't need `import pytest`.
If the notebook imports it, e.g., for `pytest.raises`, list `pytest` in `requirements.txt`.

```python
with app.setup:
    import csv

    import marimo as mo
    import numpy as np

    import mogs
```

Graded answers are functions.
Write each function that the students have to fill in as an `@app.function` cell.
Tests can read these functions, names from the setup cell, and names from `# HIDDEN` cells.
A test that reads a variable or function defined in a regular `@app.cell` fails the build.

The grader imports functions without running the notebook.
A cell variable only exists once the whole notebook has run, so one failing cell anywhere, even one the test doesn't need, would score that question zero.

Write each question and solution as a function instead, and load the data in the setup cell:

```python
with app.setup:
    import mogs
    import pandas as pd

    df = pd.read_csv("prices.csv")


@app.function
def count_missing(df):
    n = df["price"].isna().sum()  # SOLUTION
    return n


@app.cell
def _():
    def test_q1_count():
        assert count_missing(df) == 3

    return
```

A function cell that reads a variable from another cell can't be imported either, so pass data to it as arguments.

### Solutions

Mark a solution with `# BEGIN SOLUTION` and `# END SOLUTION`:

```python
@app.function
def step(x, lr):
    # BEGIN SOLUTION
    # PROMPT: return ...
    grad = 2 * x
    return x - lr * grad
    # END SOLUTION
```

In the student notebook, everything between the markers is replaced by the `# PROMPT:` lines, without the `# PROMPT:` prefix:

```python
@app.function
def step(x, lr):
    return ...
```

If there are no `# PROMPT:` lines, the student gets `...` instead:

- For a single line, put `# SOLUTION` at the end of it. E.g.:
  - `lr = 0.1  # SOLUTION` becomes `lr = ...`.
- Any other line marked `# SOLUTION`, such as `print(x)` or `total += x`, is removed.
- If that leaves a block empty, the build fails and names the line; use `# BEGIN SOLUTION` with a `# PROMPT:` line there instead.
- A misspelled marker (e.g., `# SOULTION`) will fail the build.

### Tests

A test is a function named `test_<qid>_<name>`, e.g., `test_q1_whole_numbers`.
Put the public tests of each question in their own cell, with nothing else in it.
A public test cell can't contain solution markers.
Students get the public tests as written.
The tests are plain `pytest` functions, and you can run them all with `pytest notebook.py`.

Hidden tests go in a separate cell with a `# HIDDEN` line.
The line hides the whole cell wherever it sits in it, as long as it is on a line of its own:

```python
@app.cell
def _():
    # HIDDEN
    def test_q1_negative():
        assert mean([-4, 4, -6]) == -2

    return
```

The build removes this cell from the student notebook, and only Gradescope runs it.

The question ends with a grade cell, which also sets its points:

```python
@app.cell
def _():
    mogs.grade("q1", points=25)
    return
```

The points are split evenly across the question's public and hidden tests.

The build command writes each question's public tests to `tests/<qid>.py` in the same directory as the student notebook.
While the student can modify the tests and get a 100 locally, Gradescope has the ground-truth tests---public and hidden.
So, tampering with the test files is useless for the student.

### Written answers

A question without tests is graded manually on Gradescope.
Write your answer in a Markdown cell between the solution markers, before the grade cell:

```python
@app.cell
def _():
    mo.md(r"""
    <!-- BEGIN SOLUTION -->
    It divides by zero and raises `ZeroDivisionError`.
    <!-- END SOLUTION -->
    """)
    return


@app.cell
def _():
    mogs.grade("q3", points=10)
    return
```

Students get `*Write your answer here.*` in place of your answer, or your `<!-- PROMPT: ... -->` text if you wrote some.
A question can have more than one answer cell, e.g., for parts (a) and (b).

When the student clicks the export button, the answers go into a PDF.
The PDF doesn't carry the student's name, and it goes into the submission zip, together with the notebook.

A written question can be worth 0 points, e.g., `mogs.grade("q5", points=0)`.
Its answer goes into the PDF, but it doesn't count toward the total.
Use this for a written exercise that you want to read but not grade.

## Build

To build the assignment, run:

```bash
mogs build examples/demo
```

It will create a `dist/` directory, e.g.:

```text
examples/demo/
├── hw.py
├── requirements.txt
├── data/
│   └── scores.csv
└── dist/
    ├── student/
    │   └── hw.zip
    └── autograder/
        ├── hw-autograder-2026_09_30T12_30_41_890583.zip
        ├── hw.py
        ├── requirements.txt
        ├── data/
        │   └── scores.csv
        └── tests/
            ├── q1.py
            └── q2.py
```

- `dist/student/<name>.zip` is what you give to your students.
  - It contains one directory with the student notebook, its public tests in `tests/`, and the data files.
- `dist/autograder/` is for Gradescope.
  - Upload `<name>-autograder-<timestamp>.zip` to Gradescope as the autograder.
  - The directory also contains the solved notebook, i.e., the student notebook with the solutions, together with its public tests, the data files and `requirements.txt`.

## Requirements

List the packages in `requirements.txt` next to the notebook, in pip's format.
Pins and `--extra-index-url` work, e.g., `--extra-index-url https://download.pytorch.org/whl/cpu` for the CPU-only build of `torch`.
`mogs edit`, the students' notebooks and Gradescope all install from this file.
mogs writes the notebook's header from it, so don't edit the header.

The data files in `files` are copied next to the notebook on Gradescope, at the same relative paths as on your machine.
So, code that opens `data/scores.csv` relative to the notebook works in both places.

## Gradescope

To set up the assignment on Gradescope:

1. Create a programming assignment.
2. Pick an Ubuntu 22.04 base image and upload the autograder zip.
3. Pick a container with more memory than `memory`, and a timeout longer than `budget`.
4. If the assignment has written answers, add manually graded questions to the same assignment.
   Graders read the answers in the PDF, which is one of the submission's files.

Then, upload `dist/autograder/hw.py` as a test submission to check the autograder.
It should get full marks.

## Security

On Gradescope, student code runs under a separate user account with limited permissions.
The public and hidden tests also run in separate processes.
As a result, the student's code can't read the hidden tests from disk, and it can't write the results file.
If the code crashes or loops forever, only the test where it happens fails.

There are limits, though.
A determined student can still fake a passing test from inside their own code, and the container has internet access.
Otter Grader has the same limits, and it runs student code as root.
