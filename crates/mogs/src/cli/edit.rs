//! `mogs edit`.

use std::path::Path;

use pyo3::exceptions::PyKeyboardInterrupt;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};

use super::failed;

/// Run `marimo edit --sandbox` on the source notebook at `path`, a file or its folder, and
/// return marimo's exit code.
///
/// marimo runs on this package's Python, which has pytest. The editor server compiles the
/// test cells there, and without pytest it logs a warning for each of them. A `BuildError`
/// from `mogs._analysis.source_notebook`, or an `OSError` from it or from starting marimo, fails
/// the command with exit code 1.
pub(super) fn edit(py: Python<'_>, path: &Path) -> PyResult<i64> {
    let marimo = match start(py, path) {
        Ok(marimo) => marimo,
        Err(error) => return failed(py, "Edit", error),
    };

    // Ctrl+C reaches marimo too, and marimo asks whether to quit. Wait until it exits.
    // `Popen.wait` releases the GIL.
    loop {
        match marimo.call_method0("wait") {
            Ok(code) => return code.extract(),
            Err(error) if error.is_instance_of::<PyKeyboardInterrupt>(py) => {}
            Err(error) => return Err(error),
        }
    }
}

/// `subprocess.Popen([sys.executable, "-m", "marimo", "edit", "--sandbox", str(notebook)])`.
fn start<'py>(py: Python<'py>, path: &Path) -> PyResult<Bound<'py, PyAny>> {
    let notebook = py
        .import("mogs._analysis")?
        .call_method1("source_notebook", (path,))?
        .str()?;
    let python = py.import("sys")?.getattr("executable")?;
    let args = ["-m", "marimo", "edit", "--sandbox"].map(|arg| PyString::new(py, arg).into_any());
    let command = PyList::new(
        py,
        std::iter::once(python)
            .chain(args)
            .chain([notebook.into_any()]),
    )?;

    py.import("subprocess")?.call_method1("Popen", (command,))
}
