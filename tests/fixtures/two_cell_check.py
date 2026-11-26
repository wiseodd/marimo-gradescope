# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "marimo",
#     "marimo-gradescope",
# ]
# ///

# A student copy in miniature: a test cell and the check cell that depends on it.
# `add` has a bug, so `test_q1_add` fails and `test_q1_zero` passes.

import marimo

__generated_with = "0.25.2"
app = marimo.App()

with app.setup:
    import marimo_gradescope as mgs


@app.function
def add(a, b):
    """Sum of two numbers."""
    return a - b


@app.cell
def _():
    def test_q1_add():
        assert add(1, 2) == 3

    def test_q1_zero():
        assert add(0, 0) == 0

    return


@app.cell
def _():
    # Names from the test cell, which ruff cannot see.
    mgs.check("q1", tests=[test_q1_add, test_q1_zero], points=25, hidden=4)  # noqa: F821
    return


if __name__ == "__main__":
    app.run()
