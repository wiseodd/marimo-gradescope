# A submission whose `misbehave` runs a C loop that never checks for signals.

import marimo

__generated_with = "0.25.2"
app = marimo.App()


@app.function
def add(a, b):
    """Sum of two numbers."""
    return a + b


@app.function
def misbehave():
    """Hash for minutes inside C, where SIGALRM handlers cannot run."""
    import hashlib

    return hashlib.pbkdf2_hmac("sha256", b"x", b"y", 2**31 - 1)


if __name__ == "__main__":
    app.run()
