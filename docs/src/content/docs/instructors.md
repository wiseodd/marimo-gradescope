---
title: Instructors
description: Write the source notebook, build, and set up Gradescope.
---

An assignment is a directory with one source notebook and its data files, for example:

```text
hw1/
├── hw1.py
└── data/
    └── scores.csv
```

`mogs build <directory name>` turns it into a student notebook and an autograder zip.
[`examples/demo`](https://github.com/wiseodd/mogs/tree/main/examples/demo) is a complete example.
See also [marimo's official tutorial](https://docs.marimo.io/getting_started/quickstart/).

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

| Argument       | Default   | Example                            | Description                                                                                                                          |
| -------------- | --------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `name`         | file name | `name="HW1"`                       | Names the student zip `<name>.zip` and the autograder zip `<name>-autograder-<timestamp>.zip`. Must be a valid file name on Windows. |
| `files`        | `[]`      | `files=["data/"]`                  | Data files and directories, relative to the notebook. `tests` is reserved for the public tests.                                      |
| `timeout`      | `60`      | `timeout=120`                      | Seconds per test.                                                                                                                    |
| `budget`       | `540`     | `budget=900`                       | Seconds for the whole grading run.                                                                                                   |
| `memory`       | `4096`    | `memory=8192`                      | MB per grading process, at least 2048. Linux only.                                                                                   |
| `python`       | `"3.13"`  | `python="3.14"`                    | Python version on Gradescope.                                                                                                        |
| `requirements` | none      | `requirements="requirements.txt"`  | A requirements file for Gradescope, relative to the notebook. It's used instead of `dependencies`.                                   |

The build reads the arguments without running the notebook.
So they must be written out as literals: `timeout=120` works, `timeout=2 * LIMIT` doesn't.
The same goes for the arguments of `mogs.grade` below.

Each question ends with a grade cell, `mogs.grade("q1", points=25)`.
The questions appear on Gradescope in the order of these cells.
`mogs.grade` takes:

| Argument    | Default     | Example                    | Description                                                                               |
| ----------- | ----------- | -------------------------- | ----------------------------------------------------------------------------------------- |
| question id | required    | `"q1"`                     | The first argument.                                                                       |
| `points`    | required    | `points=25`                | Points for the question. A written question can be worth 0.                               |
| `title`     | from the id | `title="Gradient descent"` | Name on Gradescope and in the PDF. `q1` and `question1` become `Question 1`; other ids stay as they are. |

Question ids are letters and digits, and start with a letter, e.g., `q1` or `bonus`.
They can't contain `_`: test names use `_` to separate the id from the rest of the name.
Two ids can't differ only in case, like `q1` and `Q1`, and an id can't be a name Windows reserves, like `con` or `nul`.
Each id names a file in the student zip, `tests/<id>.py`.

The notebook's header lists the packages it needs:

```python
# /// script
# requires-python = ">=3.13"
# dependencies = ["marimo", "mogs", "numpy"]
# ///
```

## Notebook

The source notebook is a regular marimo notebook with a few conventions.

Put all imports in the setup cell (`with app.setup:`).
This includes `import mogs` and any library the tests need.

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

This is wrong:

```python
@app.cell
def _():
    n_missing = df["price"].isna().sum()  # SOLUTION
    return (n_missing,)


@app.cell
def _(n_missing):
    def test_q1_count():
        assert n_missing == 3

    return
```

The build stops with:

```
Build failed.
hw/hw.py has 1 problem(s):
- Question `q1`: `test_q1_count` (line 31) reads `n_missing`, which a plain `@app.cell` defines at line 25. The grader imports the functions the tests use without running the notebook, so it can't get that name. Write the answer as an `@app.function` that the test calls, and put data the tests need in the setup cell (`with app.setup:`) or load it in the test.
```

Write the answer as a function instead, and load the data in the setup cell:

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

If there are no `# PROMPT:` lines, the student gets `...` instead.

For a single line, put `# SOLUTION` at the end of it.
For example, `lr = 0.1  # SOLUTION` becomes `lr = ...` in the student notebook.
The same works for `lr: float = 0.1`, `return 2 * x`, `yield x` and `yield from xs`, which become `lr: float = ...`, `return ...`, `yield ...` and `yield from ...`.
Any other line marked `# SOLUTION`, such as `print(x)` or `total += x`, is removed.
If that leaves a block empty, the build fails and names the line; use `# BEGIN SOLUTION` with a `# PROMPT:` line there instead.
A misspelled marker (e.g., `# SOULTION`) will fail the build.

The markers work in any cell.
In a Markdown cell, where a `#` line is a heading, write them as HTML comments: `<!-- BEGIN SOLUTION -->`, `<!-- END SOLUTION -->` and `<!-- PROMPT: ... -->`.
A `#` marker in a Markdown cell fails the build.
There, the student gets `*Write your answer here.*` instead of `...`.

This is useful for ungraded exercises: write one in a normal cell and put its solution between the markers.
Its answer stays in the notebook and doesn't go into the PDF.
In a question without tests, a Markdown cell with markers is the answer instead; see [Written answers](#written-answers).

### Tests

A test is a function named `test_<qid>_<name>`, e.g., `test_q1_whole_numbers`.
Put the public tests of each question in their own cell, with nothing else in it.
A public test cell can't contain solution markers.
Students get the public tests as written.
The tests are plain `pytest` functions, and you can run them all with `pytest notebook.py`.

Hidden tests go in a separate cell with a `# HIDDEN` line:

```python
@app.cell
def _():
    # HIDDEN
    def test_q1_negative():
        assert mean([-4, 4, -6]) == -2

    return
```

The build removes this cell from the student notebook, and only Gradescope runs it.
Data or helpers that the hidden tests need go in a hidden cell of their own: `pytest notebook.py` skips the tests in a cell that holds anything else.

The question ends with a grade cell, which also sets its points:

```python
@app.cell
def _():
    mogs.grade("q1", points=25)
    return
```

In the student notebook, the grade cell shows the question's points and whether each public test passes.
It updates whenever the student edits their code.
In your source notebook it also runs the hidden tests, but it doesn't rerun when you edit a solution: run it again by hand.

The build command writes each question's public tests to `tests/<qid>.py` in the same directory as the student notebook.
While the student can modify the tests and get a 100 locally, Gradescope has the ground-truth tests---public and hidden.
So, tampering with the test files is useless for the student.

### Written answers

A question without tests is graded by hand on Gradescope.
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
The question's text is the run of Markdown cells just before the answer cell.
A code cell ends that run, so the Markdown above a demo between two questions isn't part of the next question.
Hidden, test and instructor cells don't end it, since students don't see them.

A question can have more than one answer cell, e.g., for parts (a) and (b).
The Markdown between two answer cells is the text of the second one.

To check what each question's text is, read the output of `mogs build` or the `mogs.assignment(...)` summary.
Both list the first line of each text cell of a written question, or "no question text".

When the student clicks the export button, the answers go into a PDF, each after its text, with the LaTeX math rendered.
The PDF goes into the submission zip, together with the notebook.
An answer the student left blank or unchanged shows as *No answer.* in the PDF, and the export warns about it.

A written question can be worth 0 points, e.g., `mogs.grade("q5", points=0)`.
Its answer goes into the PDF, but it doesn't count toward the total.
Use this for a written exercise that you want to read but not grade.

The PDF renders inline math in `$...$` and display math in `$$...$$`, including environments such as `aligned`, `cases` and `pmatrix`.
A paragraph that is only an `align`, `equation` or `gather` environment (starred or not) is display math too, without the `$$`, as in marimo.
The environment has to start the paragraph and end it, with no blank line inside.
Other environments without `$$` show as source.

Some things can't be rendered.
LaTeX that the PDF doesn't support (e.g., TikZ) shows as source, and so does question text that isn't plain `mo.md(...)`.
Images show as their alt text.

### Notes for yourself

A cell named `instructor` or `instructor_<anything>`, like the guide that `init` writes, is for you only.
The build removes it from the student notebook.
It can't define names that other cells use, hold tests, or call `mogs.grade`.

## Build

To build the assignment, run:

```bash
mogs build examples/demo
```

It prints what to do with the result:

```text
All done!
Upload to Gradescope  : examples/demo/dist/autograder/hw-autograder-2026_09_30T12_30_41_890583.zip
Distribute to students: examples/demo/dist/student/hw.zip
```

The build writes two directories next to the notebook:

```text
examples/demo/
├── hw.py
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
  It contains one directory with the student notebook, its public tests in `tests/`, and the data files.
- `dist/autograder/` is for Gradescope.
  Upload `<name>-autograder-<timestamp>.zip` to Gradescope as the autograder.
  The directory also contains the solved notebook, i.e., the student notebook with the solutions, together with its public tests, the data files and `requirements.txt`.

Each build replaces both directories, and a failed build removes them.

Before writing them, the build checks the assignment by grading two notebooks with the real grader: the solved notebook and the blank student notebook.
It does this in a fresh environment with the same pinned requirements that Gradescope gets.
That's why the build needs network access, and why the first build of an assignment takes a while: it has to download the dependencies.

The solved notebook must get full marks, or the build fails.
The blank student notebook should get zero.
If a test passes on the blank notebook, it gives students free points, and the build warns you about it.

`build` runs on Linux and macOS.
On Windows, use WSL.

## Grade a single submission

To grade one submission locally, run:

```bash
mogs grade examples/demo submission.py
```

This uses the same grader and the same requirements as Gradescope.
The submission can be the zip a student exported, a directory with the notebook (e.g., an upload downloaded from Gradescope), or the notebook's `.py` file.
A zip is unpacked into a temporary directory and searched the same way Gradescope searches an upload.
Any other file is refused.
The command prints the score of each question and every failing test with its message, including the hidden tests.
If the notebook fails to import, the command still lists every test, then exits with status 1.
This is useful for answering regrade requests.

:::caution
This runs the student's code with your permissions.
Use a container for untrusted submissions.
:::

## Requirements

Gradescope installs the packages in the header's `dependencies`.
The build pins their versions, and Gradescope installs exactly those.

Custom package indexes in `[[tool.uv.index]]` carry over too.
For example, this installs the CPU-only build of `torch`:

```python
# dependencies = ["marimo", "mogs", "torch"]
#
# [tool.uv.sources]
# torch = [{ index = "pytorch-cpu", marker = "sys_platform != 'darwin'" }]
#
# [[tool.uv.index]]
# name = "pytorch-cpu"
# url = "https://download.pytorch.org/whl/cpu"
# explicit = true
```

The data files in `files` are copied next to the notebook on Gradescope, at the same relative paths as on your machine.
So, code that opens `data/scores.csv` relative to the notebook works in both places.

## Gradescope

To set up the assignment on Gradescope:

1. Create a programming assignment.
2. Pick an Ubuntu 22.04 base image and upload the autograder zip.
3. Pick a container with more memory than `memory`, and a timeout longer than `budget`.
4. If the assignment has written answers, add manually graded questions to the same assignment.

Then, upload `dist/autograder/hw.py` as a test submission to check the autograder.
It should get full marks.

Students see their total, each question's score, and a "Public tests" row listing each public test with its failure message.
Each question also gets a row, visible only to you, that lists all its tests and their messages, hidden ones included.
A question's points are split evenly across its tests.

## Security

On Gradescope, student code runs under a separate user account with limited permissions.
The public and hidden tests also run in separate processes.
As a result, the student's code can't read the hidden tests from disk, and it can't write the results file.
If the code crashes or loops forever, only the test where it happens fails.

There are limits, though.
A determined student can still fake a passing test from inside their own code, and the container has internet access.
Otter Grader has the same limits, and it runs student code as root.
