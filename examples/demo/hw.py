# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "marimo",
#     "marimo-gradescope",
# ]
# ///

import marimo

__generated_with = "0.24.2"
app = marimo.App()

with app.setup:
    import csv

    import marimo as mo

    import marimo_gradescope as mogs

    SCORES = mo.notebook_dir() / "data" / "scores.csv"


@app.cell
def _():
    mogs.assignment(files=["data/scores.csv"])
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    # Demo assignment

    Write the functions below, then run the check cell under each question to see how your
    code does on the public tests. Gradescope also runs a few hidden tests.

    ## Question 1: Average

    Write `mean(xs)`, the average of a list of numbers.
    """)
    return


@app.function
def mean(xs):
    """Average of the numbers in `xs`."""
    # BEGIN SOLUTION
    return sum(xs) / len(xs)
    # END SOLUTION


@app.cell
def _():
    def test_q1_whole_numbers():
        assert mean([1, 2, 3]) == 2

    def test_q1_fractions():
        assert mean([1, 2]) == 1.5

    return


@app.cell
def _():
    # HIDDEN
    def test_q1_single():
        assert mean([7]) == 7

    def test_q1_negative():
        assert mean([-4, 4, -6]) == -2

    return


@app.cell
def _():
    mogs.grade("q1", points=4, title="Average")
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## Question 2: Reading scores

    `data/scores.csv` holds one score per student:
    """)
    return


@app.cell
def _():
    mo.plain_text(SCORES.read_text(encoding="utf-8"))
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    Write `read_scores(path)`. It reads such a file and returns a dict that maps each name
    to its score as a float.
    """)
    return


@app.function
def read_scores(path):
    """Scores by name from a CSV file with columns `name` and `score`."""
    # BEGIN SOLUTION
    with open(path, encoding="utf-8", newline="") as file:
        return {row["name"]: float(row["score"]) for row in csv.DictReader(file)}
    # END SOLUTION


@app.cell
def _():
    def test_q2_reads_scores():
        scores = read_scores(SCORES)
        assert scores["Ada"] == 92

    def test_q2_fractional_score():
        scores = read_scores(SCORES)
        assert scores["Grace"] == 85.5

    return


@app.cell
def _():
    # HIDDEN
    def test_q2_every_student():
        scores = read_scores(SCORES)
        assert sorted(scores) == ["Ada", "Alan", "Edsger", "Grace", "Katherine"]

    def test_q2_floats():
        scores = read_scores(SCORES)
        assert all(type(score) is float for score in scores.values())

    return


@app.cell
def _():
    mogs.grade("q2", points=6, title="Reading scores")
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## Question 3: Empty lists

    What does your `mean` do with an empty list? Is that a good answer? Write one or two
    sentences in the cell below.
    """)
    return


@app.cell
def _():
    mo.md(r"""
    <!-- BEGIN SOLUTION -->
    It divides by zero and raises `ZeroDivisionError`. That is a fair answer: an empty list
    has no average, and returning 0 would hide the mistake.
    <!-- END SOLUTION -->
    """)
    return


@app.cell
def _():
    mogs.grade("q3", points=5, title="Empty lists")
    return


if __name__ == "__main__":
    app.run()
