//! The grading supervisor of mogs.
//!
//! It grades a submission in Python child processes (`python -m mogs.grader.child <fd>`) that
//! it starts, watches and kills ([`sandbox`], [`process`]), and turns their outcomes into
//! Gradescope's `results.json` ([`results`]). Text that follows Python's rules, such as
//! `repr` and the `g` number format, comes from the caller through [`PyText`], so this crate
//! never imitates CPython. Grading needs Linux or macOS; finding the upload and building the
//! results work everywhere.

#[cfg(unix)]
pub mod autograde;
pub mod files;
#[cfg(unix)]
pub mod process;
pub mod results;
#[cfg(test)]
mod results_tests;
#[cfg(unix)]
pub mod sandbox;
#[cfg(all(test, unix))]
mod sandbox_tests;
pub mod submission;
#[cfg(unix)]
mod watch;

use std::ffi::OsString;
use std::fmt;
use std::io;
use std::path::PathBuf;

/// The user that runs student code on Gradescope, which `setup.sh` creates.
pub const STUDENT_USER: &str = "mogs-student";

/// Text computed by Python, with Python's rules.
pub trait PyText {
    /// `format(x, "g")`.
    fn g(&self, x: f64) -> String;
    /// `repr(text)`.
    fn repr(&self, text: &str) -> String;
    /// `round(x, digits)`.
    fn round(&self, x: f64, digits: i32) -> f64;
    /// `mogs.runner.cap(text)`: at most 25 lines and 4096 bytes.
    fn cap(&self, text: &str) -> String;
    /// `text.casefold()`.
    fn casefold(&self, text: &str) -> String;
    /// `signal.Signals(number).name`, or the number when it names no signal.
    fn signal_name(&self, number: i32) -> String;
}

/// What the grader asks of its caller besides text: users, interrupts and logs.
///
/// A call that fails keeps its error for the caller, which then returns
/// [`GradeError::Host`].
pub trait Host: PyText + Sync {
    /// The password entry of `name`.
    ///
    /// # Errors
    ///
    /// [`GradeError::Host`] if there is no such user.
    fn user(&self, name: &str) -> Result<GradingUser, GradeError>;
    /// Whether the run must stop now, as after Ctrl+C.
    fn interrupted(&self) -> bool;
    /// Log a message, `%s` placeholders filled from `args` as `logging` does.
    fn log(&self, level: Level, format: &str, args: &[&str]);
}

/// Severity of a log message.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    /// `logging.INFO`.
    Info,
    /// `logging.WARNING`.
    Warning,
}

/// A user to run student code as.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GradingUser {
    /// User name.
    pub name: String,
    /// User id.
    pub uid: u32,
    /// Primary group id.
    pub gid: u32,
}

/// Why grading or building the results failed. Each variant stands for a Python exception.
#[derive(Debug)]
pub enum GradeError {
    /// `ValueError` with this text, which may hold a path that is not UTF-8.
    Value(OsString),
    /// `KeyError` of this key.
    Key(String),
    /// `OSError` (a subclass by `errno`) from an operation on `path`.
    Os {
        /// The error.
        error: io::Error,
        /// The file the operation was on, if any.
        path: Option<PathBuf>,
    },
    /// The host failed or asked to stop, and keeps the error.
    Host,
}

impl GradeError {
    /// An `OSError` from an operation on `path`.
    pub fn os(error: io::Error, path: impl Into<PathBuf>) -> Self {
        GradeError::Os {
            error,
            path: Some(path.into()),
        }
    }
}

impl From<io::Error> for GradeError {
    fn from(error: io::Error) -> Self {
        GradeError::Os { error, path: None }
    }
}

#[cfg(unix)]
impl From<rustix::io::Errno> for GradeError {
    fn from(errno: rustix::io::Errno) -> Self {
        GradeError::from(io::Error::from(errno))
    }
}

impl fmt::Display for GradeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            GradeError::Value(message) => write!(f, "{}", message.to_string_lossy()),
            GradeError::Key(key) => write!(f, "{key}"),
            GradeError::Os { error, path: None } => write!(f, "{error}"),
            GradeError::Os {
                error,
                path: Some(path),
            } => write!(f, "{error}: {}", path.display()),
            GradeError::Host => write!(f, "the work stopped without a Python error"),
        }
    }
}

impl std::error::Error for GradeError {}

#[cfg(test)]
pub(crate) mod fake {
    //! A [`Host`] for tests: plain Rust stand-ins for the few inputs the tests use.

    use std::sync::Mutex;
    #[cfg(unix)]
    use std::sync::{RwLock, RwLockReadGuard, RwLockWriteGuard};

    use super::{GradeError, GradingUser, Host, Level, PyText};

    /// Held to write a script that a test then runs, and shared to start processes. A process
    /// started while a test writes a script keeps it open for writing until its `exec`, and
    /// running the script then fails with "Text file busy".
    #[cfg(unix)]
    static SCRIPTS: RwLock<()> = RwLock::new(());

    /// Hold while writing a script that a test runs.
    #[cfg(unix)]
    pub fn writing_script() -> RwLockWriteGuard<'static, ()> {
        SCRIPTS.write().unwrap_or_else(|e| e.into_inner())
    }

    /// Hold while a test starts processes.
    #[cfg(unix)]
    pub fn spawning() -> RwLockReadGuard<'static, ()> {
        SCRIPTS.read().unwrap_or_else(|e| e.into_inner())
    }

    /// Formats like Python for the values the tests pass, and records log messages.
    #[derive(Default)]
    pub struct FakeHost {
        pub logs: Mutex<Vec<(Level, String)>>,
    }

    impl PyText for FakeHost {
        fn g(&self, x: f64) -> String {
            format!("{x}")
        }

        fn repr(&self, text: &str) -> String {
            format!("'{text}'")
        }

        fn round(&self, x: f64, digits: i32) -> f64 {
            let scale = 10f64.powi(digits);
            (x * scale).round() / scale
        }

        fn cap(&self, text: &str) -> String {
            text.to_string()
        }

        fn casefold(&self, text: &str) -> String {
            text.to_lowercase()
        }

        fn signal_name(&self, number: i32) -> String {
            match number {
                9 => "SIGKILL".into(),
                11 => "SIGSEGV".into(),
                _ => number.to_string(),
            }
        }
    }

    impl Host for FakeHost {
        /// `root`, and `me` for the user running the tests; no one else.
        #[cfg(unix)]
        fn user(&self, name: &str) -> Result<GradingUser, GradeError> {
            let (uid, gid) = match name {
                "root" => (0, 0),
                "me" => (
                    rustix::process::getuid().as_raw(),
                    rustix::process::getgid().as_raw(),
                ),
                _ => return Err(GradeError::Key(name.into())),
            };

            Ok(GradingUser {
                name: name.into(),
                uid,
                gid,
            })
        }

        #[cfg(not(unix))]
        fn user(&self, name: &str) -> Result<GradingUser, GradeError> {
            Err(GradeError::Key(name.into()))
        }

        fn interrupted(&self) -> bool {
            false
        }

        fn log(&self, level: Level, format: &str, args: &[&str]) {
            let mut message = format.to_string();

            for arg in args {
                message = message.replacen("%s", arg, 1);
            }

            self.logs.lock().unwrap().push((level, message));
        }
    }
}
