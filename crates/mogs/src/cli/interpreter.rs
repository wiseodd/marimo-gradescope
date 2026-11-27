//! Python's text, checks and file operations for `mogs-cli`.

use std::path::{Path, PathBuf};

use mogs_cli::Interpreter;
use pyo3::prelude::*;
use pyo3::types::PyDict;

/// The running interpreter, as `mogs-cli` sees it.
pub(super) struct PyInterpreter<'py> {
    pub(super) py: Python<'py>,
}

impl<'py> PyInterpreter<'py> {
    /// `pathlib.Path(path)`.
    fn pathlib(&self, path: &Path) -> PyResult<Bound<'py, PyAny>> {
        self.py
            .import("pathlib")?
            .getattr("Path")?
            .call1((path.as_os_str(),))
    }
}

impl Interpreter for PyInterpreter<'_> {
    type Error = PyErr;

    fn check_name(&self, folder: &Path) -> PyResult<String> {
        let name = self
            .pathlib(folder)?
            .call_method0("resolve")?
            .getattr("name")?;

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
        self.pathlib(path)?.extract()
    }

    fn marimo_version(&self) -> PyResult<String> {
        self.py
            .import("importlib.metadata")?
            .call_method1("version", ("marimo",))?
            .extract()
    }

    fn exists(&self, path: &Path) -> PyResult<bool> {
        self.pathlib(path)?.call_method0("exists")?.extract()
    }

    fn is_dir(&self, path: &Path) -> PyResult<bool> {
        self.pathlib(path)?.call_method0("is_dir")?.extract()
    }

    fn has_entries(&self, folder: &Path) -> PyResult<bool> {
        let entries = self.pathlib(folder)?.call_method0("iterdir")?;

        self.py
            .import("builtins")?
            .call_method1("any", (entries,))?
            .extract()
    }

    fn make_folder(&self, folder: &Path) -> PyResult<()> {
        let kwargs = PyDict::new(self.py);
        kwargs.set_item("parents", true)?;
        kwargs.set_item("exist_ok", true)?;
        self.pathlib(folder)?
            .call_method("mkdir", (), Some(&kwargs))?;

        Ok(())
    }

    fn write_text(&self, path: &Path, text: &str) -> PyResult<()> {
        let kwargs = PyDict::new(self.py);
        kwargs.set_item("encoding", "utf-8")?;
        self.pathlib(path)?
            .call_method("write_text", (text,), Some(&kwargs))?;

        Ok(())
    }
}
