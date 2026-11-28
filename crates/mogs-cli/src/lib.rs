//! The command line of mogs.
//!
//! [`parse`] reads the `mogs` command line ([`Cli`]), and [`scaffold`] runs `mogs init`. Text
//! that follows Python's rules, the checks that live in Python, and the file operations whose
//! errors Python words, come from the caller through [`Interpreter`], so this crate never
//! imitates CPython.

mod command;
pub mod diagnostic;
pub mod scaffold;

use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};

pub use command::{Cli, Command, Exit, parse};

/// What the commands ask of Python.
///
/// Every method may fail with any Python exception, such as the `OSError` of a file
/// operation.
pub trait Interpreter {
    /// A Python exception.
    type Error;

    /// `mogs.config.check_name(Path(folder).resolve().name)`, as a file name: it may hold bytes
    /// that are not UTF-8.
    ///
    /// # Errors
    ///
    /// The `ConfigError` that names the rule the name breaks.
    fn check_name(&self, folder: &Path) -> Result<OsString, Self::Error>;
    /// `mogs.transform.python_literal(text)`, which escapes what is not printable.
    ///
    /// # Errors
    ///
    /// Any Python exception.
    fn literal(&self, text: &OsStr) -> Result<String, Self::Error>;
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
    /// `Path(path).exists()`.
    ///
    /// # Errors
    ///
    /// The `OSError` that pathlib does not take as "no", such as EACCES.
    fn exists(&self, path: &Path) -> Result<bool, Self::Error>;
    /// `Path(path).is_dir()`.
    ///
    /// # Errors
    ///
    /// The `OSError` that pathlib does not take as "no", such as EACCES.
    fn is_dir(&self, path: &Path) -> Result<bool, Self::Error>;
    /// `any(Path(folder).iterdir())`.
    ///
    /// # Errors
    ///
    /// The `OSError` if the folder cannot be listed.
    fn has_entries(&self, folder: &Path) -> Result<bool, Self::Error>;
    /// `Path(folder).mkdir(parents=True, exist_ok=True)`.
    ///
    /// # Errors
    ///
    /// The `OSError` if the folder cannot be made.
    fn make_folder(&self, folder: &Path) -> Result<(), Self::Error>;
    /// `Path(path).write_text(text, encoding="utf-8")`.
    ///
    /// # Errors
    ///
    /// The `OSError` if the file cannot be written.
    fn write_text(&self, path: &Path, text: &str) -> Result<(), Self::Error>;
}
