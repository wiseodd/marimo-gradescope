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
    let executable = std::env::current_exe()
        .and_then(|path| path.canonicalize())
        .map_err(PythonNotFound::Executable)?;

    let Some(folder) = executable.parent() else {
        let error = io::Error::other(format!("{} has no parent folder", executable.display()));
        return Err(PythonNotFound::Executable(error));
    };

    interpreter_from(folder, uv_tool_dir)
}

/// The interpreter in `beside`, else in the `mogs` tool environment under `uv_tool_dir()`.
fn interpreter_from(
    beside: &Path,
    uv_tool_dir: impl FnOnce() -> Result<PathBuf, String>,
) -> Result<PathBuf, PythonNotFound> {
    if let Some(python) = interpreter_in(beside) {
        return Ok(python);
    }

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

    /// A new empty folder for one test.
    fn empty_folder(name: &str) -> PathBuf {
        let folder = std::env::temp_dir().join(format!("mogs-{name}-{}", std::process::id()));

        let _ = std::fs::remove_dir_all(&folder);
        std::fs::create_dir_all(&folder).unwrap();

        folder
    }

    /// A folder with an empty file standing in for an interpreter.
    fn folder_with_python(name: &str) -> (PathBuf, PathBuf) {
        let folder = empty_folder(name);
        let python = folder.join(NAMES[0]);
        std::fs::write(&python, "").unwrap();

        (folder, python)
    }

    #[test]
    fn finds_sibling_interpreter() {
        let (folder, python) = folder_with_python("finds-sibling-interpreter");

        let found = interpreter_from(&folder, || panic!("uv must not be asked"));

        assert_eq!(found.unwrap(), python);
    }

    #[test]
    fn falls_back_to_uv_tool_environment() {
        let beside = empty_folder("falls-back-beside");
        let tools = empty_folder("falls-back-tools");
        let bin = tools.join("mogs").join(TOOL_BIN);
        std::fs::create_dir_all(&bin).unwrap();
        let python = bin.join(NAMES[0]);
        std::fs::write(&python, "").unwrap();

        let found = interpreter_from(&beside, || Ok(tools.clone()));

        assert_eq!(found.unwrap(), python);
    }

    #[test]
    fn errors_without_one() {
        let beside = empty_folder("errors-without-one-beside");
        let tools = empty_folder("errors-without-one-tools");
        let bin = tools.join("mogs").join(TOOL_BIN);

        let error = interpreter_from(&beside, || Ok(tools.clone())).unwrap_err();
        let message = error.to_string();

        assert!(message.contains(&beside.display().to_string()), "{message}");
        assert!(message.contains(&bin.display().to_string()), "{message}");
    }

    #[test]
    fn errors_when_uv_is_unavailable() {
        let beside = empty_folder("errors-when-uv-is-unavailable");

        let error = interpreter_from(&beside, || Err("`uv` is not on PATH".to_string()));
        let message = error.unwrap_err().to_string();

        assert!(message.contains(&beside.display().to_string()), "{message}");
        assert!(message.contains("`uv` is not on PATH"), "{message}");
    }
}
