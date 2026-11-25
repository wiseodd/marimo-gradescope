# A submission whose `misbehave` crashes the interpreter.

import marimo

__generated_with = "0.25.2"
app = marimo.App()


@app.function
def add(a, b):
    """Sum of two numbers."""
    return a + b


@app.function
def misbehave():
    """Read address 0."""
    import ctypes

    return ctypes.string_at(0)


if __name__ == "__main__":
    app.run()
