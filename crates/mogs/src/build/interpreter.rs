//! Python's analysis, text and exceptions for `mogs-build`, through [`PyHost`].
//!
//! Each call attaches to Python for its own duration, so the build runs with the GIL released.

use std::ffi::OsString;
use std::path::{Path, PathBuf};

use mogs_build::{BuildInputs, Class, GradeInputs, Interpreter};
use mogs_grader::{GradeError, Host};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyString, PyTuple};

use crate::grader::decode_bundle;
use crate::host::PyHost;
use crate::python::{BuildError, ConfigError, RequirementsError, pathlib};

/// `mogs._analysis.name(*args)`.
fn analysis<'py>(
    py: Python<'py>,
    name: &str,
    args: impl pyo3::call::PyCallArgs<'py>,
) -> PyResult<Bound<'py, PyAny>> {
    py.import("mogs._analysis")?.call_method1(name, args)
}

/// What grading needs, from the dict of `build_inputs`.
fn grading(inputs: &Bound<'_, PyAny>) -> PyResult<GradeInputs> {
    Ok(GradeInputs {
        bundle: decode_bundle(&inputs.get_item("bundle_json")?.extract::<String>()?)?,
        requirements: inputs.get_item("grading_requirements")?.extract()?,
        data: inputs
            .get_item("data")?
            .extract::<Vec<_>>()?
            .into_iter()
            .collect(),
    })
}

impl Interpreter for PyHost {
    type Error = PyErr;
    type Assignment = Py<PyAny>;

    fn host(&self) -> &dyn Host {
        self
    }

    fn grade_error(&self, error: GradeError) -> PyErr {
        PyHost::grade_error(self, error)
    }

    fn raise(&self, class: Class, message: OsString) -> PyErr {
        match class {
            Class::Build => BuildError::new_err(message),
            Class::Config => ConfigError::new_err(message),
            Class::Requirements => RequirementsError::new_err(message),
        }
    }

    fn read(&self, folder: &Path) -> PyResult<Py<PyAny>> {
        Python::attach(|py| Ok(analysis(py, "read_assignment", (folder,))?.unbind()))
    }

    fn build_inputs(&self, assignment: &Py<PyAny>) -> PyResult<BuildInputs> {
        Python::attach(|py| {
            let inputs = analysis(py, "build_inputs", (assignment.bind(py),))?;

            Ok(BuildInputs {
                grading: grading(&inputs)?,
                bundle_json: inputs.get_item("bundle_json")?.extract()?,
                student: inputs.get_item("student")?.extract()?,
                solved: inputs.get_item("solved")?.extract()?,
                tests: inputs.get_item("tests")?.extract()?,
                requirements: inputs.get_item("requirements")?.extract()?,
                warnings: inputs.get_item("warnings")?.extract()?,
                prompts: inputs.get_item("prompts")?.extract()?,
            })
        })
    }

    fn tool_install_args(&self) -> PyResult<Vec<String>> {
        Python::attach(|py| analysis(py, "installed_tool_args", ())?.extract())
    }

    fn which(&self, name: &str) -> PyResult<Option<PathBuf>> {
        Python::attach(|py| {
            py.import("shutil")?
                .call_method1("which", (name,))?
                .extract()
        })
    }

    fn path(&self, path: &Path) -> PyResult<PathBuf> {
        Python::attach(|py| pathlib(py, path)?.extract())
    }

    fn now(&self, format: &str) -> PyResult<String> {
        Python::attach(|py| {
            let datetime = py.import("datetime")?.getattr("datetime")?;
            datetime
                .call_method0("now")?
                .call_method1("strftime", (format,))?
                .extract()
        })
    }

    fn decoded(&self, streams: &[&[u8]]) -> PyResult<String> {
        Python::attach(|py| {
            let streams = streams.iter().map(|stream| PyBytes::new(py, stream));
            analysis(py, "decoded", PyTuple::new(py, streams)?)?.extract()
        })
    }

    fn strip(&self, text: &str) -> PyResult<String> {
        Python::attach(|py| PyString::new(py, text).call_method0("strip")?.extract())
    }

    fn sum(&self, values: &[f64]) -> PyResult<f64> {
        Python::attach(|py| {
            py.import("builtins")?
                .call_method1("sum", (PyList::new(py, values)?,))?
                .extract()
        })
    }

    fn deflate(&self, data: &[u8]) -> PyResult<Vec<u8>> {
        Python::attach(|py| {
            let zlib = py.import("zlib")?;
            let level = zlib.getattr("Z_DEFAULT_COMPRESSION")?;
            let compressed = zlib.call_method1("compress", (PyBytes::new(py, data), level, -15))?;

            Ok(compressed.cast_into::<PyBytes>()?.as_bytes().to_vec())
        })
    }
}
