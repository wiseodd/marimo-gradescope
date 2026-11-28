//! Gradescope's `setup.sh` and `run_autograder`, from the files in `templates/`.

use std::sync::LazyLock;

use regex::Regex;

use crate::{Class, Interpreter};

/// The uv that `setup.sh` installs on Gradescope.
pub const UV_VERSION: &str = "0.11.21";

/// The `run_autograder` script.
pub const RUN_AUTOGRADER: &str = include_str!("../templates/run_autograder");

/// `setup.sh`, with `${uv_version}`, `${python}` and `${user}` to fill in.
const SETUP: &str = include_str!("../templates/setup.sh");

/// A Python version such as `3.13` or `3.13.5`.
static PYTHON_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A3\.\d+(\.\d+)?\z").expect("the pattern is valid"));

/// Render `setup.sh` for an assignment on Python `python`.
///
/// # Errors
///
/// `ConfigError` if `python` is not a version such as `3.13` or `3.13.5`.
pub fn setup_script<I: Interpreter>(i: &I, python: &str) -> Result<String, I::Error> {
    if !PYTHON_RE.is_match(python) {
        let message = format!(
            "mogs.assignment() `python` must be a version such as \"3.13\", got {}.",
            i.host().repr(python)
        );
        return Err(i.raise(Class::Config, message.into()));
    }

    Ok(SETUP
        .replace("${uv_version}", UV_VERSION)
        .replace("${python}", python)
        .replace("${user}", mogs_grader::STUDENT_USER))
}

#[cfg(test)]
mod tests {
    use std::process::Command;

    use super::*;
    use crate::fake::Fake;

    /// `bash -n` on `script`, and shellcheck when it is installed.
    fn bash_check(script: &str) {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("script.sh");
        std::fs::write(&path, script).unwrap();
        let _spawning = crate::fake::spawning();

        let bash = Command::new("bash").arg("-n").arg(&path).output().unwrap();
        assert!(bash.status.success(), "{bash:?}");

        if let Ok(shellcheck) = Command::new("shellcheck").arg(&path).output() {
            assert!(shellcheck.status.success(), "{shellcheck:?}");
        }
    }

    #[test]
    fn setup_script() {
        let script = super::setup_script(&Fake::default(), "3.12").unwrap();

        assert!(script.starts_with("#!/usr/bin/env bash\n"));
        assert!(script.contains("set -euo pipefail"));
        assert!(script.contains(
            "command -v curl >/dev/null || { apt-get update && apt-get install -y curl "
        ));
        assert!(script.find("command -v curl") < script.find("astral.sh"));
        assert!(script.contains(
            "command -v pkill >/dev/null || { apt-get update && apt-get install -y procps; }"
        ));
        assert!(script.contains(&format!("https://astral.sh/uv/{UV_VERSION}/install.sh")));
        assert!(script.contains("UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1"));
        assert!(script.contains("UV_PYTHON_INSTALL_DIR=/opt/mogs-python"));
        assert!(script.contains("uv venv --managed-python --python 3.12 /opt/mogs-venv"));
        assert!(script.contains("--index-strategy unsafe-best-match"));
        assert!(script.contains("--compile-bytecode"));
        assert!(
            script.contains(
                "useradd --system --no-create-home --shell /usr/sbin/nologin mogs-student"
            )
        );
        assert!(script.contains("chmod -R a+rX /opt/mogs-python /opt/mogs-venv"));
        assert!(script.contains("chmod -R go-rwx /autograder/source"));
        assert!(script.trim_end().ends_with(
            "runuser -u mogs-student -- /opt/mogs-venv/bin/python -c \"import mogs, marimo\""
        ));
        assert!(!script.contains("${"));
        bash_check(&script);
    }

    #[test]
    fn setup_script_takes_a_patch_version() {
        let script = super::setup_script(&Fake::default(), "3.13.5").unwrap();

        assert!(script.contains("--python 3.13.5 /opt/mogs-venv"));
    }

    #[test]
    fn setup_script_rejects_bad_python() {
        for python in ["3.13; rm -rf /", "latest", "3", "", "3.13\n"] {
            let error = super::setup_script(&Fake::default(), python).unwrap_err();

            assert_eq!(
                error,
                format!(
                    "Config: mogs.assignment() `python` must be a version such as \"3.13\", \
                     got '{python}'."
                )
            );
        }
    }

    #[test]
    fn run_autograder_runs_the_hidden_command() {
        assert_eq!(
            RUN_AUTOGRADER,
            "#!/usr/bin/env bash\nset -euo pipefail\nexec /opt/mogs-venv/bin/mogs autograde\n"
        );
        bash_check(RUN_AUTOGRADER);
    }
}
