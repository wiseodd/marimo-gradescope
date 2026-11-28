//! [`PyHost`]: Python for the Rust crates that work with the GIL released.
//!
//! `mogs-grader` asks it through `Host` and `PyText`, `mogs-pdf` through `Signals`, and
//! `mogs-build` through `Interpreter` (in `crate::build`). Each crate keeps its own trait, as
//! the PDF also runs on Windows and asks only for signals.

use std::sync::Mutex;

use mogs_grader::{GradeError, GradingUser, Host, Level, PyText};
#[cfg(unix)]
use pyo3::exceptions::PyKeyError;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyString, PyTuple};

#[cfg(unix)]
use crate::python::os_error;

/// The logger of the supervisor's messages, as in v0.7.
const SANDBOX_LOGGER: &str = "mogs.grader.sandbox";

/// Python as the grader, the build and the PDF see it: text, users, signals and logs.
///
/// Every call attaches to Python for its own duration, so it works with the GIL released. The
/// first Python error a call hits is kept, and `finish` raises it.
#[derive(Default)]
pub(crate) struct PyHost {
    pending: Mutex<Option<PyErr>>,
}

impl PyHost {
    /// Run `f` attached to Python; on error keep it and give `fallback`, which the caller
    /// discards once `finish` raises the error.
    fn call<T>(&self, fallback: T, f: impl FnOnce(Python<'_>) -> PyResult<T>) -> T {
        Python::attach(|py| match f(py) {
            Ok(value) => value,
            Err(error) => {
                let mut pending = self.pending.lock().unwrap_or_else(|e| e.into_inner());
                pending.get_or_insert(error);
                fallback
            }
        })
    }

    fn failed(&self) -> bool {
        self.pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .is_some()
    }

    /// `result` as Python sees it: the error a call kept comes first, then `result`'s own,
    /// made a Python exception by `error`.
    pub(crate) fn finish<T, E>(
        &self,
        result: Result<T, E>,
        error: impl FnOnce(E) -> PyErr,
    ) -> PyResult<T> {
        let kept = self
            .pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take();

        match kept {
            Some(kept) => Err(kept),
            None => result.map_err(error),
        }
    }

    /// The Python exception for `error`, or the one a call kept. Linux and macOS, as the
    /// build that asks for it.
    #[cfg(unix)]
    pub(crate) fn grade_error(&self, error: GradeError) -> PyErr {
        let made = Python::attach(|py| self.finish(Err::<(), _>(error), |e| grade_err(py, e)));

        made.expect_err("an error stays an error")
    }
}

/// What a stop without a Python error raises. Each stop keeps the error that caused it, so
/// this means a bug.
pub(crate) fn stopped() -> PyErr {
    PyRuntimeError::new_err(GradeError::Host.to_string())
}

/// The Python exception for `error`. Linux and macOS, as the grading that raises it.
#[cfg(unix)]
pub(crate) fn grade_err(py: Python<'_>, error: GradeError) -> PyErr {
    match error {
        GradeError::Value(message) => PyValueError::new_err(message),
        GradeError::Key(key) => PyKeyError::new_err(key),
        GradeError::Os { error, path } => os_error(py, &error, path.as_deref()),
        GradeError::Host => stopped(),
    }
}

impl PyText for PyHost {
    fn g(&self, x: f64) -> String {
        self.call(String::new(), |py| {
            let format = py.import("builtins")?.getattr("format")?;
            format.call1((x, "g"))?.extract()
        })
    }

    fn repr(&self, text: &str) -> String {
        self.call(String::new(), |py| {
            PyString::new(py, text).repr().map(|repr| repr.to_string())
        })
    }

    fn round(&self, x: f64, digits: i32) -> f64 {
        self.call(x, |py| {
            py.import("builtins")?
                .getattr("round")?
                .call1((x, digits))?
                .extract()
        })
    }

    fn cap(&self, text: &str) -> String {
        self.call(String::new(), |py| {
            py.import("mogs.runner")?
                .call_method1("cap", (text,))?
                .extract()
        })
    }

    fn casefold(&self, text: &str) -> String {
        self.call(String::new(), |py| {
            PyString::new(py, text).call_method0("casefold")?.extract()
        })
    }

    fn signal_name(&self, number: i32) -> String {
        self.call(String::new(), |py| {
            match py.import("signal")?.getattr("Signals")?.call1((number,)) {
                Ok(signal) => signal.getattr("name")?.extract(),
                Err(error) if error.is_instance_of::<PyValueError>(py) => Ok(number.to_string()),
                Err(error) => Err(error),
            }
        })
    }
}

impl Host for PyHost {
    fn user(&self, name: &str) -> Result<GradingUser, GradeError> {
        let found = self.call(None, |py| {
            let entry = py.import("pwd")?.call_method1("getpwnam", (name,))?;
            let uid = entry.getattr("pw_uid")?.extract()?;
            let gid = entry.getattr("pw_gid")?.extract()?;
            Ok(Some(GradingUser {
                name: name.into(),
                uid,
                gid,
            }))
        });

        found.ok_or(GradeError::Host)
    }

    fn interrupted(&self) -> bool {
        mogs_pdf::Signals::interrupted(self)
    }

    fn log(&self, level: Level, format: &str, args: &[&str]) {
        let level: i32 = match level {
            Level::Info => 20,
            Level::Warning => 30,
        };

        self.call((), |py| {
            let logger = py
                .import("logging")?
                .call_method1("getLogger", (SANDBOX_LOGGER,))?;
            let mut call: Vec<Bound<'_, PyAny>> = vec![
                level.into_pyobject(py)?.into_any(),
                PyString::new(py, format).into_any(),
            ];
            call.extend(args.iter().map(|arg| PyString::new(py, arg).into_any()));
            logger.call_method1("log", PyTuple::new(py, call)?)?;
            Ok(())
        });
    }
}

impl mogs_pdf::Signals for PyHost {
    fn interrupted(&self) -> bool {
        self.call((), |py| py.check_signals());
        self.failed()
    }
}
