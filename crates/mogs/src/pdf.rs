//! The written-answer PDF (`mogs-pdf`) as a Python function.
//!
//! Only glue: the questions cross as the Python objects `mogs._answers` builds, Typst runs
//! with the GIL released, and Ctrl+C stops the wait at once. Typst runs on a worker thread
//! that `mogs-pdf` watches, so its panic comes back as an error and is raised as
//! `PanicException`; a panic in this module reaches the PyO3 boundary, which does the same.

use mogs_pdf::RenderError;
use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyRuntimeError, PyValueError};
use pyo3::panic::PanicException;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::host::PyHost;

create_exception!(
    mogs._native,
    TypstError,
    PyException,
    "Typst failed, even on the plain-text document. The message is Typst's."
);

/// Call `status` with a short message, then make the PDF of `questions`.
///
/// `questions` is read after `status`, so a generator builds them after the message shows.
#[pyfunction]
pub fn render_pdf<'py>(
    py: Python<'py>,
    questions: &Bound<'py, PyAny>,
    status: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyBytes>> {
    status.call1((mogs_pdf::STATUS,))?;

    let questions = questions
        .try_iter()?
        .map(|question| Ok(question?.extract::<Question>()?.into()))
        .collect::<PyResult<Vec<mogs_pdf::Question>>>()?;

    let host = PyHost::default();
    let rendered = py.detach(|| mogs_pdf::render(questions, &host));

    if let Some(error) = host.take() {
        return Err(error);
    }

    match rendered {
        Ok(pdf) => Ok(PyBytes::new(py, &pdf)),
        Err(RenderError::Typst(message)) => Err(TypstError::new_err(message)),
        Err(RenderError::Panicked(message)) => Err(PanicException::new_err(message)),
        Err(RenderError::Interrupted) => Err(PyRuntimeError::new_err(
            "the PDF stopped without a Python error",
        )),
    }
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
    role: Role,
    style: Style,
}

/// A `mogs._answers.Role` member.
struct Role(mogs_pdf::Role);

/// A `mogs._answers.Style` member.
struct Style(mogs_pdf::Style);

impl<'a, 'py> FromPyObject<'a, 'py> for Role {
    type Error = PyErr;

    fn extract(value: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        match value.extract::<String>()?.as_str() {
            "prompt" => Ok(Self(mogs_pdf::Role::Prompt)),
            "answer" => Ok(Self(mogs_pdf::Role::Answer)),
            _ => Err(unknown("role", &value)),
        }
    }
}

impl<'a, 'py> FromPyObject<'a, 'py> for Style {
    type Error = PyErr;

    fn extract(value: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        match value.extract::<String>()?.as_str() {
            "markdown" => Ok(Self(mogs_pdf::Style::Markdown)),
            "plain" => Ok(Self(mogs_pdf::Style::Plain)),
            "note" => Ok(Self(mogs_pdf::Style::Note)),
            _ => Err(unknown("style", &value)),
        }
    }
}

/// A `ValueError` that names the value, as Python's `repr` shows it.
fn unknown(what: &str, value: &Bound<'_, PyAny>) -> PyErr {
    match value.repr() {
        Ok(repr) => PyValueError::new_err(format!("unknown {what} {repr}")),
        Err(error) => error,
    }
}

impl From<Question> for mogs_pdf::Question {
    fn from(question: Question) -> Self {
        let parts = question
            .parts
            .into_iter()
            .map(|part| mogs_pdf::Part::new(part.text, part.role.0, part.style.0))
            .collect();

        Self {
            title: question.title,
            parts,
        }
    }
}
