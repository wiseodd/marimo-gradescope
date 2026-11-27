//! The written-answer PDF (`mogs-pdf`) as a Python function.
//!
//! Only glue: the questions cross as the Python objects `mogs._answers` builds, Typst runs
//! with the GIL released, and Ctrl+C stops the wait at once.

use std::sync::Mutex;

use mogs_pdf::{Host, Part, Question, RenderError, Role, Style};
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

/// The PDF of `questions`, after calling `status` with a short message.
#[pyfunction]
pub fn render_pdf<'py>(
    py: Python<'py>,
    questions: &Bound<'py, PyAny>,
    status: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyBytes>> {
    let questions = questions
        .try_iter()?
        .map(|question| question_from_py(&question?))
        .collect::<PyResult<Vec<_>>>()?;

    status.call1((mogs_pdf::STATUS,))?;

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

/// A `mogs._answers.Question` as the Rust type.
fn question_from_py(question: &Bound<'_, PyAny>) -> PyResult<Question> {
    let parts = question
        .getattr("parts")?
        .try_iter()?
        .map(|part| part_from_py(&part?))
        .collect::<PyResult<Vec<_>>>()?;

    Ok(Question {
        title: question.getattr("title")?.extract()?,
        parts,
    })
}

/// A `mogs._answers.Part` as the Rust type.
fn part_from_py(part: &Bound<'_, PyAny>) -> PyResult<Part> {
    let text: String = part.getattr("text")?.extract()?;
    let role = named(&part.getattr("role")?, "role", Role::named)?;
    let style = named(&part.getattr("style")?, "style", Style::named)?;

    Ok(Part::new(text, role, style))
}

/// The value of a `StrEnum` member, or a `ValueError` that names it.
fn named<T>(
    value: &Bound<'_, PyAny>,
    what: &str,
    parse: impl Fn(&str) -> Option<T>,
) -> PyResult<T> {
    let name: String = value.extract()?;

    match parse(&name) {
        Some(found) => Ok(found),
        None => Err(PyValueError::new_err(format!(
            "unknown {what} {}",
            value.repr()?
        ))),
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
