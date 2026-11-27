//! Python's text and checks for `mogs-cli`.

use std::ffi::OsStr;
use std::path::{Path, PathBuf};

use mogs_cli::Interpreter;
use pyo3::prelude::*;

/// The running interpreter, as `mogs-cli` sees it.
pub(super) struct PyInterpreter<'py> {
    pub(super) py: Python<'py>,
}

impl Interpreter for PyInterpreter<'_> {
    type Error = PyErr;

    fn check_name(&self, name: &OsStr) -> PyResult<String> {
        self.py
            .import("mogs.config")?
            .call_method1("check_name", (name,))?
            .extract()
    }

    fn literal(&self, text: &str) -> PyResult<String> {
        self.py
            .import("mogs.transform")?
            .call_method1("python_literal", (text,))?
            .extract()
    }

    fn quote(&self, path: &Path) -> PyResult<String> {
        self.py
            .import("shlex")?
            .call_method1("quote", (path.as_os_str(),))?
            .extract()
    }

    fn path(&self, path: &Path) -> PyResult<PathBuf> {
        self.py
            .import("pathlib")?
            .getattr("Path")?
            .call1((path.as_os_str(),))?
            .extract()
    }

    fn marimo_version(&self) -> PyResult<String> {
        self.py
            .import("importlib.metadata")?
            .call_method1("version", ("marimo",))?
            .extract()
    }
}
