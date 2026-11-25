# A submission whose `misbehave` allocates until memory runs out.

import marimo

__generated_with = "0.25.2"
app = marimo.App()


@app.function
def add(a, b):
    """Sum of two numbers."""
    return a + b


@app.function
def misbehave():
    """Allocate 100 MB blocks forever."""
    blocks = []
    while True:
        blocks.append(bytearray(10**8))


if __name__ == "__main__":
    app.run()
