//! `mogs._native`: the Rust side of mogs, as a Python extension module.

mod build;
mod cli;
mod grader;
mod host;
mod pdf;
mod python;

use pyo3::prelude::*;

#[pymodule]
mod _native {
    use pyo3::prelude::*;

    #[pymodule_export]
    use super::grader::data_files;

    #[pymodule_export]
    use super::pdf::{TypstError, render_pdf};

    /// `PDF_STATUS`: the message to show while `render_pdf` works.
    #[pymodule_init]
    fn init(module: &Bound<'_, PyModule>) -> PyResult<()> {
        module.add("PDF_STATUS", mogs_pdf::STATUS)
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
