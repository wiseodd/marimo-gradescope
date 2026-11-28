//! Commands run as `subprocess.run(..., capture_output=True)` runs them, stoppable by Ctrl+C.

use std::io::{self, Read};
use std::path::PathBuf;
use std::process::{Command, ExitStatus, Stdio};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use mogs_grader::GradeError;
use mogs_grader::process::DROPPED;

use crate::{Interpreter, interrupted};

/// How often a running command checks for Ctrl+C.
const POLL: Duration = Duration::from_millis(50);

/// What a command did.
pub(crate) struct Output {
    pub(crate) status: ExitStatus,
    pub(crate) stdout: Vec<u8>,
    pub(crate) stderr: Vec<u8>,
}

/// Run `command` to its end and capture both streams.
///
/// The environment is the command's without the variables the grader's children drop
/// ([`DROPPED`]). If the host is interrupted, the command is killed and the host's error
/// returned.
pub(crate) fn run<I: Interpreter>(i: &I, command: &mut Command) -> Result<Output, I::Error> {
    let program = PathBuf::from(command.get_program());
    let os = |error| i.grade_error(GradeError::os(error, &program));

    for key in DROPPED {
        command.env_remove(key);
    }

    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(os)?;
    let stdout = drain(child.stdout.take());
    let stderr = drain(child.stderr.take());

    let status = loop {
        if let Some(status) = child.try_wait().map_err(os)? {
            break status;
        }

        if let Err(error) = interrupted(i) {
            // The readers end when the pipes close; they are not waited for.
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }

        thread::sleep(POLL);
    };

    let read = |reader: JoinHandle<io::Result<Vec<u8>>>| {
        reader
            .join()
            .expect("the pipe reader does not panic")
            .map_err(|error| i.grade_error(GradeError::from(error)))
    };

    Ok(Output {
        status,
        stdout: read(stdout)?,
        stderr: read(stderr)?,
    })
}

/// Read all of `pipe` on a thread of its own.
fn drain(pipe: Option<impl Read + Send + 'static>) -> JoinHandle<io::Result<Vec<u8>>> {
    thread::spawn(move || {
        let mut out = Vec::new();

        if let Some(mut pipe) = pipe {
            pipe.read_to_end(&mut out)?;
        }

        Ok(out)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fake::{Fake, spawning};

    fn ran(command: &mut Command) -> Result<Output, String> {
        let _spawning = spawning();
        run(&Fake::default(), command)
    }

    #[test]
    fn captures_both_streams_and_the_status() {
        let mut command = Command::new("sh");
        command.args(["-c", "echo out; echo err >&2; exit 3"]);

        let output = ran(&mut command).unwrap();

        assert_eq!(output.stdout, b"out\n");
        assert_eq!(output.stderr, b"err\n");
        assert_eq!(output.status.code(), Some(3));
    }

    #[test]
    fn drops_the_pytest_variable() {
        let mut command = Command::new("sh");
        command
            .args(["-c", "echo \"${PYTEST_CURRENT_TEST-unset} $KEPT\""])
            .env("PYTEST_CURRENT_TEST", "x")
            .env("KEPT", "kept");

        let output = ran(&mut command).unwrap();

        assert_eq!(output.stdout, b"unset kept\n");
    }

    #[test]
    fn missing_program_is_an_os_error_naming_it() {
        let mut command = Command::new("/no/such/program");

        let error = ran(&mut command).err().unwrap();

        assert!(error.starts_with("OSError: "), "{error}");
        assert!(error.ends_with(": /no/such/program"), "{error}");
    }
}
