# A submission whose setup cell fails, so importing it raises.

import marimo

__generated_with = "0.25.2"
app = marimo.App()

with app.setup:
    import a_module_that_does_not_exist  # noqa: F401


@app.function
def add(a, b):
    """Sum of two numbers."""
    return a + b


if __name__ == "__main__":
    app.run()
