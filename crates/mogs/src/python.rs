//! Small calls into Python that several glue modules share.

use std::path::Path;

use pyo3::exceptions::PyOSError;
use pyo3::prelude::*;

/// `pathlib.Path(path)`, which shows `./a/` as `a`.
pub(crate) fn pathlib<'py>(py: Python<'py>, path: &Path) -> PyResult<Bound<'py, PyAny>> {
    py.import("pathlib")?
        .getattr("Path")?
        .call1((path.as_os_str(),))
}

/// The exception `module.name(message)`, or the error of making it.
pub(crate) fn exception(
    py: Python<'_>,
    module: &str,
    name: &str,
    message: impl for<'py> IntoPyObject<'py>,
) -> PyErr {
    let made = py
        .import(module)
        .and_then(|module| module.getattr(name))
        .and_then(|class| class.call1((message,)));

    match made {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

/// `OSError(errno, strerror, filename)`, which Python turns into the subclass for `errno`.
pub(crate) fn os_error(py: Python<'_>, error: &std::io::Error, path: Option<&Path>) -> PyErr {
    let Some(errno) = error.raw_os_error() else {
        return PyOSError::new_err(error.to_string());
    };

    let made = (|| {
        let strerror = py.import("os")?.call_method1("strerror", (errno,))?;
        // The path as `str`, without loss: bytes that are not UTF-8 become surrogates.
        let filename = path.map(Path::as_os_str);
        let builtins = py.import("builtins")?;
        let error = match filename {
            Some(filename) => builtins
                .getattr("OSError")?
                .call1((errno, strerror, filename))?,
            None => builtins.getattr("OSError")?.call1((errno, strerror))?,
        };
        PyResult::Ok(PyErr::from_value(error))
    })();

    made.unwrap_or_else(|error| error)
}
