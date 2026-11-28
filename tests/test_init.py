import subprocess
import sys

from mogs._env import clean_env

# marimo's reactive tests call `sysconfig.get_path` for the first time while iterating over
# `sys.modules`. If that call imports a module, marimo 0.25 and 0.26 crash.
NEW_MODULES = """\
import sys
import sysconfig

import mogs

before = set(sys.modules)
sysconfig.get_paths()
print(sorted(set(sys.modules) - before))
"""


def test_import_loads_what_sysconfig_needs():
    env = clean_env()

    result = subprocess.run(
        [sys.executable, "-c", NEW_MODULES], capture_output=True, text=True, check=True, env=env
    )

    assert result.stdout.strip() == "[]", result.stdout
