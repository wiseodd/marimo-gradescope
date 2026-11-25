# A submission that does not parse.

import marimo

__generated_with = "0.25.2"
app = marimo.App()


@app.function
def add(a, b)
    return a + b


if __name__ == "__main__":
    app.run()
