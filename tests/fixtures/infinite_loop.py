# A submission whose `misbehave` loops forever in Python.

import marimo

__generated_with = "0.25.2"
app = marimo.App()


@app.function
def add(a, b):
    """Sum of two numbers."""
    return a + b


@app.function
def misbehave():
    """Loop forever."""
    while True:
        pass


if __name__ == "__main__":
    app.run()
