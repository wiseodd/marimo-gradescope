//! The command line of mogs.
//!
//! [`parse`] reads the `mogs` command line ([`Cli`]). `mogs init` ([`scaffold`]) and
//! `mogs edit` ([`edit`]) run here. Text that follows Python's rules, and the checks that live
//! in Python, come from the caller through [`Interpreter`], so this crate never imitates
//! CPython.

mod command;
pub mod edit;
pub mod scaffold;

use std::ffi::OsStr;
use std::io;
use std::path::{Path, PathBuf};

pub use command::{Cli, Command, Exit, parse};

/// What the commands ask of Python.
pub trait Interpreter {
    /// A Python exception.
    type Error;

    /// `mogs.config.check_name(name)`: the name, or a `ConfigError`.
    ///
    /// # Errors
    ///
    /// The `ConfigError` that names the rule `name` breaks.
    fn check_name(&self, name: &OsStr) -> Result<String, Self::Error>;
    /// `mogs.transform.python_literal(text)`.
    ///
    /// # Errors
    ///
    /// Any Python exception.
    fn literal(&self, text: &str) -> Result<String, Self::Error>;
    /// `shlex.quote(str(path))`.
    ///
    /// # Errors
    ///
    /// Any Python exception.
    fn quote(&self, path: &Path) -> Result<String, Self::Error>;
    /// `pathlib.Path(path)`, which shows `./a/` as `a`.
    ///
    /// # Errors
    ///
    /// Any Python exception.
    fn path(&self, path: &Path) -> Result<PathBuf, Self::Error>;
    /// `importlib.metadata.version("marimo")`.
    ///
    /// # Errors
    ///
    /// `PackageNotFoundError` if marimo is not installed.
    fn marimo_version(&self) -> Result<String, Self::Error>;
}

/// A system call that failed.
#[derive(Debug)]
pub struct OsError {
    /// The error.
    pub error: io::Error,
    /// The path that Python's error would name, if any.
    pub path: Option<PathBuf>,
    /// The kind of call, which decides how Python words the error on Windows.
    pub call: Call,
}

impl OsError {
    fn new(error: io::Error, path: Option<&Path>, call: Call) -> Self {
        Self {
            error,
            path: path.map(Path::to_path_buf),
            call,
        }
    }
}

/// The kind of system call that failed.
///
/// On Windows, Python shows a failed file open or write by its C runtime `errno`
/// (`[Errno 13]`), and a failed call to the Windows API by its Windows error code
/// (`[WinError 5]`). Elsewhere every error shows its `errno`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Call {
    /// Opening or writing a file, as `open()` does: by `errno`, with the path.
    File,
    /// Making or listing a folder, as `os.mkdir` and `os.scandir` do: by Windows error code on
    /// Windows, with the path.
    Folder,
    /// Starting a process, as `subprocess.Popen` does: by Windows error code and without the
    /// path on Windows.
    Spawn,
}
