//! `mogs edit`: marimo's editor on the source notebook.

use std::path::Path;
use std::process::{Child, Command, ExitStatus};
use std::time::Duration;

use crate::{Call, OsError};

/// How often [`wait`] checks on marimo and calls its `poll`.
pub const POLL: Duration = Duration::from_millis(50);

/// Why `edit` failed.
#[derive(Debug)]
pub enum EditError<E> {
    /// marimo could not be started or waited for.
    Os(OsError),
    /// `poll` failed, as on an exception other than Ctrl+C.
    Python(E),
}

/// `python -m marimo edit --sandbox notebook`.
///
/// marimo runs on mogs' own Python, which has pytest. The editor server compiles the test
/// cells there, and without pytest it logs a warning for each of them.
pub fn command(python: &Path, notebook: &Path) -> Command {
    let mut command = Command::new(python);
    command
        .args(["-m", "marimo", "edit", "--sandbox"])
        .arg(notebook);

    command
}

/// Start marimo on `notebook` with `python`.
///
/// # Errors
///
/// The [`OsError`] of the failed start, naming `python`.
pub fn spawn(python: &Path, notebook: &Path) -> Result<Child, OsError> {
    command(python, notebook)
        .spawn()
        .map_err(|error| OsError::new(error, Some(python), Call::Spawn))
}

/// Wait for `child` to exit, calling `poll` every [`POLL`] while it runs.
///
/// Ctrl+C reaches marimo too, and marimo asks whether to quit, so `poll` should let Ctrl+C
/// pass and keep waiting.
///
/// # Returns
///
/// The exit code as `Popen.wait()` gives it: negative for the signal that ended the child on
/// Unix, the unsigned exit code on Windows.
///
/// # Errors
///
/// [`EditError::Python`] with the first error of `poll`, leaving `child` running, and
/// [`EditError::Os`] if waiting fails.
pub fn wait<E>(
    child: &mut Child,
    mut poll: impl FnMut() -> Result<(), E>,
) -> Result<i64, EditError<E>> {
    loop {
        let exited = child
            .try_wait()
            .map_err(|error| EditError::Os(OsError::new(error, None, Call::Spawn)))?;

        if let Some(status) = exited {
            return Ok(returncode(status));
        }

        poll().map_err(EditError::Python)?;
        std::thread::sleep(POLL);
    }
}

#[cfg(unix)]
fn returncode(status: ExitStatus) -> i64 {
    use std::os::unix::process::ExitStatusExt;

    match (status.code(), status.signal()) {
        (Some(code), _) => code.into(),
        (None, Some(signal)) => -i64::from(signal),
        (None, None) => unreachable!("a child that exited has a code or a signal"),
    }
}

#[cfg(windows)]
fn returncode(status: ExitStatus) -> i64 {
    let code = status.code().expect("a child on Windows exits with a code");

    u32::from_ne_bytes(code.to_ne_bytes()).into()
}

#[cfg(test)]
mod tests {
    use std::ffi::OsStr;

    use super::*;

    #[test]
    fn command_runs_marimo_edit_in_the_sandbox() {
        let command = command(Path::new("/venv/python"), Path::new("hw/hw.py"));

        assert_eq!(command.get_program(), "/venv/python");
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            ["-m", "marimo", "edit", "--sandbox", "hw/hw.py"].map(OsStr::new)
        );
    }

    #[test]
    fn spawn_names_the_python_that_does_not_start() {
        let python = Path::new("/no/such/python");

        let error = spawn(python, Path::new("hw.py")).unwrap_err();

        assert_eq!(error.path.as_deref(), Some(python));
        assert_eq!(error.call, Call::Spawn);
        assert_eq!(error.error.kind(), std::io::ErrorKind::NotFound);
    }

    #[cfg(unix)]
    fn shell(script: &str) -> Child {
        Command::new("sh").args(["-c", script]).spawn().unwrap()
    }

    #[cfg(unix)]
    #[test]
    fn wait_returns_the_exit_code() {
        let mut child = shell("exit 3");

        assert_eq!(wait(&mut child, || Ok::<(), ()>(())).unwrap(), 3);
    }

    #[cfg(unix)]
    #[test]
    fn wait_gives_a_signal_as_a_negative_code() {
        let mut child = shell("kill -9 $$");

        assert_eq!(wait(&mut child, || Ok::<(), ()>(())).unwrap(), -9);
    }

    #[cfg(unix)]
    #[test]
    fn wait_polls_until_the_child_exits() {
        let mut child = shell("sleep 0.3");
        let mut polls = 0;

        let code = wait(&mut child, || {
            polls += 1;
            Ok::<(), ()>(())
        });

        assert_eq!(code.unwrap(), 0);
        assert!(polls >= 2, "{polls}");
    }

    #[cfg(unix)]
    #[test]
    fn wait_stops_on_the_first_poll_error() {
        let mut child = shell("sleep 30");

        let error = wait(&mut child, || Err("stop"));

        assert!(matches!(error, Err(EditError::Python("stop"))));
        assert_eq!(child.try_wait().unwrap(), None);
        child.kill().unwrap();
        child.wait().unwrap();
    }
}
