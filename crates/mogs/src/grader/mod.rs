//! The grading supervisor (`mogs-grader`) for the rest of the extension.
//!
//! Only glue: `autograde` runs `mogs autograde` with the GIL released; `data_files` serves
//! `mogs._analysis`; and bundles and results cross to and from Python as JSON text.
//! [`PyHost`] gives the grader Python's text, users, signals and logs. A panic here reaches the
//! PyO3 boundary, which raises it as `PanicException`; `autograde` catches its own (see there).

#[cfg(unix)]
pub mod autograde;

use std::ffi::OsString;
use std::path::PathBuf;

use mogs_schema::Bundle;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::host::PyHost;
use crate::python::ConfigError;

/// The data files to ship, by `PurePosixPath` relative to `root`, with folders expanded.
///
/// A bad entry is a `ConfigError` of `mogs.assignment() \`files\``.
#[pyfunction]
pub fn data_files<'py>(
    py: Python<'py>,
    root: PathBuf,
    files: Vec<String>,
) -> PyResult<Bound<'py, PyDict>> {
    let found = match mogs_grader::files::data_files(&root, &files) {
        Ok(found) => found,
        Err(error) => {
            let host = PyHost::default();
            let message = host.finish(Ok::<_, PyErr>(error.message(&host)), |e| e)?;
            let assignment: String = py.import("mogs.config")?.getattr("ASSIGNMENT")?.extract()?;
            let lead = format!("{assignment} `files`: ");
            let text: OsString = [lead.as_ref(), message.as_os_str(), ".".as_ref()]
                .into_iter()
                .collect();
            return Err(ConfigError::new_err(text));
        }
    };
    let posix = py.import("pathlib")?.getattr("PurePosixPath")?;
    let out = PyDict::new(py);

    for (rel, source) in found {
        out.set_item(posix.call1((rel,))?, source)?;
    }

    Ok(out)
}

/// The bundle JSON `text`, as Python's `json` writes it, as the Rust type.
pub(crate) fn decode_bundle(text: &str) -> PyResult<Bundle> {
    mogs_schema::decode(text).map_err(|error| PyValueError::new_err(error.to_string()))
}

/// `results` as plain Python values, through Python's JSON.
#[cfg(unix)]
pub(crate) fn results_to_py<'py>(
    py: Python<'py>,
    results: &mogs_schema::Results,
) -> PyResult<Bound<'py, PyAny>> {
    let text =
        mogs_schema::encode(results).map_err(|error| PyValueError::new_err(error.to_string()))?;

    py.import("json")?.call_method1("loads", (text,))
}
