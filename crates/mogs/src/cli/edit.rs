//! `mogs edit`.

use std::path::{Path, PathBuf};

use mogs_cli::edit::EditError;
use pyo3::exceptions::{PyKeyboardInterrupt, PyOSError};
use pyo3::prelude::*;

use super::os_error::os_error;
use super::{fail, is_instance};

/// Run marimo's editor on the source notebook at `path`, a file or its folder, and return
/// marimo's exit code.
///
/// The notebook comes from `mogs.build.source_notebook`. Its `BuildError` or `OSError`, or a
/// failed start, fails the command with exit code 1. While marimo runs, the GIL is released
/// and Ctrl+C is left to marimo.
pub(super) fn edit(py: Python<'_>, path: &Path) -> PyResult<i64> {
    let found = py
        .import("mogs.build")?
        .call_method1("source_notebook", (path,));
    let notebook: PathBuf = match found {
        Ok(notebook) => notebook.extract()?,
        Err(error)
            if error.is_instance_of::<PyOSError>(py)
                || is_instance(py, &error, "mogs.build", "BuildError")? =>
        {
            return fail(py, "Edit", error.value(py).str()?);
        }
        Err(error) => return Err(error),
    };

    let python: PathBuf = py.import("sys")?.getattr("executable")?.extract()?;
    let mut marimo = match mogs_cli::edit::spawn(&python, &notebook) {
        Ok(marimo) => marimo,
        Err(error) => return fail(py, "Edit", os_error(py, &error).value(py).str()?),
    };

    let waited = py.detach(|| mogs_cli::edit::wait(&mut marimo, || Python::attach(ignore_ctrl_c)));

    match waited {
        Ok(code) => Ok(code),
        Err(EditError::Os(error)) => Err(os_error(py, &error)),
        Err(EditError::Python(error)) => Err(error),
    }
}

/// Run the signal handlers, letting a `KeyboardInterrupt` pass.
fn ignore_ctrl_c(py: Python<'_>) -> PyResult<()> {
    match py.check_signals() {
        Err(error) if error.is_instance_of::<PyKeyboardInterrupt>(py) => Ok(()),
        checked => checked,
    }
}
