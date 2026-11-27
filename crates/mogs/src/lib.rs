//! `mogs._native`: the Rust side of mogs, as a Python extension module.

use pyo3::prelude::*;

#[pymodule]
mod _native {
    use pyo3::prelude::*;

    /// The version of mogs.
    #[pyfunction]
    fn version() -> &'static str {
        env!("CARGO_PKG_VERSION")
    }

    /// Run the `mogs` command line on `sys.argv` and return its exit code.
    ///
    /// The `mogs` console script calls this and passes the code to `sys.exit`.
    #[pyfunction]
    fn main(py: Python<'_>) -> PyResult<i32> {
        py.import("mogs.cli")?.call_method0("main")?.extract()
    }
}
