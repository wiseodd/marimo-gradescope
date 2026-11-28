//! Python's analysis, text and exceptions for `mogs-build`.
//!
//! Each call attaches to Python for its own duration, so the build runs with the GIL released.

use std::convert::Infallible;
use std::ffi::OsString;
use std::path::{Path, PathBuf};

use mogs_build::{BuildInputs, Class, GradeInputs, Interpreter, Question};
use mogs_grader::{GradeError, Host};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyString, PyTuple};

use crate::grader::bundle_from_dict;
use crate::host::PyHost;
use crate::python::{exception, pathlib};

/// The running interpreter, as `mogs-build` sees it.
#[derive(Default)]
pub(super) struct PyBuild {
    host: PyHost,
}

impl PyBuild {
    /// The error the host kept, if any.
    pub(super) fn finish(&self, py: Python<'_>) -> PyResult<()> {
        self.host.finish(py, Ok(()))
    }

    /// `mogs._analysis.name(*args)`.
    fn analysis<'py>(
        py: Python<'py>,
        name: &str,
        args: impl pyo3::call::PyCallArgs<'py>,
    ) -> PyResult<Bound<'py, PyAny>> {
        py.import("mogs._analysis")?.call_method1(name, args)
    }
}

/// What grading needs, from the dict of `build_inputs` or `grade_inputs`.
fn grading(inputs: &Bound<'_, PyAny>) -> PyResult<GradeInputs> {
    Ok(GradeInputs {
        bundle: bundle_from_dict(&inputs.get_item("bundle")?)?,
        requirements: inputs.get_item("grading_requirements")?.extract()?,
    })
}

fn question(item: PyResult<Bound<'_, PyAny>>) -> PyResult<Question> {
    let item = item?;

    Ok(Question {
        id: item.get_item("id")?.extract()?,
        title: item.get_item("title")?.extract()?,
        points: item.get_item("points")?.extract()?,
        public: item.get_item("public")?.extract()?,
        hidden: item.get_item("hidden")?.extract()?,
        prompts: item.get_item("prompts")?.extract()?,
    })
}

impl Interpreter for PyBuild {
    type Error = PyErr;
    type Assignment = Py<PyAny>;

    fn host(&self) -> &dyn Host {
        &self.host
    }

    fn grade_error(&self, error: GradeError) -> PyErr {
        Python::attach(
            |py| match self.host.finish(py, Err::<Infallible, _>(error)) {
                Err(error) => error,
                Ok(never) => match never {},
            },
        )
    }

    fn raise(&self, class: Class, message: OsString) -> PyErr {
        let (module, name) = match class {
            Class::Build => ("mogs._analysis", "BuildError"),
            Class::Config => ("mogs.config", "ConfigError"),
            Class::Requirements => ("mogs.requirements", "RequirementsError"),
        };

        Python::attach(|py| exception(py, module, name, message))
    }

    fn read(&self, folder: &Path) -> PyResult<Py<PyAny>> {
        Python::attach(|py| Ok(Self::analysis(py, "read_assignment", (folder,))?.unbind()))
    }

    fn notebook(&self, assignment: &Py<PyAny>) -> PyResult<String> {
        Python::attach(|py| {
            let config = assignment.bind(py).getattr("cfg")?;
            config.getattr("notebook")?.extract()
        })
    }

    fn build_inputs(&self, assignment: &Py<PyAny>) -> PyResult<BuildInputs> {
        Python::attach(|py| {
            let inputs = Self::analysis(py, "build_inputs", (assignment.bind(py),))?;
            let questions = inputs.get_item("questions")?;

            Ok(BuildInputs {
                grading: grading(&inputs)?,
                bundle_json: inputs.get_item("bundle_json")?.extract()?,
                student: inputs.get_item("student")?.extract()?,
                solved: inputs.get_item("solved")?.extract()?,
                tests: inputs.get_item("tests")?.extract()?,
                data: inputs.get_item("data")?.extract()?,
                requirements: inputs.get_item("requirements")?.extract()?,
                warnings: inputs.get_item("warnings")?.extract()?,
                questions: questions
                    .try_iter()?
                    .map(question)
                    .collect::<PyResult<_>>()?,
            })
        })
    }

    fn grade_inputs(&self, assignment: &Py<PyAny>) -> PyResult<GradeInputs> {
        Python::attach(|py| grading(&Self::analysis(py, "grade_inputs", (assignment.bind(py),))?))
    }

    fn unpack(&self, archive: &Path, target: &Path) -> PyResult<()> {
        Python::attach(|py| Self::analysis(py, "unpack", (archive, target)).map(drop))
    }

    fn tool_install_args(&self) -> PyResult<Vec<String>> {
        Python::attach(|py| Self::analysis(py, "installed_tool_args", ())?.extract())
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
            Self::analysis(py, "decoded", PyTuple::new(py, streams)?)?.extract()
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
            let method = zlib.getattr("DEFLATED")?;
            let compressor = zlib.call_method1("compressobj", (level, method, -15))?;
            let head: Vec<u8> = compressor
                .call_method1("compress", (PyBytes::new(py, data),))?
                .extract()?;
            let tail: Vec<u8> = compressor.call_method0("flush")?.extract()?;

            Ok([head, tail].concat())
        })
    }
}
