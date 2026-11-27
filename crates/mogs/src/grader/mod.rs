//! The grading supervisor (`mogs-grader`) as Python functions.
//!
//! Only glue: values cross as JSON or as the Python objects mogs already has, grading runs
//! with the GIL released, and every [`GradeError`] becomes the Python exception that v0.7
//! raised, with the same text.

#[cfg(unix)]
pub mod autograde;

use std::path::PathBuf;
use std::sync::Mutex;

use indexmap::IndexMap;
use mogs_grader::{GradeError, GradingUser, Host, Level, PyText};
use mogs_schema::{Bundle, Outcome};
use pyo3::exceptions::{PyKeyError, PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple};
use serde::Serialize;
use serde::de::DeserializeOwned;

/// The logger of the supervisor's messages, as in v0.7.
const SANDBOX_LOGGER: &str = "mogs.grader.sandbox";

/// The files found in an upload.
#[pyclass(frozen, eq, module = "mogs._native")]
#[derive(PartialEq)]
pub struct Submission {
    /// The notebook to grade, or `None` when there is none or it is ambiguous.
    #[pyo3(get)]
    py: Option<PathBuf>,
    /// The written answers, or `None`.
    #[pyo3(get)]
    answers: Option<PathBuf>,
    /// Why `py` is `None`, ready to show the student.
    #[pyo3(get)]
    error: Option<String>,
}

#[pymethods]
impl Submission {
    /// As the v0.7 dataclass shows it: each field as Python's `repr`.
    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        let py_repr = self.py.as_ref().into_pyobject(py)?.repr()?;
        let answers = self.answers.as_ref().into_pyobject(py)?.repr()?;
        let error = self.error.as_deref().into_pyobject(py)?.repr()?;

        Ok(format!(
            "Submission(py={py_repr}, answers={answers}, error={error})"
        ))
    }
}

/// Find the notebook and the written answers anywhere under `sub_dir`.
#[pyfunction]
#[pyo3(signature = (sub_dir, notebook, name=None))]
pub fn find_submission(
    py: Python<'_>,
    sub_dir: PathBuf,
    notebook: &str,
    name: Option<&str>,
) -> PyResult<Submission> {
    let host = PyHost::default();
    let found = mogs_grader::submission::find_submission(&sub_dir, notebook, name, &host);

    host.finish(py, Ok(found)).map(|found| Submission {
        py: found.py,
        answers: found.answers,
        error: found.error,
    })
}

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

/// Gradescope's `results.json` content for the outcomes of `bundle`'s tests.
#[pyfunction]
pub fn build_results<'py>(
    py: Python<'py>,
    bundle: &Bound<'py, PyAny>,
    outcomes: &Bound<'py, PyAny>,
    notices: Vec<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let bundle = bundle_from_py(bundle)?;
    let outcomes = outcomes_from_py(outcomes)?;
    let host = PyHost::default();
    let built = mogs_grader::results::build_results(&bundle, &outcomes, &notices, &host);

    to_py(py, &host.finish(py, built)?)
}

/// Results for a grader crash, naming only the class of `error`.
#[pyfunction]
pub fn failure_results<'py>(
    py: Python<'py>,
    bundle: Option<&Bound<'py, PyAny>>,
    error: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let bundle = bundle.map(bundle_from_py).transpose()?;
    let name = error.get_type().name()?.to_string();

    to_py(
        py,
        &mogs_grader::results::failure_results(bundle.as_ref(), &name),
    )
}

/// A `mogs.grader.bundle.GradingBundle` as the Rust type, through its JSON.
pub(crate) fn bundle_from_py(bundle: &Bound<'_, PyAny>) -> PyResult<Bundle> {
    let text: String = bundle.call_method0("to_json")?.extract()?;

    mogs_schema::decode(&text).map_err(|error| PyValueError::new_err(error.to_string()))
}

/// A mapping of names to `mogs.runner.Outcome`s as Rust outcomes, through their JSON.
fn outcomes_from_py(outcomes: &Bound<'_, PyAny>) -> PyResult<IndexMap<String, Outcome>> {
    let py = outcomes.py();
    let dicts = PyDict::new(py);

    for item in outcomes.call_method0("items")?.try_iter()? {
        let (name, outcome): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
        dicts.set_item(name, outcome.call_method0("to_dict")?)?;
    }

    from_py(&dicts)
}

/// A JSON-like Python value as a Rust value, through `json.dumps`.
fn from_py<T: DeserializeOwned>(value: &Bound<'_, PyAny>) -> PyResult<T> {
    let text: String = value
        .py()
        .import("json")?
        .call_method1("dumps", (value,))?
        .extract()?;

    serde_json::from_str(&text).map_err(|error| PyValueError::new_err(error.to_string()))
}

/// A Rust value as plain Python values, through `json.loads`.
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
