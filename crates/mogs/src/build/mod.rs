//! `mogs build` and `mogs grade` (`mogs-build`), with the notebook's analysis in Python.
//!
//! Only glue: both run in Rust with the GIL released, and print v0.7's text. Their errors are
//! Python exceptions, and `BuildError`, `ConfigError`, `SourceError`, `RequirementsError` and
//! `OSError` fail the command with exit code 1.

#[cfg(unix)]
mod interpreter;

use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

use pyo3::exceptions::{PyException, PyOSError};
use pyo3::prelude::*;

use crate::cli::{fail, is_instance, print};

/// The exceptions that fail a command with its message, by module and class.
const FAILURES: [(&str, &str); 4] = [
    ("mogs._analysis", "BuildError"),
    ("mogs.config", "ConfigError"),
    ("mogs.source", "SourceError"),
    ("mogs.requirements", "RequirementsError"),
];

/// Run `mogs build folder`: build, then print the report; give the exit code.
///
/// A failed build removes `dist/student/` and `dist/autograder/`.
pub(crate) fn build(py: Python<'_>, folder: &Path) -> PyResult<i64> {
    let folder = pathlib(py, folder)?;

    match posix(py).and_then(|()| built(py, &folder)) {
        Ok(report) => {
            print(py, report.into_pyobject(py)?, false, "\n")?;
            Ok(0)
        }
        Err(error) => {
            if error.is_instance_of::<PyException>(py) {
                remove_output(&folder);
            }

            failed(py, "Build", error)
        }
    }
}

/// Run `mogs grade folder submission`: grade, then print the scores; give the exit code.
///
/// A submission that does not load fails the command after the scores.
pub(crate) fn grade(py: Python<'_>, folder: &Path, submission: &Path) -> PyResult<i64> {
    let folder = pathlib(py, folder)?;
    let submission = pathlib(py, submission)?;

    match posix(py).and_then(|()| graded(py, &folder, &submission)) {
        Ok((report, None)) => {
            print(py, report.into_pyobject(py)?, false, "\n")?;
            Ok(0)
        }
        Ok((report, Some(load_failure))) => {
            print(py, report.into_pyobject(py)?, false, "\n")?;
            fail(py, "Grading", load_failure.into_pyobject(py)?)
        }
        Err(error) => failed(py, "Grading", error),
    }
}

/// The report of a build of `folder`.
#[cfg(unix)]
fn built(py: Python<'_>, folder: &Path) -> PyResult<OsString> {
    run(py, |i| {
        let built = mogs_build::build::build(i, folder)?;
        mogs_build::report::report(i, &built)
    })
}

/// The report of grading `submission`, and why it did not load, if it did not.
#[cfg(unix)]
fn graded(
    py: Python<'_>,
    folder: &Path,
    submission: &Path,
) -> PyResult<(OsString, Option<OsString>)> {
    run(py, |i| {
        let regraded = mogs_build::regrade::grade(i, folder, submission)?;
        let report = mogs_build::report::grade_report(i, &regraded)?;
        let load_failure = regraded.graded.load_error.as_ref().map(|error| {
            let mut message = regraded.submission.clone().into_os_string();
            message.push(" failed to load: ");
            message.push(error);
            message
        });

        Ok((report, load_failure))
    })
}

#[cfg(not(unix))]
fn built(py: Python<'_>, _folder: &Path) -> PyResult<OsString> {
    Err(refusal(py))
}

#[cfg(not(unix))]
fn graded(
    py: Python<'_>,
    _folder: &Path,
    _submission: &Path,
) -> PyResult<(OsString, Option<OsString>)> {
    Err(refusal(py))
}

/// Run `f` with the GIL released. An error the host kept comes first.
#[cfg(unix)]
fn run<T: Send>(
    py: Python<'_>,
    f: impl FnOnce(&interpreter::PyBuild) -> PyResult<T> + Send,
) -> PyResult<T> {
    let python = interpreter::PyBuild::default();
    let result = py.detach(|| f(&python));
    python.finish(py)?;

    result
}

/// Refuse to grade on a platform the sandbox does not support, as `sys.platform` names it.
fn posix(py: Python<'_>) -> PyResult<()> {
    let platform: String = py.import("sys")?.getattr("platform")?.extract()?;

    match platform.as_str() {
        "linux" | "darwin" => Ok(()),
        _ => Err(refusal(py)),
    }
}

fn refusal(py: Python<'_>) -> PyErr {
    let made = py
        .import("mogs._analysis")
        .and_then(|module| module.getattr("BuildError"))
        .and_then(|class| class.call1(("Grading needs Linux or macOS (WSL on Windows).",)));

    match made {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

/// Print `<what> failed.` and the message of `error` if it is one that fails a command, or
/// raise it.
fn failed(py: Python<'_>, what: &str, error: PyErr) -> PyResult<i64> {
    let mut failure = error.is_instance_of::<PyOSError>(py);

    for (module, name) in FAILURES {
        failure = failure || is_instance(py, &error, module, name)?;
    }

    match failure {
        true => fail(py, what, error.value(py).str()?),
        false => Err(error),
    }
}

/// Remove what a failed build leaves, ignoring errors, as `shutil.rmtree(...,
/// ignore_errors=True)`.
fn remove_output(folder: &Path) {
    for part in ["student", "autograder"] {
        let _ = fs::remove_dir_all(folder.join("dist").join(part));
    }
}

/// `pathlib.Path(path)`, which shows `./a/` as `a`.
fn pathlib(py: Python<'_>, path: &Path) -> PyResult<PathBuf> {
    py.import("pathlib")?
        .getattr("Path")?
        .call1((path,))?
        .extract()
}
