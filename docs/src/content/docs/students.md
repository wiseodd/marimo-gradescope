---
title: Students
description: Work on the assignment and submit it to Gradescope.
---

## Installing marimo

Install Python 3.13 or 3.14, for example via [pyenv](https://github.com/pyenv/pyenv#installation).
Then, install marimo and uv in your Python environment.

```bash
pip install marimo uv
```

This only needs to be done once.

## Open

Download the assignment, unzip it.
For example, you might see the following directory structure:

```text
hw/
├── hw.py
├── data/
│   └── scores.csv
└── tests/
    ├── q1.py
    └── q2.py
```

In the directory containing the assignment, run this:

```bash
marimo edit <assignment filename>.py
```

marimo asks whether to run the notebook in a sandbox with the notebook's packages.
Press Enter to say yes.
The sandbox is a separate Python environment.
Its packages don't clash with the ones you already have.
The first start takes a while since it has to install the required packages.

## Work

Fill in every `...` in the notebook.

Keep each function self-contained.
Your solution must only use the provided function arguments, the imports at the top of the notebook, and other functions.
Don't rename the functions since otherwise the autograder will fail and you will not receive the correct score.

At the end of each question, a grade cell runs the public tests whenever you edit your code.
It reads the tests from the `tests` directory.
Do not modify, delete, or move that directory.

On Gradescope, your code will be run against both public and additional hidden tests---you can't see the latter.
So, passing all public tests doesn't guarantee full marks.

## Submit

1. Save the notebook.
2. Click **Export submission** at the bottom of the notebook.
   If you don't see the button, run the notebook first with **Run all stale cells** (Ctrl+Shift+R, or Cmd+Shift+R on a Mac).
3. Download the zip and upload it to Gradescope.

The export runs the public tests on the saved file and tells you if something is wrong.
If the assignment has written answers, the zip also contains a PDF of them.
The export warns about any written answer that is still blank or unchanged.
