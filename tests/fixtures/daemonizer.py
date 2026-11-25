# A submission whose `misbehave` leaves a detached grandchild and a thread behind.

import marimo

__generated_with = "0.25.2"
app = marimo.App()


@app.function
def add(a, b):
    """Sum of two numbers."""
    return a + b


@app.function
def misbehave():
    """Fork a grandchild in its own session and start a thread that never ends.

    The grandchild keeps every descriptor of the grading child, writes its pid to
    `grandchild.pid` and sleeps for a minute. The thread is not a daemon, so the child cannot
    exit on its own.
    """
    import os
    import threading
    import time

    if os.fork() == 0:
        os.setsid()
        with open("grandchild.pid", "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        time.sleep(60)
        os._exit(0)

    threading.Thread(target=time.sleep, args=(3600,)).start()


if __name__ == "__main__":
    app.run()
