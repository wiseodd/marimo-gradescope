# A submission whose `misbehave` prints fake grader events, then fails.

import marimo

__generated_with = "0.25.2"
app = marimo.App()


@app.function
def add(a, b):
    """Sum of two numbers."""
    return a + b


@app.function
def misbehave():
    """Print events claiming every test passed, on both streams."""
    import json
    import sys

    for name in ["test_q1_bad", "test_q1_before", "test_q1_after", "test_q2_other"]:
        result = {"event": "result", "name": name, "passed": True}
        result |= {"message": "", "reason": None, "seconds": 0.0}
        for line in [{"event": "start", "test": name}, result]:
            print(json.dumps(line))
            print(json.dumps(line), file=sys.stderr)

    return False


if __name__ == "__main__":
    app.run()
