//! The `mogs` command line (`mogs-cli`) as `_native.main`.
//!
//! Only glue: argv goes to clap, `init` runs in Rust with Python's text, checks and file
//! operations, `edit` starts marimo through `subprocess`, `build` and `grade` run in Rust
//! (`crate::build`), and the hidden `autograde` runs the grader. Output goes through Python's
//! `print`, so it lands in `sys.stdout` and `sys.stderr` in order with Python's own. A panic
//! reaches the PyO3 boundary of `_native.main`, which raises it as `PanicException`, except in
//! `autograde`, which turns it into failure results.

mod edit;
mod interpreter;
mod scaffold;

use std::ffi::OsString;
use std::path::PathBuf;

use mogs_cli::Command;
use mogs_cli::diagnostic::{Located, render};
use pyo3::exceptions::{PyBaseException, PyOSError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

use crate::python::UsageError;

/// Run the `mogs` command line on `argv`, `sys.argv[1:]` when `None`, and return its exit
/// code.
pub fn main(py: Python<'_>, argv: Option<Vec<OsString>>) -> PyResult<i64> {
    let argv = match argv {
        Some(argv) => argv,
        None => {
            let argv: Vec<OsString> = py.import("sys")?.getattr("argv")?.extract()?;
            argv.into_iter().skip(1).collect()
        }
    };

    let command = match mogs_cli::parse(argv) {
        Ok(command) => command,
        Err(exit) => {
            print(py, PyString::new(py, &exit.text), exit.to_stderr, "")?;
            return Ok(exit.code.into());
        }
    };

    match command {
        Command::Init { dir } => scaffold::init(py, &dir),
        Command::Edit { path } => edit::edit(py, &path),
        Command::Build { dir } => crate::build::build(py, &dir),
        Command::Grade { dir, submission } => crate::build::grade(py, &dir, &submission),
        Command::Autograde {
            source,
            submission,
            results,
            workdir,
        } => crate::build::autograde(py, [source, submission, results, workdir]),
    }
}

/// `print(text, file=sys.stderr if to_stderr else sys.stdout, end=end)`.
pub(crate) fn print(
    py: Python<'_>,
    text: Bound<'_, PyString>,
    to_stderr: bool,
    end: &str,
) -> PyResult<()> {
    let sys = py.import("sys")?;
    let file = if to_stderr {
        sys.getattr("stderr")?
    } else {
        sys.getattr("stdout")?
    };
    let kwargs = PyDict::new(py);
    kwargs.set_item("file", file)?;
    kwargs.set_item("end", end)?;
    py.import("builtins")?
        .getattr("print")?
        .call((text,), Some(&kwargs))?;

    Ok(())
}

/// Print `<what> failed.` and `message` on stderr, and give exit code 1.
pub(crate) fn fail(py: Python<'_>, what: &str, message: Bound<'_, PyString>) -> PyResult<i64> {
    let text = PyString::new(py, &format!("{what} failed.\n"))
        .add(message)?
        .cast_into::<PyString>()?;
    print(py, text, true, "\n")?;

    Ok(1)
}

/// Print `<what> failed.` and the message of `error` if it is a `UsageError` or an
/// `OSError`, and give exit code 1; raise any other error.
///
/// A `UsageError` whose `diagnostic.span` names a file is drawn on that file's lines, in
/// color when stderr is a terminal and `NO_COLOR` is unset. Without such a span, when the
/// file no longer holds the span, or when drawing panics, its text is printed as it is.
pub(crate) fn failed(py: Python<'_>, what: &str, error: PyErr) -> PyResult<i64> {
    if error.is_instance_of::<UsageError>(py) {
        let value = error.value(py);

        return match drawn(py, value)? {
            Some(text) => fail(py, what, PyString::new(py, &text)),
            None => fail(py, what, value.str()?),
        };
    }

    if error.is_instance_of::<PyOSError>(py) {
        return fail(py, what, error.value(py).str()?);
    }

    Err(error)
}

/// The `UsageError` `error` drawn on the lines of its file, if its span names one.
fn drawn(py: Python<'_>, error: &Bound<'_, PyBaseException>) -> PyResult<Option<String>> {
    let diagnostic = error.getattr("diagnostic")?;
    let span = diagnostic.getattr("span")?;
    if span.is_none() {
        return Ok(None);
    }

    let path = span.getattr("path")?;
    if path.is_none() {
        return Ok(None);
    }

    let file: PathBuf = path.extract()?;
    let Ok(source) = std::fs::read_to_string(&file) else {
        return Ok(None);
    };

    let located = Located {
        message: diagnostic.getattr("message")?.extract()?,
        path: path.str()?.extract()?,
        line: span.getattr("line")?.extract()?,
        col: span.getattr("col")?.extract()?,
        end_line: span.getattr("end_line")?.extract()?,
        end_col: span.getattr("end_col")?.extract()?,
        label: diagnostic.getattr("label")?.extract()?,
        help: diagnostic.getattr("help")?.extract()?,
    };

    let terminal: bool = py
        .import("sys")?
        .getattr("stderr")?
        .call_method0("isatty")?
        .extract()?;
    let color = terminal && std::env::var_os("NO_COLOR").is_none_or(|value| value.is_empty());

    // A bug in the drawing must not hide the user's error: its plain text prints instead.
    let drawn = std::panic::catch_unwind(|| render(&located, &source, color));

    Ok(drawn.ok().flatten())
}
