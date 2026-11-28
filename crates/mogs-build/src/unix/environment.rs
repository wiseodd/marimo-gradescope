//! The Python environment the build grades in.
//!
//! It holds what Gradescope installs: the pinned requirements and mogs. The mogs in it is the
//! one that runs the build, so a build from a source checkout grades with that checkout.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use mogs_grader::GradeError;

use crate::unix::{Class, Interpreter, process};

/// Create a virtual environment in `folder` and install the grading requirements.
///
/// # Arguments
///
/// * `requirements` - Text of the `requirements.txt` to install, without a mogs line: the
///   running mogs is installed instead.
/// * `folder` - Where to create the environment; must not exist.
/// * `python_version` - Python version for the environment, as on Gradescope.
///
/// Returns the environment's interpreter.
///
/// # Errors
///
/// `RequirementsError` if `uv` is missing, or the environment cannot be created or the
/// requirements cannot be installed.
pub fn grading_python<I: Interpreter>(
    i: &I,
    requirements: &str,
    folder: &Path,
    python_version: &str,
) -> Result<PathBuf, I::Error> {
    let Some(uv) = i.which("uv")? else {
        let message = "`uv` is not on PATH; it is needed to install the requirements.";
        return Err(i.raise(Class::Requirements, message.into()));
    };

    let mut venv = Command::new(&uv);
    venv.args(["venv", "--quiet", "--python", python_version])
        .arg(folder);
    run_uv(i, &mut venv, "create")?;

    let python = folder.join("bin").join("python");
    let listed = folder.join("requirements.txt");
    fs::write(&listed, requirements)
        .map_err(|error| i.grade_error(GradeError::os(error, &listed)))?;

    // Gradescope installs with pip, which takes each package's best version from any index.
    let mut install = Command::new(&uv);
    install
        .args(["pip", "install", "--quiet", "--python"])
        .arg(&python)
        .arg("-r")
        .arg(&listed)
        .args(["--index-strategy", "unsafe-best-match"])
        .args(i.tool_install_args()?);
    run_uv(i, &mut install, "install the requirements in")?;

    Ok(python)
}

/// Run `uv`, which `action`s the environment.
fn run_uv<I: Interpreter>(i: &I, command: &mut Command, action: &str) -> Result<(), I::Error> {
    let output = process::run(i, command)?;

    if !output.status.success() {
        let stderr = i.decoded(&[&output.stderr])?;
        let message = format!("`uv` could not {action} the build's grading environment:\n{stderr}");
        return Err(i.raise(Class::Requirements, message.into()));
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::unix::fake::{Fake, script, spawning};

    /// A fake `uv` in `folder` that logs its arguments, one per line, then runs `body`.
    fn fake_uv(folder: &Path, body: &str) -> Fake {
        let uv = folder.join("uv");
        let log = folder.join("log");
        let logged = format!(
            "printf '%s\\n' \"$@\" >> '{0}'\necho >> '{0}'\n",
            log.display()
        );
        script(&uv, &(logged + body));

        Fake {
            found: Some(uv),
            tool: vec!["--editable".into(), "/src/mogs".into()],
        }
    }

    /// The calls `fake_uv` logged.
    fn calls(folder: &Path) -> Vec<Vec<String>> {
        let log = fs::read_to_string(folder.join("log")).unwrap();

        log.split("\n\n")
            .filter(|call| !call.trim().is_empty())
            .map(|call| call.lines().map(String::from).collect())
            .collect()
    }

    #[test]
    fn uv_missing_is_an_error() {
        let error = grading_python(&Fake::default(), "", Path::new("/x/env"), "3.13").unwrap_err();

        assert_eq!(
            error,
            "Requirements: `uv` is not on PATH; it is needed to install the requirements."
        );
    }

    #[test]
    fn creates_the_environment_and_installs_like_pip() {
        let tmp = tempfile::tempdir().unwrap();
        let env = tmp.path().join("env");
        let fake = fake_uv(tmp.path(), &format!("mkdir -p '{}'", env.display()));

        let _spawning = spawning();
        let python = grading_python(&fake, "torch==1\n", &env, "3.12").unwrap();

        assert_eq!(python, env.join("bin/python"));
        assert_eq!(
            fs::read_to_string(env.join("requirements.txt")).unwrap(),
            "torch==1\n"
        );
        let [venv, install] = &calls(tmp.path())[..] else {
            panic!("two calls of uv");
        };
        assert_eq!(
            venv,
            &[
                "venv",
                "--quiet",
                "--python",
                "3.12",
                &env.display().to_string()
            ]
        );
        assert_eq!(
            install,
            &[
                "pip",
                "install",
                "--quiet",
                "--python",
                &python.display().to_string(),
                "-r",
                &env.join("requirements.txt").display().to_string(),
                "--index-strategy",
                "unsafe-best-match",
                "--editable",
                "/src/mogs",
            ]
        );
    }

    #[test]
    fn failed_uv_names_the_action_and_shows_its_stderr() {
        let tmp = tempfile::tempdir().unwrap();
        let fake = fake_uv(tmp.path(), "echo '  no Python 3.99  ' >&2; exit 2");

        let _spawning = spawning();
        let error = grading_python(&fake, "", &tmp.path().join("env"), "3.99").unwrap_err();

        assert_eq!(
            error,
            "Requirements: `uv` could not create the build's grading environment:\n\
             no Python 3.99"
        );
    }

    #[test]
    fn failed_install_shows_uv_stderr() {
        let tmp = tempfile::tempdir().unwrap();
        let env = tmp.path().join("env");
        let body = format!(
            "[ \"$1\" = venv ] && exec mkdir -p '{}'\necho 'no-such-package' >&2\nexit 1",
            env.display()
        );
        let fake = fake_uv(tmp.path(), &body);

        let _spawning = spawning();
        let error = grading_python(&fake, "no-such-package==1\n", &env, "3.13").unwrap_err();

        assert_eq!(
            error,
            "Requirements: `uv` could not install the requirements in the build's grading \
             environment:\nno-such-package"
        );
    }
}
