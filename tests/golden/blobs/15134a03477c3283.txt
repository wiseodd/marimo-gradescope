# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "marimo",
# ]
#
# [tool.marimo.runtime]
# reactive_tests = false
# ///

# Runs under marimo without this package's runtime API: no config or check calls, so
# `marimo export html` exercises only marimo's pytest hook on pure test cells. Tests pass
# its config themselves.

import marimo

__generated_with = "0.25.2"
app = marimo.App()

with app.setup:
    import marimo as mo


@app.function
def add(a, b):
    """Sum of two numbers."""
    # BEGIN SOLUTION
    # PROMPT: total = None
    # PROMPT: return total
    return a + b
    # END SOLUTION


@app.function
def double(x):
    """Twice `x`."""
    factor = 2  # SOLUTION
    return factor * x


@app.cell
def _():
    def test_q1_add():
        assert add(1, 2) == 3

    def test_q1_add_negative():
        assert add(-1, -2) == -3

    return


@app.cell
def _():
    # HIDDEN
    def test_q1_add_zero():
        assert add(0, 0) == 0

    return


@app.cell
def _():
    def test_q2_double():
        assert double(2) == 4

    def test_q2_double_float():
        assert double(0.5) == 1.0

    return


@app.cell
def _():
    mo.md(r"""
    Done.
    """)
    return


if __name__ == "__main__":
    app.run()
