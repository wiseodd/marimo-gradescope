//! The grading supervisor (`mogs-grader`) for the rest of the extension.
//!
//! Only glue: `PyHost` gives the grader Python's text, users, signals and logs; `autograde`
//! runs `mogs autograde` with the GIL released; `data_files` serves `mogs._analysis`; and
//! bundles and results cross to and from Python. Every [`GradeError`] becomes the Python
//! exception that v0.7 raised, with the same text.

#[cfg(unix)]
pub mod autograde;

use std::path::PathBuf;
use std::sync::Mutex;

use mogs_grader::{GradeError, GradingUser, Host, Level, PyText};
#[cfg(unix)]
use mogs_schema::Bundle;
use pyo3::exceptions::{PyKeyError, PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple};
#[cfg(unix)]
use serde::Serialize;

/// The logger of the supervisor's messages, as in v0.7.
const SANDBOX_LOGGER: &str = "mogs.grader.sandbox";

/// The data files to ship, by `PurePosixPath` relative to `root`, with folders expanded.
#[pyfunction]
pub fn data_files<'py>(
    py: Python<'py>,
    root: PathBuf,
    files: Vec<String>,
) -> PyResult<Bound<'py, PyDict>> {
    let host = PyHost::default();
    let found = host.finish(py, mogs_grader::files::data_files(&root, &files, &host))?;
    let posix = py.import("pathlib")?.getattr("PurePosixPath")?;
    let out = PyDict::new(py);

    for (rel, source) in found {
        out.set_item(posix.call1((rel,))?, source)?;
    }

    Ok(out)
}

/// A `mogs.grader.bundle.GradingBundle` as the Rust type, through its JSON.
#[cfg(unix)]
pub(crate) fn bundle_from_py(bundle: &Bound<'_, PyAny>) -> PyResult<Bundle> {
    bundle_from_dict(&bundle.call_method0("to_dict")?)
}

/// The fields of a bundle, as `GradingBundle.to_dict` gives them, as the Rust type. It sets
/// the numbers of `data` to 0.
///
/// The numbers go around JSON: the config accepts infinity (such as `budget=1e999`), which
/// Python's JSON writes as `Infinity` and serde_json rejects.
#[cfg(unix)]
pub(crate) fn bundle_from_dict(data: &Bound<'_, PyAny>) -> PyResult<Bundle> {
    let config = data.get_item("config")?;
    let timeout: f64 = config.get_item("timeout")?.extract()?;
    let budget: f64 = config.get_item("budget")?.extract()?;
    let mut points: Vec<f64> = Vec::new();
    config.set_item("timeout", 0.0)?;
    config.set_item("budget", 0.0)?;

    for question in config.get_item("questions")?.try_iter()? {
        let question = question?;
        points.push(question.get_item("points")?.extract()?);
        question.set_item("points", 0.0)?;
    }

    let text: String = data
        .py()
        .import("json")?
        .call_method1("dumps", (data,))?
        .extract()?;
    let mut read: Bundle =
        mogs_schema::decode(&text).map_err(|error| PyValueError::new_err(error.to_string()))?;
    read.config.timeout = timeout;
    read.config.budget = budget;

    for (question, points) in read.config.questions.iter_mut().zip(points) {
        question.points = points;
    }

    Ok(read)
}

/// `results` as Python values. Scores keep infinity and NaN, which JSON cannot carry.
#[cfg(unix)]
pub(crate) fn results_to_py<'py>(
    py: Python<'py>,
    results: &mogs_schema::Results,
) -> PyResult<Bound<'py, PyAny>> {
    let out = to_py(py, results)?;
    let rows = out.get_item("tests")?;

    for (at, row) in results.tests.iter().enumerate() {
        let shown = rows.get_item(at)?;
        shown.set_item("score", row.score)?;
        shown.set_item("max_score", row.max_score)?;
    }

    Ok(out)
}

/// A Rust value as plain Python values, through `json.loads`.
#[cfg(unix)]
pub(crate) fn to_py<'py, T: Serialize>(py: Python<'py>, value: &T) -> PyResult<Bound<'py, PyAny>> {
    let text = serde_json::to_string(value).expect("the value is plain JSON");

    py.import("json")?.call_method1("loads", (text,))
}

/// `OSError(errno, strerror, filename)`, which Python turns into the subclass for `errno`.
pub(crate) fn os_error(py: Python<'_>, error: &std::io::Error, path: Option<PathBuf>) -> PyErr {
    let Some(errno) = error.raw_os_error() else {
        return PyOSError::new_err(error.to_string());
    };

    let made = (|| {
        let strerror = py.import("os")?.call_method1("strerror", (errno,))?;
        let filename = path.map(|path| path.to_string_lossy().into_owned());
        let builtins = py.import("builtins")?;
        let error = match filename {
            Some(filename) => builtins
                .getattr("OSError")?
                .call1((errno, strerror, filename))?,
            None => builtins.getattr("OSError")?.call1((errno, strerror))?,
        };
        PyResult::Ok(PyErr::from_value(error))
    })();

    made.unwrap_or_else(|error| error)
}

/// The grader's view of Python: text, users, signals and logs.
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

    /// The result as Python sees it: the kept error first, then the grader's own.
    pub(crate) fn finish<T>(&self, py: Python<'_>, result: Result<T, GradeError>) -> PyResult<T> {
        let pending = self
            .pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take();

        match (pending, result) {
            (Some(error), _) => Err(error),
            (None, Ok(value)) => Ok(value),
            (None, Err(error)) => Err(to_py_err(py, error)),
        }
    }
}

/// The Python exception for `error`.
fn to_py_err(py: Python<'_>, error: GradeError) -> PyErr {
    match error {
        GradeError::Value(message) => PyValueError::new_err(message),
        GradeError::Key(key) => PyKeyError::new_err(key),
        GradeError::Os { error, path } => os_error(py, &error, path),
        GradeError::Host => PyRuntimeError::new_err("the grader stopped without an error"),
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
        self.call((), |py| py.check_signals());
        self.failed()
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
