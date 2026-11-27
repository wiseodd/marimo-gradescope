//! The written-answer PDF (`mogs-pdf`) as a Python function.
//!
//! Only glue: the questions cross as the Python objects `mogs._answers` builds, Typst runs
//! with the GIL released, and Ctrl+C stops the wait at once.

use std::sync::Mutex;

use mogs_pdf::{Host, RenderError};
use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyRuntimeError, PyValueError};
use pyo3::panic::PanicException;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

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

    let host = SignalHost::default();
    let rendered = py.detach(|| mogs_pdf::render(questions, &host));

    if let Some(error) = host
        .pending
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .take()
    {
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

/// Asks Python for pending signals, such as Ctrl+C, and keeps the first error they raise.
#[derive(Default)]
struct SignalHost {
    pending: Mutex<Option<PyErr>>,
}

impl Host for SignalHost {
    fn interrupted(&self) -> bool {
        match Python::attach(|py| py.check_signals()) {
            Ok(()) => false,
            Err(error) => {
                let mut pending = self.pending.lock().unwrap_or_else(|e| e.into_inner());
                pending.get_or_insert(error);
                true
            }
        }
    }
}
