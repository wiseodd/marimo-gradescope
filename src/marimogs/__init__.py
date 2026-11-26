"""Gradescope autograding for marimo notebooks."""

import sysconfig
from importlib.metadata import version

from marimogs.assignment import assignment
from marimogs.check import grade
from marimogs.export import export, export_button

__all__ = ["assignment", "export", "export_button", "grade"]
__version__ = version("marimo-gradescope")

# marimo 0.25 and 0.26 run pytest after each test cell, then call `sysconfig.get_path` for the
# first time while iterating over `sys.modules`. That call imports a module, and the iteration
# fails with "dictionary changed size during iteration". Calling it here, when the setup cell
# imports this package, imports that module before marimo's loop.
sysconfig.get_paths()
