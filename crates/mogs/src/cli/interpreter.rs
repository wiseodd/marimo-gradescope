//! Python's text, checks and file operations for `mogs-cli`.

use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};

use mogs_cli::Interpreter;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::python::pathlib;

/// The running interpreter, as `mogs-cli` sees it.
pub(super) struct PyInterpreter<'py> {
    pub(super) py: Python<'py>,
}

impl Interpreter for PyInterpreter<'_> {
    type Error = PyErr;

    fn check_name(&self, folder: &Path) -> PyResult<OsString> {
        let name = pathlib(self.py, folder)?
            .call_method0("resolve")?
            .getattr("name")?;

        self.py
            .import("mogs.config")?
            .call_method1("check_name", (name,))?
            .extract()
    }

    fn literal(&self, text: &OsStr) -> PyResult<String> {
        self.py
            .import("mogs.transform")?
            .call_method1("python_literal", (text,))?
            .extract()
    }

    fn path(&self, path: &Path) -> PyResult<PathBuf> {
        pathlib(self.py, path)?.extract()
    }

    fn marimo_version(&self) -> PyResult<String> {
        self.py
            .import("importlib.metadata")?
            .call_method1("version", ("marimo",))?
            .extract()
    }

    fn exists(&self, path: &Path) -> PyResult<bool> {
        pathlib(self.py, path)?.call_method0("exists")?.extract()
    }

    fn is_dir(&self, path: &Path) -> PyResult<bool> {
        pathlib(self.py, path)?.call_method0("is_dir")?.extract()
    }

    fn has_entries(&self, folder: &Path) -> PyResult<bool> {
        let entries = pathlib(self.py, folder)?.call_method0("iterdir")?;

        self.py
            .import("builtins")?
            .call_method1("any", (entries,))?
            .extract()
    }

    fn make_folder(&self, folder: &Path) -> PyResult<()> {
        let kwargs = PyDict::new(self.py);
        kwargs.set_item("parents", true)?;
        kwargs.set_item("exist_ok", true)?;
        pathlib(self.py, folder)?.call_method("mkdir", (), Some(&kwargs))?;

        Ok(())
    }

    fn write_text(&self, path: &Path, text: &str) -> PyResult<()> {
        let kwargs = PyDict::new(self.py);
        kwargs.set_item("encoding", "utf-8")?;
        pathlib(self.py, path)?.call_method("write_text", (text,), Some(&kwargs))?;

        Ok(())
    }
}
