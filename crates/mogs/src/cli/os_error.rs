//! A failed system call of `mogs-cli` as the `OSError` Python would raise for it.

use mogs_cli::OsError;
use pyo3::exceptions::PyOSError;
use pyo3::prelude::*;

/// The `OSError` for `error`, of the subclass Python picks for its code, with Python's text.
pub(super) fn os_error(py: Python<'_>, error: &OsError) -> PyErr {
    let Some(code) = error.error.raw_os_error() else {
        return PyOSError::new_err(error.error.to_string());
    };

    made(py, code, error).unwrap_or_else(|error| error)
}

/// `OSError(errno, os.strerror(errno), path)`, as Python raises for a failed call.
#[cfg(not(windows))]
fn made(py: Python<'_>, errno: i32, error: &OsError) -> PyResult<PyErr> {
    by_errno(py, errno, error)
}

/// On Windows, by the C runtime's `errno` for a file, else by the Windows error code.
#[cfg(windows)]
fn made(py: Python<'_>, winerror: i32, error: &OsError) -> PyResult<PyErr> {
    use mogs_cli::Call;

    let class = py.import("builtins")?.getattr("OSError")?;

    match error.call {
        Call::File => {
            // The `errno` that Python maps the Windows error code to.
            let errno: i32 = class
                .call1((py.None(), py.None(), py.None(), winerror))?
                .getattr("errno")?
                .extract()?;
            by_errno(py, errno, error)
        }
        Call::Folder | Call::Spawn => {
            // `Popen` names no path in its Windows errors.
            let filename = match error.call {
                Call::Folder => error.path.as_deref().map(std::path::Path::as_os_str),
                Call::File | Call::Spawn => None,
            };
            let strerror = windows_message(py, winerror)?;
            let raised = class.call1((py.None(), strerror, filename, winerror))?;
            Ok(PyErr::from_value(raised))
        }
    }
}

/// `OSError(errno, os.strerror(errno))`, with the path when `error` has one.
fn by_errno(py: Python<'_>, errno: i32, error: &OsError) -> PyResult<PyErr> {
    let strerror = py.import("os")?.call_method1("strerror", (errno,))?;
    let class = py.import("builtins")?.getattr("OSError")?;
    let raised = match &error.path {
        Some(path) => class.call1((errno, strerror, path.as_os_str()))?,
        None => class.call1((errno, strerror))?,
    };

    Ok(PyErr::from_value(raised))
}

/// The system's message for a Windows error code, trimmed as Python trims it: no trailing
/// white space or dots.
#[cfg(windows)]
fn windows_message(py: Python<'_>, winerror: i32) -> PyResult<String> {
    let message: String = py
        .import("ctypes")?
        .call_method1("FormatError", (winerror,))?
        .extract()?;

    Ok(message
        .trim_end_matches(|c: char| c <= ' ' || c == '.')
        .to_owned())
}
