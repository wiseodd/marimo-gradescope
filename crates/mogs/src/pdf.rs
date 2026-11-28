//! The written-answer PDF (`mogs-pdf`) as a Python function.
//!
//! Only glue: the questions cross as the Python objects `mogs._answers` builds, Typst runs
//! with the GIL released, and Ctrl+C stops the wait at once. Typst runs on a worker thread
//! that `mogs-pdf` watches, so its panic comes back as an error and is raised as
//! `PanicException`; a panic in this module reaches the PyO3 boundary, which does the same.

use std::str::FromStr;

use mogs_pdf::{RenderError, Unknown};
use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyValueError};
use pyo3::panic::PanicException;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::host::{PyHost, stopped};

create_exception!(
    mogs._native,
    TypstError,
    PyException,
    "Typst failed, even on the plain-text document. The message is Typst's."
);

/// The PDF of `questions`.
#[pyfunction]
pub fn render_pdf<'py>(
    py: Python<'py>,
    questions: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyBytes>> {
    let questions = questions
        .try_iter()?
        .map(|question| Ok(question?.extract::<Question>()?.into()))
        .collect::<PyResult<Vec<mogs_pdf::Question>>>()?;

    let host = PyHost::default();
    let rendered = py.detach(|| mogs_pdf::render(questions, &host));
    let pdf = host.finish(rendered, |error| match error {
        RenderError::Typst(message) => TypstError::new_err(message),
        RenderError::Panicked(message) => PanicException::new_err(message),
        RenderError::Interrupted => stopped(),
    })?;

    Ok(PyBytes::new(py, &pdf))
}

/// A `mogs._answers.Question`, read by attribute.
#[derive(FromPyObject)]
struct Question {
    title: String,
    parts: Vec<Part>,
}

/// A `mogs._answers.Part`, read by attribute.
#[derive(FromPyObject)]
struct Part {
    text: String,
    #[pyo3(from_py_with = named)]
    role: mogs_pdf::Role,
    #[pyo3(from_py_with = named)]
    style: mogs_pdf::Style,
}

/// A `Role` or `Style` member, by its value. An unknown one is a `ValueError` that names the
/// value as Python's `repr` shows it.
fn named<T: FromStr<Err = Unknown>>(value: &Bound<'_, PyAny>) -> PyResult<T> {
    match value.extract::<String>()?.parse() {
        Ok(parsed) => Ok(parsed),
        Err(Unknown(what)) => Err(PyValueError::new_err(format!(
            "unknown {what} {}",
            value.repr()?
        ))),
    }
}

impl From<Question> for mogs_pdf::Question {
    fn from(question: Question) -> Self {
        let parts = question
            .parts
            .into_iter()
            .map(|part| mogs_pdf::Part::new(part.text, part.role, part.style))
            .collect();

        Self {
            title: question.title,
            parts,
        }
    }
}
