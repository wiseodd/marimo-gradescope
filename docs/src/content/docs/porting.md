---
title: Porting from Otter
description: Port an Otter Assign notebook to mogs.
---

mogs covers a subset of [Otter Assign](https://otter-grader.readthedocs.io/en/latest/otter_assign/)'s v1 notebook format, and the port is done by hand.
Read the [instructor guide](/instructors/) first.

## Convert

Turn the Jupyter notebook into a marimo notebook:

```bash
marimo convert notebook.ipynb -o notebook.py
```

Otter's raw cells come out as code cells that hold the YAML, or as unparsable cells when the YAML isn't valid Python.
Move their settings into `mogs.assignment(...)` and `mogs.grade(...)`, then delete them.
Until the notebook calls `mogs.assignment(...)`, `mogs build` names the file and line of the first Otter marker.

The conversion keeps the test expressions but drops their saved outputs.
Keep the `.ipynb` open to read the expected values.

## Mapping

| Otter                                                | mogs                                                                                                            |
| ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `# ASSIGNMENT CONFIG` raw cell                       | A cell with one `mogs.assignment(...)` call.                                                                    |
| `name`                                               | `name=`.                                                                                                        |
| `files`                                              | `files=`. The files go to students and to Gradescope.                                                           |
| `autograder_files`, `student_files`                  | None.                                                                                                           |
| `requirements` as a list                             | `dependencies` in the notebook's header.                                                                        |
| `requirements` as a file                             | `requirements="requirements.txt"`, or `dependencies` in the header.                                             |
| `environment`                                        | None. Gradescope installs with uv.                                                                              |
| `python_version`                                     | `python="3.13"`.                                                                                                |
| `seed`, `# SEED`                                     | None.                                                                                                           |
| `init_cell`, `check_cell`, `check_all_cell`          | None needed. The grade cell shows each public test's result.                                                    |
| `export_cell`                                        | None needed. The student notebook ends with an **Export submission** button.                                    |
| `solutions_pdf`, `template_pdf`                      | None.                                                                                                           |
| `generate`, `show_hidden`, `show_stdout`             | None. Students see the public test results and their scores.                                                    |
| `points_possible`, `score_threshold`                 | None. The total is the sum of the question points.                                                              |
| `save_environment`, `variables`, `runs_on`           | None.                                                                                                           |
| `# BEGIN QUESTION` raw cell                          | A `mogs.grade("q1", points=2)` cell after the question's tests.                                                 |
| question `name`                                      | The question id, letters and digits only: `q1a` works, `q1_a` and `q1-a` don't.                                 |
| question `points`                                    | `points=`. It is required.                                                                                      |
| `manual: true`                                       | A question without tests. See [Written answers](/instructors/#written-answers).                                 |
| `all_or_nothing`                                     | None.                                                                                                           |
| `# BEGIN PROMPT` raw cell                            | `<!-- PROMPT: ... -->` inside the Markdown answer cell.                                                         |
| `""" # BEGIN PROMPT` in a code cell                  | `# PROMPT:` lines between `# BEGIN SOLUTION` and `# END SOLUTION`.                                              |
| `# BEGIN SOLUTION` raw cell                          | `# BEGIN SOLUTION` and `# END SOLUTION` inside the answer cell.                                                 |
| `# BEGIN SOLUTION` in a code cell                    | The same.                                                                                                       |
| solution in a Markdown cell                          | `<!-- BEGIN SOLUTION -->` and `<!-- END SOLUTION -->` inside `mo.md(...)`.                                      |
| `# SOLUTION` at the end of a line                    | The same, except that a line other than an assignment, `return` or `yield` is removed, not replaced with `...`. |
| `# SOLUTION NO PROMPT`, `# BEGIN SOLUTION NO PROMPT` | None. The block becomes `...`, or the `# PROMPT:` lines.                                                        |
| `# BEGIN TESTS` raw cell                             | A cell of functions named `test_<qid>_<name>`.                                                                  |
| test cell with a saved output                        | `assert expression == output`.                                                                                  |
| exception-format test (`ok_format: false`)           | A function named `test_<qid>_<name>` with no arguments. It calls the answer functions itself.                   |
| `# HIDDEN` on a test cell                            | `# HIDDEN` as the first line of a cell of hidden tests.                                                         |
| per-test `points`                                    | None. A question's points are split evenly across its tests.                                                    |
| `success_message`                                    | None.                                                                                                           |
| `failure_message`                                    | The assert's message: `assert x == 1, "x should be 1"`.                                                         |
| `# IGNORE`                                           | A cell named `instructor` or `instructor_<anything>`.                                                           |
| `# BEGIN PLUGIN`                                     | None.                                                                                                           |

## Answers are functions

A test can read functions, names from the setup cell, and names from `# HIDDEN` cells.
It can't read a variable that a regular cell defines.
An Otter question that asks for `answer = ...` becomes a function that returns the answer:

```python
@app.function
def answer():
    return 42  # SOLUTION
```

See [Notebook](/instructors/#notebook) for why.

## Example

An Otter question with one public and one hidden test, as cells:

```text
[raw]       # BEGIN QUESTION
            name: q1
            points: 2
[markdown]  **Question 1.** Write `square(x)`.
[raw]       # BEGIN SOLUTION
[code]      def square(x):
                return x * x # SOLUTION
[raw]       # END SOLUTION
[raw]       # BEGIN TESTS
[code]      square(3)
[output]    9
[code]      # HIDDEN
            square(-2)
[output]    4
[raw]       # END TESTS
[raw]       # END QUESTION
```

The same question in mogs:

```python
@app.cell(hide_code=True)
def _():
    mo.md(r"""
    **Question 1.** Write `square(x)`.
    """)
    return


@app.function
def square(x):
    return x * x  # SOLUTION


@app.cell
def _():
    def test_q1_positive():
        assert square(3) == 9

    return


@app.cell
def _():
    # HIDDEN
    def test_q1_negative():
        assert square(-2) == 4

    return


@app.cell
def _():
    mogs.grade("q1", points=2)
    return
```

Each test is worth 1 point.
