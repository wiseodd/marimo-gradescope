//! Commands run as `subprocess.run(..., capture_output=True)` runs them, stoppable by Ctrl+C
//! and by a time limit.

use std::io::{self, Read};
use std::path::PathBuf;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crate::{Interpreter, interrupted};

/// How often a running command checks for Ctrl+C and its time limit.
const POLL: Duration = Duration::from_millis(50);

/// How long a command over its time limit has to end after it is asked to, before it is
/// killed.
const GRACE: Duration = Duration::from_secs(5);

/// The variables no process that mogs starts keeps, as `mogs_grader::process::DROPPED`,
/// which exists only where the grader runs. `PYTEST_CURRENT_TEST` makes marimo act as if under
/// test.
const DROPPED: [&str; 1] = ["PYTEST_CURRENT_TEST"];

/// What a command did.
pub(crate) struct Output {
    pub(crate) status: ExitStatus,
    pub(crate) stdout: Vec<u8>,
    pub(crate) stderr: Vec<u8>,
}

/// Run `command` to its end and capture both streams.
///
/// The environment is the command's without [`DROPPED`]. If the host is interrupted, the
/// command is killed and the host's error returned.
pub(crate) fn run<I: Interpreter>(i: &I, command: &mut Command) -> Result<Output, I::Error> {
    let output = run_within(i, command, None)?;

    Ok(output.expect("a command without a time limit finishes"))
}

/// Run `command` as [`run`] does, for at most `limit`.
///
/// Returns `None` when the command is still running at `limit`. It is then asked to end, as
/// Ctrl+C would (`SIGTERM` on Unix, which `uv run` passes on to its child), and killed if it
/// has not ended [`GRACE`] later.
pub(crate) fn run_within<I: Interpreter>(
    i: &I,
    command: &mut Command,
    limit: Option<Duration>,
) -> Result<Option<Output>, I::Error> {
    let program = PathBuf::from(command.get_program());
    let os = |error| i.os(error, &program);

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
    let deadline = limit.map(|limit| Instant::now() + limit);

    // The readers end when the pipes close; they are not waited for after a stop, since a
    // child of the command may hold them open.
    let status = loop {
        if let Some(status) = child.try_wait().map_err(os)? {
            break status;
        }

        if let Err(error) = interrupted(i) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }

        if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
            stop(&mut child);
            return Ok(None);
        }

        thread::sleep(POLL);
    };

    let read = |reader: JoinHandle<io::Result<Vec<u8>>>| {
        reader
            .join()
            .expect("the pipe reader does not panic")
            .map_err(|error| i.io(error))
    };

    Ok(Some(Output {
        status,
        stdout: read(stdout)?,
        stderr: read(stderr)?,
    }))
}

/// Ask `child` to end, then kill it if it has not ended [`GRACE`] later.
fn stop(child: &mut Child) {
    terminate(child);
    let deadline = Instant::now() + GRACE;

    while Instant::now() < deadline {
        if !matches!(child.try_wait(), Ok(None)) {
            return;
        }

        thread::sleep(POLL);
    }

    let _ = child.kill();
    let _ = child.wait();
}

/// Send `SIGTERM` to `child`.
#[cfg(unix)]
fn terminate(child: &mut Child) {
    let pid = rustix::process::Pid::from_child(child);
    let _ = rustix::process::kill_process(pid, rustix::process::Signal::TERM);
}

/// Kill `child`: Windows has no signal to ask a process to end.
#[cfg(not(unix))]
fn terminate(child: &mut Child) {
    let _ = child.kill();
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

#[cfg(all(test, unix))]
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

    #[test]
    fn command_over_its_limit_is_stopped() {
        let mut command = Command::new("sh");
        command.args(["-c", "exec sleep 30"]);
        let started = Instant::now();

        let _spawning = spawning();
        let output = run_within(
            &Fake::default(),
            &mut command,
            Some(Duration::from_millis(200)),
        );

        assert!(output.unwrap().is_none());
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn command_that_ignores_the_request_to_end_is_killed() {
        let mut command = Command::new("sh");
        command.args(["-c", "trap '' TERM; while :; do sleep 0.1; done"]);
        let started = Instant::now();

        let _spawning = spawning();
        let output = run_within(
            &Fake::default(),
            &mut command,
            Some(Duration::from_millis(200)),
        );

        assert!(output.unwrap().is_none());
        assert!(started.elapsed() >= GRACE);
    }

    #[test]
    fn command_within_its_limit_finishes() {
        let mut command = Command::new("sh");
        command.args(["-c", "echo done"]);

        let _spawning = spawning();
        let output = run_within(
            &Fake::default(),
            &mut command,
            Some(Duration::from_secs(30)),
        );

        assert_eq!(output.unwrap().unwrap().stdout, b"done\n");
    }
}
