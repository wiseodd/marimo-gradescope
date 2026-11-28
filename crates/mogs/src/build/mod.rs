//! `mogs build`, and `mogs autograde`, which needs Linux or macOS.
//!
//! Only glue: `build` runs `mogs-build` with the GIL released, with the notebook's analysis in
//! Python, and prints v0.7's text. Its errors are Python exceptions; a
//! `UsageError` or an `OSError` fails the command with exit code 1. A panic reaches the PyO3
//! boundary, which raises it as `PanicException`: the command fails with a traceback, as for
//! a bug. `autograde` catches its own (see `crate::grader::autograde`). On other platforms,
//! `platform` refuses it.

mod interpreter;

use std::ffi::OsString;
use std::path::{Path, PathBuf};

use pyo3::exceptions::PyException;
use pyo3::prelude::*;

pub(crate) use self::platform::autograde;
use crate::cli::{failed, print};
use crate::host::PyHost;
use crate::python::pathlib;

/// Run `mogs build folder`: build, then print the report; give the exit code.
///
/// A failed build removes `dist/student/` and `dist/autograder/`.
pub(crate) fn build(py: Python<'_>, folder: &Path) -> PyResult<i64> {
    let folder: PathBuf = pathlib(py, folder)?.extract()?;

    match built(py, &folder) {
        Ok(report) => {
            print(py, report.into_pyobject(py)?, false, "\n")?;
            Ok(0)
        }
        Err(error) => {
            if error.is_instance_of::<PyException>(py) {
                mogs_build::remove_output(&folder);
            }

            failed(py, "Build", error)
        }
    }
}

/// The report of a build of `folder`, with the GIL released. An error the host kept comes
/// first.
fn built(py: Python<'_>, folder: &Path) -> PyResult<OsString> {
    let host = PyHost::default();
    let result = py.detach(|| {
        let built = mogs_build::build::build(&host, folder)?;
        mogs_build::report::report(&host, &built)
    });

    host.finish(result, |error| error)
}

#[cfg(unix)]
mod platform {
    use std::path::PathBuf;

    use pyo3::prelude::*;
    use pyo3::types::PyDict;

    /// Grade the submission and write `results.json`, whatever happens, with logs on stderr.
    ///
    /// Exit code 0, since Gradescope treats any other as a crash and shows no results.
    pub(crate) fn autograde(py: Python<'_>, paths: [PathBuf; 4]) -> PyResult<i64> {
        let [source, submission, results, workdir] = paths;
        let logging = py.import("logging")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("level", logging.getattr("INFO")?)?;
        kwargs.set_item("stream", py.import("sys")?.getattr("stderr")?)?;
        logging.call_method("basicConfig", (), Some(&kwargs))?;

        crate::grader::autograde::autograde(py, source, submission, results, workdir)?;

        Ok(0)
    }
}

#[cfg(not(unix))]
mod platform {
    use std::path::PathBuf;

    use pyo3::prelude::*;
    use pyo3::types::PyString;

    use crate::cli::fail;
    use crate::python::REFUSAL;

    pub(crate) fn autograde(py: Python<'_>, _paths: [PathBuf; 4]) -> PyResult<i64> {
        fail(py, "Grading", PyString::new(py, REFUSAL))
    }
}
