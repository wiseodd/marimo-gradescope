//! `mogs init`.

use std::path::Path;

use mogs_cli::scaffold::{Created, InitError};
use pyo3::exceptions::PyOSError;
use pyo3::prelude::*;
use pyo3::types::PyString;

use super::interpreter::PyInterpreter;
use super::os_error::os_error;
use super::{Stream, fail, is_instance, print};

/// Create the assignment `folder` and say what to do next.
///
/// A `ConfigError` or `OSError` fails the command with exit code 1. Other Python exceptions
/// propagate.
pub(super) fn init(py: Python<'_>, folder: &Path) -> PyResult<i64> {
    let error = match mogs_cli::scaffold::init(folder, &PyInterpreter { py }) {
        Ok(created) => return next_steps(py, &created).map(|()| 0),
        Err(InitError::Refused(refusal)) => PyString::new(py, &refusal.to_string()),
        Err(InitError::Os(error)) => os_error(py, &error).value(py).str()?,
        Err(InitError::Python(error))
            if error.is_instance_of::<PyOSError>(py)
                || is_instance(py, &error, "mogs.config", "ConfigError")? =>
        {
            error.value(py).str()?
        }
        Err(InitError::Python(error)) => return Err(error),
    };

    fail(py, "Init", error)
}

fn next_steps(py: Python<'_>, created: &Created) -> PyResult<()> {
    let notebook = created.notebook.as_path().into_pyobject(py)?.str()?;
    let lines = [
        PyString::new(py, "Created ").add(notebook)?.cast_into()?,
        PyString::new(py, &format!("Edit it  : mogs edit {}", created.quoted)),
        PyString::new(py, &format!("Build it : mogs build {}", created.quoted)),
    ];

    for line in lines {
        print(py, line, Stream::Stdout, "\n")?;
    }

    Ok(())
}
