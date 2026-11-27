//! The `mogs` command line (`mogs-cli`) as `_native.main`.
//!
//! Only glue: argv goes to clap, `init` and `edit` run in Rust with Python's text and checks,
//! and `build` and `grade` run their Python handlers in `mogs.cli`. Output goes through
//! Python's `print`, so it lands in `sys.stdout` and `sys.stderr` in order with Python's own.

mod edit;
mod interpreter;
mod os_error;
mod scaffold;

use std::ffi::OsString;

use mogs_cli::Command;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

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
            print(
                py,
                PyString::new(py, &exit.text),
                stream(exit.to_stderr),
                "",
            )?;
            return Ok(exit.code.into());
        }
    };

    match command {
        Command::Init { dir } => scaffold::init(py, &dir),
        Command::Edit { path } => edit::edit(py, &path),
        Command::Build { dir } => py
            .import("mogs.cli")?
            .call_method1("build_command", (dir,))?
            .extract(),
        Command::Grade { dir, submission } => py
            .import("mogs.cli")?
            .call_method1("grade_command", (dir, submission))?
            .extract(),
    }
}

/// Where `print` writes.
#[derive(Clone, Copy)]
enum Stream {
    Stdout,
    Stderr,
}

fn stream(to_stderr: bool) -> Stream {
    if to_stderr {
        Stream::Stderr
    } else {
        Stream::Stdout
    }
}

/// `print(text, file=sys.stdout or sys.stderr, end=end)`.
fn print(py: Python<'_>, text: Bound<'_, PyString>, to: Stream, end: &str) -> PyResult<()> {
    let sys = py.import("sys")?;
    let file = match to {
        Stream::Stdout => sys.getattr("stdout")?,
        Stream::Stderr => sys.getattr("stderr")?,
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
fn fail(py: Python<'_>, what: &str, message: Bound<'_, PyString>) -> PyResult<i64> {
    let text = PyString::new(py, &format!("{what} failed.\n"))
        .add(message)?
        .cast_into::<PyString>()?;
    print(py, text, Stream::Stderr, "\n")?;

    Ok(1)
}

/// Whether `error` is an instance of the class `name` in `module`.
fn is_instance(py: Python<'_>, error: &PyErr, module: &str, name: &str) -> PyResult<bool> {
    let class = py.import(module)?.getattr(name)?;

    error.value(py).is_instance(&class)
}
