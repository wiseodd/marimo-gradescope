# A submission in miniature: `add` is a pure function, `double` depends on another cell,
# and one cell has a side effect. `double` sits in a cell named `double`, so the module
# attribute is a marimo cell object, not the function.
#
# The side-effect cell appends a line to the file named by `MOGS_SIDE_EFFECT`, so tests can
# tell whether the whole notebook ran.

import marimo

__generated_with = "0.25.2"
app = marimo.App()

with app.setup:
    import os
    from pathlib import Path


@app.function
def add(a, b):
    """Sum of two numbers."""
    return a + b


@app.cell
def _():
    factor = 2
    return (factor,)


@app.cell
def double(factor):
    def double(x):
        return factor * x

    return (double,)


@app.cell
def _():
    if "MOGS_SIDE_EFFECT" in os.environ:
        with Path(os.environ["MOGS_SIDE_EFFECT"]).open("a", encoding="utf-8") as file:
            file.write("ran\n")
    return


if __name__ == "__main__":
    app.run()
