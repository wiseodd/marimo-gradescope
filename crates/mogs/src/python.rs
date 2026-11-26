//! The Python interpreter installed next to the `mogs` executable.

use std::fmt;
use std::io;
use std::path::{Path, PathBuf};

/// Interpreter names to look for, in order.
#[cfg(windows)]
const NAMES: &[&str] = &["python.exe"];
#[cfg(not(windows))]
const NAMES: &[&str] = &["python3", "python"];

/// Why no interpreter was found.
#[derive(Debug)]
pub enum PythonNotFound {
    /// The path of the running executable could not be resolved.
    Executable(io::Error),
    /// The executable's folder has no interpreter.
    Missing(PathBuf),
}

impl fmt::Display for PythonNotFound {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Executable(error) => {
                write!(f, "Cannot find the path of the mogs executable: {error}")
            }
            Self::Missing(folder) => write!(
                f,
                "No Python interpreter ({}) in {}, the folder of the mogs executable.",
                NAMES.join(", "),
                folder.display()
            ),
        }
    }
}

/// The interpreter in the folder of the running executable, symlinks resolved.
///
/// That folder is the `bin` (or `Scripts`) folder of the environment `mogs` is installed in,
/// such as a uv tool environment or a virtual environment.
pub fn interpreter() -> Result<PathBuf, PythonNotFound> {
    let executable = std::env::current_exe()
        .and_then(|path| path.canonicalize())
        .map_err(PythonNotFound::Executable)?;

    let folder = executable
        .parent()
        .ok_or_else(|| PythonNotFound::Missing(executable.clone()))?;

    interpreter_in(folder)
}

/// The first interpreter in `folder`.
fn interpreter_in(folder: &Path) -> Result<PathBuf, PythonNotFound> {
    NAMES
        .iter()
        .map(|name| folder.join(name))
        .find(|path| path.is_file())
        .ok_or_else(|| PythonNotFound::Missing(folder.to_path_buf()))
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

    #[test]
    fn finds_sibling_interpreter() {
        let folder = empty_folder("finds-sibling-interpreter");
        let python = folder.join(NAMES[0]);
        std::fs::write(&python, "").unwrap();

        assert_eq!(interpreter_in(&folder).unwrap(), python);
    }

    #[test]
    fn errors_without_one() {
        let folder = empty_folder("errors-without-one");

        let error = interpreter_in(&folder).unwrap_err();

        assert!(matches!(&error, PythonNotFound::Missing(missing) if *missing == folder));
        assert!(error.to_string().contains(&folder.display().to_string()));
    }
}
