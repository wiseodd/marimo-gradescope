//! The notebook's own environment, which the build checks the solved copy in.
//!
//! It is the one `mogs edit` opens: `marimo edit --sandbox` syncs the environment of the
//! notebook's PEP 723 header with `uv sync --script`, then runs Python from it with
//! `uv run --active`, layering marimo on top. The build does the same and layers the running
//! mogs on top, so a build from a source checkout checks with that checkout. Neither the
//! environment nor the notebook is changed.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::{Class, Interpreter, process};

/// How to start Python in the notebook's environment.
#[derive(Clone, Debug, PartialEq)]
pub struct NotebookPython {
    /// `uv`.
    uv: PathBuf,
    /// The environment's folder, for `VIRTUAL_ENV`.
    root: PathBuf,
    /// The arguments of `uv` up to and including `python`.
    args: Vec<OsString>,
}

impl NotebookPython {
    /// A command that runs `python` in the environment; add Python's arguments.
    pub fn command(&self) -> Command {
        let mut command = Command::new(&self.uv);
        command
            .args(&self.args)
            .env("VIRTUAL_ENV", &self.root)
            .env_remove("UV_PROJECT_ENVIRONMENT")
            .env_remove("UV_PYTHON");

        command
    }
}

/// Sync the environment of the notebook `source` and give how to run Python in it.
///
/// # Errors
///
/// `RequirementsError` if `uv` is missing, or cannot sync or find the environment.
pub fn notebook_python<I: Interpreter>(i: &I, source: &Path) -> Result<NotebookPython, I::Error> {
    let Some(uv) = i.which("uv")? else {
        let message = "`uv` is not on PATH; it is needed to set up the notebook's environment.";
        return Err(i.raise(Class::Requirements, message.into()));
    };
    // `uv` runs from the notebook's folder, so a relative path would name the wrong file.
    let source = &std::path::absolute(source).map_err(|error| i.os(error, source))?;
    let folder = source.parent().expect("the notebook is in a folder");

    let mut sync = Command::new(&uv);
    sync.args(["sync", "--quiet", "--script"]).arg(source);
    run_uv(i, &mut sync, folder, "sync")?;

    let mut find = Command::new(&uv);
    find.args(["python", "find", "--script"]).arg(source);
    let found = run_uv(i, &mut find, folder, "find")?;
    let python = PathBuf::from(i.decoded(&[&found])?);
    let root = python
        .parent()
        .and_then(Path::parent)
        .expect("the interpreter is in the environment's `bin` or `Scripts`")
        .to_path_buf();

    let mut args: Vec<OsString> = ["run", "--quiet", "--active", "--no-project", "--python"]
        .map(OsString::from)
        .into();
    args.push(python.into());
    args.extend(overlay(&i.tool_install_args()?));
    args.extend(["--", "python"].map(OsString::from));

    Ok(NotebookPython { uv, root, args })
}

/// The `uv run` arguments that layer what the `uv pip install` arguments `install` install.
fn overlay(install: &[String]) -> Vec<OsString> {
    match install {
        [flag, path] if flag == "--editable" => vec!["--with-editable".into(), path.into()],
        _ => install
            .iter()
            .flat_map(|requirement| ["--with".into(), requirement.into()])
            .collect(),
    }
}

/// Run `uv` from `folder`, which `action`s the notebook's environment; give its stdout.
fn run_uv<I: Interpreter>(
    i: &I,
    command: &mut Command,
    folder: &Path,
    action: &str,
) -> Result<Vec<u8>, I::Error> {
    // The header's `requires-python` picks the Python, not a `UV_PYTHON` of the caller's.
    command
        .current_dir(folder)
        .env_remove("VIRTUAL_ENV")
        .env_remove("UV_PROJECT_ENVIRONMENT")
        .env_remove("UV_PYTHON");
    let output = process::run(i, command)?;

    if !output.status.success() {
        let stderr = i.decoded(&[&output.stderr])?;
        let message = format!("`uv` could not {action} the notebook's environment:\n{stderr}");
        return Err(i.raise(Class::Requirements, message.into()));
    }

    Ok(output.stdout)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fake::Fake;

    #[test]
    fn uv_missing_is_an_error() {
        let error = notebook_python(&Fake::default(), Path::new("/x/hw.py")).unwrap_err();

        assert_eq!(
            error,
            "Requirements: `uv` is not on PATH; it is needed to set up the notebook's \
             environment."
        );
    }

    #[test]
    fn overlay_layers_the_running_mogs() {
        let editable = ["--editable".to_string(), "/src/mogs".to_string()];
        let pinned = ["mogs==0.8".to_string()];

        assert_eq!(overlay(&editable), ["--with-editable", "/src/mogs"]);
        assert_eq!(overlay(&pinned), ["--with", "mogs==0.8"]);
    }

    #[cfg(unix)]
    mod with_uv {
        use std::fs;

        use super::*;
        use crate::fake::{script, spawning};

        /// A fake `uv` in `folder` that logs its arguments, one per line, then runs `body`.
        fn fake_uv(folder: &Path, body: &str) -> Fake {
            let uv = folder.join("uv");
            let log = folder.join("log");
            let logged = format!(
                "printf '%s\\n' \"$PWD\" \"$@\" >> '{0}'\necho >> '{0}'\n",
                log.display()
            );
            script(&uv, &(logged + body));

            Fake {
                found: Some(uv),
                tool: vec!["--editable".into(), "/src/mogs".into()],
            }
        }

        /// The calls `fake_uv` logged: the working directory, then the arguments.
        fn calls(folder: &Path) -> Vec<Vec<String>> {
            let log = fs::read_to_string(folder.join("log")).unwrap();

            log.split("\n\n")
                .filter(|call| !call.trim().is_empty())
                .map(|call| call.lines().map(String::from).collect())
                .collect()
        }

        #[test]
        fn syncs_the_script_environment_and_layers_mogs() {
            let tmp = tempfile::tempdir().unwrap();
            // macOS's temporary folder is behind a symlink, which `uv`'s working directory resolves.
            let root = tmp.path().canonicalize().unwrap();
            let source = root.join("hw.py");
            let fake = fake_uv(
                &root,
                "[ \"$1\" = python ] && echo '/cache/env/bin/python3'; exit 0",
            );

            let _spawning = spawning();
            let python = notebook_python(&fake, &source).unwrap();

            let cwd = root.display().to_string();
            let source = source.display().to_string();
            assert_eq!(
                calls(&root),
                [
                    vec![&cwd, "sync", "--quiet", "--script", &source],
                    vec![&cwd, "python", "find", "--script", &source],
                ]
            );
            let command = python.command();
            let args: Vec<_> = command.get_args().collect();
            assert_eq!(command.get_program(), root.join("uv"));
            assert!(
                command
                    .get_envs()
                    .any(|(key, value)| key == "UV_PYTHON" && value.is_none())
            );
            assert_eq!(
                args,
                [
                    "run",
                    "--quiet",
                    "--active",
                    "--no-project",
                    "--python",
                    "/cache/env/bin/python3",
                    "--with-editable",
                    "/src/mogs",
                    "--",
                    "python",
                ]
            );
            assert!(
                command.get_envs().any(
                    |(key, value)| key == "VIRTUAL_ENV" && value == Some("/cache/env".as_ref())
                )
            );
        }

        #[test]
        fn failed_sync_shows_uv_stderr() {
            let tmp = tempfile::tempdir().unwrap();
            let fake = fake_uv(tmp.path(), "echo '  no-such-package  ' >&2; exit 1");

            let _spawning = spawning();
            let error = notebook_python(&fake, &tmp.path().join("hw.py")).unwrap_err();

            assert_eq!(
                error,
                "Requirements: `uv` could not sync the notebook's environment:\nno-such-package"
            );
        }
    }
}
