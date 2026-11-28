//! Gradescope's `setup.sh` and `run_autograder`, from the files in `templates/`.

/// The uv that `setup.sh` installs on Gradescope.
pub const UV_VERSION: &str = "0.11.21";

/// The `run_autograder` script.
pub const RUN_AUTOGRADER: &str = include_str!("../templates/run_autograder");

/// `setup.sh`, with `${uv_version}`, `${python}` and `${user}` to fill in.
const SETUP: &str = include_str!("../templates/setup.sh");

/// Render `setup.sh` for an assignment on Python `python`, a version such as `3.13`, which
/// `mogs._analysis.build_inputs` checks.
pub fn setup_script(python: &str) -> String {
    SETUP
        .replace("${uv_version}", UV_VERSION)
        .replace("${python}", python)
        .replace("${user}", mogs_grader::STUDENT_USER)
}

#[cfg(test)]
mod tests {
    use std::process::Command;

    use super::*;

    /// `bash -n` on `script`, and shellcheck when it is installed.
    #[cfg(unix)]
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
        let script = super::setup_script("3.12");

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
        #[cfg(unix)]
        bash_check(&script);
    }

    #[test]
    fn setup_script_takes_a_patch_version() {
        let script = super::setup_script("3.13.5");

        assert!(script.contains("--python 3.13.5 /opt/mogs-venv"));
    }

    #[test]
    fn run_autograder_runs_the_hidden_command() {
        assert_eq!(
            RUN_AUTOGRADER,
            "#!/usr/bin/env bash\nset -euo pipefail\nexec /opt/mogs-venv/bin/mogs autograde\n"
        );
        #[cfg(unix)]
        bash_check(RUN_AUTOGRADER);
    }
}
