//! `mogs init`.

use std::path::Path;

use mogs_cli::scaffold::{Created, InitError};
use pyo3::prelude::*;
use pyo3::types::PyString;

use super::interpreter::PyInterpreter;
use super::{fail, failed, print};

/// Create the assignment `folder` and say what to do next.
///
/// A refused folder, a `UsageError` (here a `ConfigError`) or an `OSError` fails the command
/// with exit code 1. Other Python exceptions propagate.
pub(super) fn init(py: Python<'_>, folder: &Path) -> PyResult<i64> {
    match mogs_cli::scaffold::init(folder, &PyInterpreter { py }) {
        Ok(created) => next_steps(py, &created).map(|()| 0),
        Err(InitError::Refused(refusal)) => {
            let message = refusal
                .folder()
                .into_pyobject(py)?
                .str()?
                .add(refusal.reason())?
                .cast_into::<PyString>()?;
            fail(py, "Init", message)
        }
        Err(InitError::Python(error)) => failed(py, "Init", error),
    }
}

/// Print the notebook and the commands to edit and build it.
///
/// The paths stay Python strings until printed, so bytes that are not UTF-8 show as in v0.7.
fn next_steps(py: Python<'_>, created: &Created) -> PyResult<()> {
    let notebook = created.notebook.as_path().into_pyobject(py)?.str()?;
    let quoted = py
        .import("shlex")?
        .call_method1("quote", (created.folder.as_os_str(),))?;

    for (lead, path) in [
        ("Created ", notebook.into_any()),
        ("Edit it  : mogs edit ", quoted.clone()),
        ("Build it : mogs build ", quoted),
    ] {
        let line = PyString::new(py, lead).add(path)?.cast_into()?;
        print(py, line, false, "\n")?;
    }

    Ok(())
}
