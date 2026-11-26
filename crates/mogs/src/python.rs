//! The Python interpreter of the environment `mogs` is installed in.

use std::fmt;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;

/// Interpreter names to look for, in order.
#[cfg(windows)]
const NAMES: &[&str] = &["python.exe"];
#[cfg(not(windows))]
const NAMES: &[&str] = &["python3", "python"];

/// The scripts folder of a uv tool environment.
#[cfg(windows)]
const TOOL_BIN: &str = "Scripts";
#[cfg(not(windows))]
const TOOL_BIN: &str = "bin";

/// Why no interpreter was found.
#[derive(Debug)]
pub enum PythonNotFound {
    /// The path of the running executable could not be resolved.
    Executable(io::Error),
    /// Neither the executable's folder nor the uv tool environment has an interpreter.
    Missing {
        /// The folder of the executable.
        beside: PathBuf,
        /// The scripts folder of the `mogs` uv tool environment, or why it is unknown.
        tool: Result<PathBuf, String>,
    },
}

impl fmt::Display for PythonNotFound {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Executable(error) => {
                write!(f, "Cannot find the path of the mogs executable: {error}")
            }
            Self::Missing { beside, tool } => {
                write!(
                    f,
                    "No Python interpreter ({}) in {}, the folder of the mogs executable",
                    NAMES.join(", "),
                    beside.display()
                )?;

                match tool {
                    Ok(folder) => write!(
                        f,
                        ", or in {}, the uv tool environment of mogs.",
                        folder.display()
                    ),
                    Err(reason) => write!(f, ". The uv tool environment is unknown: {reason}"),
                }
            }
        }
    }
}

/// The interpreter of the environment the running executable is installed in.
///
/// Looks in the executable's folder, symlinks resolved: the scripts folder of a virtual
/// environment, or of a uv tool environment on Unix. Then looks in the scripts folder of the
/// `mogs` environment under `uv tool dir`, since uv copies tool executables on Windows.
pub fn interpreter() -> Result<PathBuf, PythonNotFound> {
    interpreter_from(&executable_folder()?, uv_tool_dir)
}

/// The folder of the running executable, symlinks resolved.
///
/// A plain path: on Windows, `dunce` drops the `\\?\` prefix of `canonicalize`, since the
/// interpreter path becomes `sys.executable` for Python and its children.
fn executable_folder() -> Result<PathBuf, PythonNotFound> {
    let executable = std::env::current_exe()
        .and_then(dunce::canonicalize)
        .map_err(PythonNotFound::Executable)?;

    match executable.parent() {
        Some(folder) => Ok(folder.to_path_buf()),
        None => {
            let error = io::Error::other(format!("{} has no parent folder", executable.display()));
            Err(PythonNotFound::Executable(error))
        }
    }
}

/// The interpreter in `beside`, else in the `mogs` tool environment under `uv_tool_dir()`.
fn interpreter_from(
    beside: &Path,
    uv_tool_dir: impl FnOnce() -> Result<PathBuf, String>,
) -> Result<PathBuf, PythonNotFound> {
    if let Some(python) = interpreter_in(beside) {
        return Ok(python);
    }

    // On Windows `uv tool install` copies the executable out of its environment. The tool
    // environment found here may hold a different mogs version than this executable.
    let tool = uv_tool_dir().map(|dir| dir.join("mogs").join(TOOL_BIN));

    if let Ok(folder) = &tool
        && let Some(python) = interpreter_in(folder)
    {
        return Ok(python);
    }

    Err(PythonNotFound::Missing {
        beside: beside.to_path_buf(),
        tool,
    })
}

/// The first interpreter in `folder`.
fn interpreter_in(folder: &Path) -> Option<PathBuf> {
    NAMES
        .iter()
        .map(|name| folder.join(name))
        .find(|path| path.is_file())
}

/// The folder of uv's tool environments, from `uv tool dir`.
fn uv_tool_dir() -> Result<PathBuf, String> {
    let output = match Command::new("uv").args(["tool", "dir"]).output() {
        Ok(output) => output,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Err("`uv` is not on PATH.".to_string());
        }
        Err(error) => return Err(format!("cannot run `uv tool dir`: {error}")),
    };

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "`uv tool dir` failed ({}): {}",
            output.status,
            stderr.trim()
        ));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);

    Ok(PathBuf::from(stdout.trim()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::{TempDir, tempdir};

    /// A file standing in for an interpreter in `folder`.
    fn fake_python(folder: &Path) -> PathBuf {
        let python = folder.join(NAMES[0]);
        std::fs::create_dir_all(folder).unwrap();
        std::fs::write(&python, "").unwrap();

        python
    }

    /// Two empty folders: the executable's and uv's tool folder.
    fn folders() -> (TempDir, TempDir) {
        (tempdir().unwrap(), tempdir().unwrap())
    }

    #[test]
    fn executable_folder_is_a_plain_path() {
        let folder = executable_folder().unwrap();

        assert!(folder.is_absolute());
        assert!(
            !folder.to_string_lossy().starts_with(r"\\?\"),
            "{}",
            folder.display()
        );
    }

    #[test]
    fn finds_sibling_interpreter() {
        let beside = tempdir().unwrap();
        let python = fake_python(beside.path());

        let found = interpreter_from(beside.path(), || panic!("uv must not be asked"));

        assert_eq!(found.unwrap(), python);
    }

    #[test]
    fn falls_back_to_uv_tool_environment() {
        let (beside, tools) = folders();
        let python = fake_python(&tools.path().join("mogs").join(TOOL_BIN));

        let found = interpreter_from(beside.path(), || Ok(tools.path().to_path_buf()));

        assert_eq!(found.unwrap(), python);
    }

    #[test]
    fn errors_without_one() {
        let (beside, tools) = folders();
        let bin = tools.path().join("mogs").join(TOOL_BIN);

        let error = interpreter_from(beside.path(), || Ok(tools.path().to_path_buf()));
        let message = error.unwrap_err().to_string();

        assert!(
            message.contains(&beside.path().display().to_string()),
            "{message}"
        );
        assert!(message.contains(&bin.display().to_string()), "{message}");
    }

    #[test]
    fn errors_when_uv_is_unavailable() {
        let beside = tempdir().unwrap();

        let error = interpreter_from(beside.path(), || Err("`uv` is not on PATH".to_string()));
        let message = error.unwrap_err().to_string();

        assert!(
            message.contains(&beside.path().display().to_string()),
            "{message}"
        );
        assert!(message.contains("`uv` is not on PATH"), "{message}");
    }
}
