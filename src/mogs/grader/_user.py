"""The user that runs student code on Gradescope.

Kept apart from the sandbox, so build-time code can read it on any platform.
"""

from mogs._native import STUDENT_USER

__all__ = ["STUDENT_USER"]
