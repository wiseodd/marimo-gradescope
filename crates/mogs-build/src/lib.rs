//! `mogs build`.
//!
//! `build` writes the student notebook and the Gradescope autograder zip, after running every
//! test of the solved copy in the notebook's own environment (`environment`). `report` turns
//! the result into the text the command prints.
//!
//! Python reads the source notebook and computes what follows from it: the student notebook,
//! the bundle, the requirements and the warnings. That, text with Python's rules and Python's
//! exceptions come from the caller through `Interpreter`. Text that may hold a path is an
//! `OsString`, since a path may hold bytes that are not UTF-8.

pub mod build;
pub mod environment;
mod process;
pub mod report;
pub mod templates;
pub mod zipout;

use std::collections::HashMap;
use std::ffi::OsString;
use std::io;
use std::path::{Path, PathBuf};

use indexmap::IndexMap;
use mogs_grader::{GradeError, Host};
use mogs_schema::Bundle;

/// The output folder next to the source notebook.
pub const DIST: &str = "dist";
/// The parts of `DIST` a build replaces.
pub const PARTS: [&str; 2] = ["student", "autograder"];

/// Remove what a failed build of `folder` leaves, ignoring errors, as `shutil.rmtree(...,
/// ignore_errors=True)`.
pub fn remove_output(folder: &Path) {
    for part in PARTS {
        let _ = std::fs::remove_dir_all(folder.join(DIST).join(part));
    }
}

/// The Python exception classes that Rust raises.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Class {
    /// `mogs._analysis.BuildError`.
    Build,
    /// `mogs.config.ConfigError`.
    Config,
    /// `mogs.requirements.RequirementsError`.
    Requirements,
}

/// What `build` asks of Python.
///
/// Every method may fail with any Python exception.
pub trait Interpreter {
    /// A Python exception.
    type Error;
    /// A source notebook that Python has read and checked.
    type Assignment;

    /// The grader's view of Python, which also gives text with Python's rules.
    fn host(&self) -> &dyn Host;
    /// The Python exception for `error`. An error that `host` kept comes first.
    fn grade_error(&self, error: GradeError) -> Self::Error;
    /// `class(message)`.
    fn raise(&self, class: Class, message: OsString) -> Self::Error;

    /// The `OSError` of `error` from an operation on `path`.
    fn os(&self, error: io::Error, path: &Path) -> Self::Error {
        self.grade_error(GradeError::os(error, path))
    }

    /// The `OSError` of `error`.
    fn io(&self, error: io::Error) -> Self::Error {
        self.grade_error(GradeError::from(error))
    }

    /// `mogs._analysis.read_assignment(folder)`.
    ///
    /// # Errors
    ///
    /// The `BuildError`, `ConfigError` or `SourceError` of a missing or invalid notebook.
    fn read(&self, folder: &Path) -> Result<Self::Assignment, Self::Error>;
    /// `mogs._analysis.build_inputs(assignment)`.
    ///
    /// # Errors
    ///
    /// The `ConfigError`, `SourceError` or `RequirementsError` of the notebook.
    fn build_inputs(&self, assignment: &Self::Assignment) -> Result<BuildInputs, Self::Error>;
    /// `mogs._analysis.installed_tool_args()`: the `uv pip install` arguments that install
    /// the running mogs.
    ///
    /// # Errors
    ///
    /// The `RequirementsError` of a `direct_url.json` that names no known source.
    fn tool_install_args(&self) -> Result<Vec<String>, Self::Error>;

    /// `shutil.which(name)`.
    ///
    /// # Errors
    ///
    /// Any Python exception.
    fn which(&self, name: &str) -> Result<Option<PathBuf>, Self::Error>;
    /// `pathlib.Path(path)`, which shows `./a/` as `a`.
    ///
    /// # Errors
    ///
    /// Any Python exception.
    fn path(&self, path: &Path) -> Result<PathBuf, Self::Error>;
    /// `datetime.now().strftime(format)`.
    ///
    /// # Errors
    ///
    /// Any Python exception.
    fn now(&self, format: &str) -> Result<String, Self::Error>;
    /// `mogs._analysis.decoded(*streams)`: a command's output as `subprocess.run(...,
    /// text=True)` reads it, joined and stripped.
    ///
    /// # Errors
    ///
    /// The `UnicodeDecodeError` of output that is not in the locale's encoding.
    fn decoded(&self, streams: &[&[u8]]) -> Result<String, Self::Error>;
    /// `text.strip()`.
    ///
    /// # Errors
    ///
    /// Any Python exception.
    fn strip(&self, text: &str) -> Result<String, Self::Error>;
    /// `sum(values)`, which rounds as Python does.
    ///
    /// # Errors
    ///
    /// Any Python exception.
    fn sum(&self, values: &[f64]) -> Result<f64, Self::Error>;
    /// `data` compressed as `zipfile` does for `ZIP_DEFLATED`: raw deflate at the default
    /// level, with Python's zlib.
    ///
    /// # Errors
    ///
    /// Any Python exception.
    fn deflate(&self, data: &[u8]) -> Result<Vec<u8>, Self::Error>;
}

/// What a build needs from the source notebook.
#[derive(Clone, Debug, PartialEq)]
pub struct BuildInputs {
    /// The source notebook, whose own environment runs the check.
    pub source: PathBuf,
    /// The grading bundle.
    pub bundle: Bundle,
    /// The bundle as `bundle.json`.
    pub bundle_json: String,
    /// Data files, by path relative to the notebook, with their sources.
    pub data: IndexMap<String, PathBuf>,
    /// The student notebook.
    pub student: String,
    /// The solved copy: the student notebook with the solutions kept.
    pub solved: String,
    /// Public test files, by path relative to the notebook.
    pub tests: Vec<(String, String)>,
    /// Gradescope's `requirements.txt`.
    pub requirements: String,
    /// Things in the notebook and its requirements the instructor should look at.
    pub warnings: Vec<String>,
    /// The first line of each prompt cell of each manual question, by question id.
    pub prompts: HashMap<String, Vec<String>>,
}

/// One question of the config, with its tests counted.
#[derive(Clone, Debug, PartialEq)]
pub struct Question {
    /// Question id.
    pub id: String,
    /// Question title.
    pub title: String,
    /// Maximum score.
    pub points: f64,
    /// Number of public tests.
    pub public: usize,
    /// Number of hidden tests.
    pub hidden: usize,
    /// Whether it is graded by hand, from the PDF.
    pub manual: bool,
    /// First line of each prompt cell of a manual question.
    pub prompts: Vec<String>,
}

/// Fail with the host's error, such as `KeyboardInterrupt` after Ctrl+C, if it has one.
fn interrupted<I: Interpreter>(i: &I) -> Result<(), I::Error> {
    if i.host().interrupted() {
        return Err(i.grade_error(GradeError::Host));
    }

    Ok(())
}

/// `parts` joined, as one text that may hold paths.
fn text(parts: &[&std::ffi::OsStr]) -> OsString {
    parts.iter().copied().collect()
}

#[cfg(test)]
pub(crate) mod fake {
    //! An [`Interpreter`] for tests: plain Rust stand-ins for the inputs the tests use.

    use std::ffi::OsString;
    use std::path::{Path, PathBuf};

    use mogs_grader::{GradeError, GradingUser, Host, Level, PyText};

    use super::{BuildInputs, Class, Interpreter};

    #[cfg(unix)]
    pub use self::scripts::{script, spawning};

    #[cfg(unix)]
    mod scripts {
        use std::fs;
        use std::os::unix::fs::PermissionsExt;
        use std::path::Path;
        use std::sync::{RwLock, RwLockReadGuard};

        /// Held to write a script, and shared to start processes. A process started while a
        /// test writes a script keeps it open for writing until its `exec`, and running the
        /// script then fails with "Text file busy".
        static SCRIPTS: RwLock<()> = RwLock::new(());

        /// Write the executable shell script `path` with `body`.
        pub fn script(path: &Path, body: &str) {
            let _writing = SCRIPTS.write().unwrap_or_else(|e| e.into_inner());
            fs::write(path, format!("#!/bin/sh\n{body}\n")).unwrap();
            fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
        }

        /// Hold while a test starts processes.
        pub fn spawning() -> RwLockReadGuard<'static, ()> {
            SCRIPTS.read().unwrap_or_else(|e| e.into_inner())
        }
    }

    /// Formats like Python for the values the tests pass. Errors are their text, after the
    /// class name.
    #[derive(Default)]
    pub struct Fake {
        /// What `which` finds.
        pub found: Option<PathBuf>,
        /// What `tool_install_args` gives.
        pub tool: Vec<String>,
    }

    impl PyText for Fake {
        fn g(&self, x: f64) -> String {
            format!("{x}")
        }

        fn repr(&self, text: &str) -> String {
            format!("'{text}'")
        }

        fn round(&self, x: f64, _digits: i32) -> f64 {
            x
        }

        fn cap(&self, text: &str) -> String {
            text.into()
        }

        fn casefold(&self, text: &str) -> String {
            text.to_lowercase()
        }

        fn signal_name(&self, number: i32) -> String {
            number.to_string()
        }
    }

    impl Host for Fake {
        fn user(&self, name: &str) -> Result<GradingUser, GradeError> {
            Err(GradeError::Key(name.into()))
        }

        fn interrupted(&self) -> bool {
            false
        }

        fn log(&self, _level: Level, _format: &str, _args: &[&str]) {}
    }

    impl Interpreter for Fake {
        type Error = String;
        type Assignment = ();

        fn host(&self) -> &dyn Host {
            self
        }

        fn grade_error(&self, error: GradeError) -> String {
            format!("OSError: {error}")
        }

        fn raise(&self, class: Class, message: OsString) -> String {
            format!("{class:?}: {}", message.to_string_lossy())
        }

        fn read(&self, _folder: &Path) -> Result<(), String> {
            unimplemented!()
        }

        fn build_inputs(&self, _assignment: &()) -> Result<BuildInputs, String> {
            unimplemented!()
        }

        fn tool_install_args(&self) -> Result<Vec<String>, String> {
            Ok(self.tool.clone())
        }

        fn which(&self, _name: &str) -> Result<Option<PathBuf>, String> {
            Ok(self.found.clone())
        }

        fn path(&self, path: &Path) -> Result<PathBuf, String> {
            Ok(path.components().collect())
        }

        fn now(&self, _format: &str) -> Result<String, String> {
            Ok("2026_09_30T12_30_41_890583".into())
        }

        fn decoded(&self, streams: &[&[u8]]) -> Result<String, String> {
            let text: String = streams.iter().map(|s| String::from_utf8_lossy(s)).collect();
            Ok(text.trim().into())
        }

        fn strip(&self, text: &str) -> Result<String, String> {
            Ok(text.trim().into())
        }

        fn sum(&self, values: &[f64]) -> Result<f64, String> {
            Ok(values.iter().sum())
        }

        fn deflate(&self, data: &[u8]) -> Result<Vec<u8>, String> {
            Ok(data.to_vec())
        }
    }
}
