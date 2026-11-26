# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "marimo",
#     "mogs",
# ]
# ///

import marimo

__generated_with = "0.24.2"
app = marimo.App()

with app.setup:
    import mogs


@app.cell
def _():
    mogs.assignment(name="Plain cell")
    return


@app.function
def three():
    n = 3  # SOLUTION
    return n


@app.cell
def _():
    def test_q1_three():
        assert three() == 3

    return


@app.cell
def _():
    mogs.grade("q1", points=1)
    return


@app.function
def double(x):
    y = 2 * x  # SOLUTION
    return y


@app.cell
def _():
    def test_q2_double():
        assert double(2) == 4

    return


@app.cell
def _():
    mogs.grade("q2", points=1)
    return


@app.cell
def _():
    demo = double(4) + 1
    return (demo,)


if __name__ == "__main__":
    app.run()
