//! `mogs._native`: the Rust side of mogs, as a Python extension module.

mod cli;
mod grader;
mod pdf;

use pyo3::prelude::*;

#[pymodule]
mod _native {
    use pyo3::prelude::*;

    #[pymodule_export]
    use super::grader::{Submission, build_results, data_files, failure_results, find_submission};

    #[pymodule_export]
    use super::pdf::{TypstError, render_pdf};

    #[cfg(unix)]
    #[pymodule_export]
    use super::grader::autograde::{Graded, autograde, grade};

    /// The user that runs student code on Gradescope.
    #[pymodule_export]
    const STUDENT_USER: &str = mogs_grader::STUDENT_USER;

    /// The version of mogs.
    #[pyfunction]
    fn version() -> &'static str {
        env!("CARGO_PKG_VERSION")
    }

    /// Run the `mogs` command line on `argv`, `sys.argv[1:]` when `None`, and return its exit
    /// code.
    ///
    /// The `mogs` console script calls this and passes the code to `sys.exit`.
    #[pyfunction]
    #[pyo3(signature = (argv=None))]
    fn main(py: Python<'_>, argv: Option<Vec<std::ffi::OsString>>) -> PyResult<i64> {
        super::cli::main(py, argv)
    }
}
