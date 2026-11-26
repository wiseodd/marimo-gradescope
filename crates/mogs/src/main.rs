//! The `mogs` command: runs `python -P -m mogs.cli` with the interpreter installed next to it.

mod python;

use std::process::{Command, ExitCode};

fn main() -> ExitCode {
    let python = match python::interpreter() {
        Ok(python) => python,
        Err(error) => {
            eprintln!("{error}");
            return ExitCode::FAILURE;
        }
    };

    // `-P` keeps the working directory off `sys.path`, as for a console script.
    let mut command = Command::new(&python);
    command
        .args(["-P", "-m", "mogs.cli"])
        .args(std::env::args_os().skip(1));

    let error = run(command);
    eprintln!("Cannot run {}: {error}", python.display());

    ExitCode::FAILURE
}

/// Replace this process with `command`. Returns only on failure.
#[cfg(unix)]
fn run(mut command: Command) -> std::io::Error {
    use std::os::unix::process::CommandExt;

    command.exec()
}

/// Run `command` and exit with its exit code. Returns only on failure.
///
/// Ctrl+C reaches the child too, so this process ignores it and waits for the child to exit.
#[cfg(windows)]
fn run(mut command: Command) -> std::io::Error {
    use windows_sys::Win32::Foundation::TRUE;
    use windows_sys::Win32::System::Console::SetConsoleCtrlHandler;
    use windows_sys::core::BOOL;

    unsafe extern "system" fn ignore(_event: u32) -> BOOL {
        TRUE
    }

    // SAFETY: `ignore` is a valid handler for the life of the process.
    unsafe { SetConsoleCtrlHandler(Some(ignore), TRUE) };

    match command.status() {
        Ok(status) => std::process::exit(status.code().unwrap_or(1)),
        Err(error) => error,
    }
}
