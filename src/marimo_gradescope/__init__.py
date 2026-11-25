"""Gradescope autograding for marimo notebooks."""

from importlib.metadata import version

from marimo_gradescope.check import check
from marimo_gradescope.export import export, export_button

__all__ = ["check", "export", "export_button"]
__version__ = version("marimo-gradescope")
