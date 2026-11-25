# A submission whose `misbehave` writes 50 MB to stdout and stderr.

import marimo

__generated_with = "0.25.2"
app = marimo.App()


@app.function
def add(a, b):
    """Sum of two numbers."""
    return a + b


@app.function
def misbehave():
    """Write 25 MB to each stream."""
    import sys

    line = "x" * (1024 * 1024 - 1) + "\n"
    for _ in range(25):
        sys.stdout.write(line)
        sys.stderr.write(line)


if __name__ == "__main__":
    app.run()
