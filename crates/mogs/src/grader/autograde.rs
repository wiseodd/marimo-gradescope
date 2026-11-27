//! `mogs autograde`: grading with the GIL released. Linux and macOS.

use std::ffi::OsString;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::{Path, PathBuf};

use mogs_schema::{Bundle, Results};
use pyo3::exceptions::PyException;
use pyo3::panic::PanicException;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use super::{PyHost, bundle_from_py, os_error, results_to_py};

/// The logger of the autograder's crash report, as in v0.7.
const MAIN_LOGGER: &str = "mogs.grader.main";

/// Grade `submission` against `source/bundle.json` and write `results`, whatever happens.
///
/// A failure, an `Exception` or a Rust panic, becomes results that name only its class,
/// after a log of it. Other exceptions, such as `KeyboardInterrupt`, propagate.
pub fn autograde(
    py: Python<'_>,
    source: PathBuf,
    submission: PathBuf,
    results: PathBuf,
    workdir: PathBuf,
) -> PyResult<()> {
    let mut bundle = None;
    let graded = autograde_run(py, &mut bundle, &source, &submission, &workdir);

    let content = match graded {
        Ok(content) => content,
        Err(error)
            if error.is_instance_of::<PyException>(py)
                || error.is_instance_of::<PanicException>(py) =>
        {
            let logger = py
                .import("logging")?
                .call_method1("getLogger", (MAIN_LOGGER,))?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("exc_info", error.value(py))?;
            logger.call_method("error", ("the autograder failed",), Some(&kwargs))?;
            let name = error.get_type(py).name()?.to_string();
            mogs_grader::results::failure_results(bundle.as_ref(), &name)
        }
        Err(error) => return Err(error),
    };

    // As v0.7 wrote it, with infinite or NaN scores as Python's JSON writes them.
    let kwargs = PyDict::new(py);
    kwargs.set_item("indent", 2)?;
    let text: String = py
        .import("json")?
        .call_method("dumps", (results_to_py(py, &content)?,), Some(&kwargs))?
        .extract()?;

    if let Some(parent) = results.parent() {
        std::fs::create_dir_all(parent).map_err(|e| os_error(py, &e, Some(parent.into())))?;
    }

    std::fs::write(&results, text).map_err(|e| os_error(py, &e, Some(results.clone())))
}

/// Read the bundle as Python does, then grade with the GIL released.
fn autograde_run(
    py: Python<'_>,
    bundle: &mut Option<Bundle>,
    source: &Path,
    submission: &Path,
    workdir: &Path,
) -> PyResult<Results> {
    // Python reads the bundle, so a bad one fails with v0.7's exception class.
    let text = py
        .import("pathlib")?
        .getattr("Path")?
        .call1((source.join("bundle.json"),))?
        .call_method1("read_text", ("utf-8",))?;
    let read = py
        .import("mogs.grader.bundle")?
        .getattr("GradingBundle")?
        .call_method1("from_json", (text,))?;
    let read = bundle.insert(bundle_from_py(&read)?);
    let python: OsString = py.import("sys")?.getattr("executable")?.extract()?;

    let host = PyHost::default();
    let ran = py.detach(|| {
        catch_unwind(AssertUnwindSafe(|| {
            mogs_grader::autograde::run(read, source, submission, workdir, &python, &host)
        }))
    });

    match ran {
        Ok(ran) => host.finish(py, ran),
        // The panic hook has printed the message on stderr.
        Err(_) => Err(PanicException::new_err("the grader panicked")),
    }
}
