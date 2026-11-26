#!/usr/bin/env bash
# Gradescope runs this once, as root, when it builds the autograder image.
set -euo pipefail

# A pinned uv.
command -v curl >/dev/null || { apt-get update && apt-get install -y curl ca-certificates; }
# The grader kills the grading user's processes with pkill.
command -v pkill >/dev/null || { apt-get update && apt-get install -y procps; }
curl -LsSf https://astral.sh/uv/${uv_version}/install.sh \
    | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh

# Python and the dependencies, outside /autograder/source so the student user can read them.
export UV_PYTHON_INSTALL_DIR=/opt/mogs-python
uv venv --managed-python --python ${python} /opt/mogs-venv
# Compiled now, so grading children (unprivileged, no write access) do not recompile per run.
uv pip install --python /opt/mogs-venv/bin/python --index-strategy unsafe-best-match \
    --compile-bytecode -r /autograder/source/requirements.txt

# The unprivileged user that runs student code.
id -u ${user} >/dev/null 2>&1 \
    || useradd --system --no-create-home --shell /usr/sbin/nologin ${user}

# Python and the venv are readable by everyone; the hidden tests only by root.
chmod -R a+rX /opt/mogs-python /opt/mogs-venv
chmod -R go-rwx /autograder/source

# Fail the build here, not at grading time, if the student user cannot import the grader.
runuser -u ${user} -- /opt/mogs-venv/bin/python -c "import mogs, marimo"
