# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "marimo",
#     "marimo-gradescope",
# ]
#
# [tool.marimo-gradescope]
# name = "Fixture"
#
# [tool.marimo-gradescope.questions]
# q1 = 4
# q2 = { points = 6, title = "Vowels" }
# q3 = { points = 5, manual = true }
# ///

import marimo

__generated_with = "0.24.2"
app = marimo.App()

with app.setup:
    import marimo as mo

    import marimo_gradescope as mgs


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    # Fixture assignment

    ## Question 1
    """)
    return


@app.function
def add(a, b):
    """Sum of two numbers."""
    # BEGIN SOLUTION
    return a + b
    # END SOLUTION


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
    def big(n):
        return 10**n

    def test_q1_add_large():
        assert add(big(20), 1) == big(20) + 1

    return


@app.cell
def _():
    mgs.check("q1")
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## Question 2
    """)
    return


@app.function
def count_vowels(s):
    """Number of vowels in `s`."""
    # BEGIN SOLUTION
    return sum(char in "aeiou" for char in s.lower())
    # END SOLUTION


@app.cell
def _():
    def test_q2_count():
        assert count_vowels("marimo") == 3

    return


@app.cell
def _():
    # HIDDEN
    def test_q2_upper():
        assert count_vowels("AEIOU") == 5

    def test_q2_empty():
        assert count_vowels("") == 0

    return


@app.cell
def _():
    mgs.check("q2")
    return


@app.cell(hide_code=True)
def prompt_q3():
    mo.md(r"""
    ## Question 3

    Why is addition commutative?
    """)
    return


@app.cell
def answer_q3():
    mo.md(r"""
    Because the order of the terms does not change the sum.
    """)
    return


if __name__ == "__main__":
    app.run()
