# A submission whose `misbehave` ends the process without cleanup.

import marimo

__generated_with = "0.25.2"
app = marimo.App()


@app.function
def add(a, b):
    """Sum of two numbers."""
    return a + b


@app.function
def misbehave():
    """Exit at once with code 3."""
    import os

    os._exit(3)


if __name__ == "__main__":
    app.run()
