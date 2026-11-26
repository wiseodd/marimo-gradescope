"""The user that runs student code on Gradescope.

Kept apart from the sandbox, so build-time code can read it without importing Linux-only
modules.
"""

STUDENT_USER = "mgs-student"
