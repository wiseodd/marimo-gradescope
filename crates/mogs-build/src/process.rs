//! Commands run as `subprocess.run(..., capture_output=True)` runs them, stoppable by Ctrl+C
//! and by a time limit.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::PathBuf;
#[cfg(not(unix))]
use std::process::Stdio;
use std::process::{Child, Command, ExitStatus};
use std::thread;
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
/// Ctrl+C would (`SIGTERM` on Unix), and killed if it has not ended [`GRACE`] later. Stopping
/// it stops what it started too: on Unix it runs in a process group of its own, and on Windows
/// `taskkill /T` ends its process tree. On Unix, what it left running when it ends is killed.
/// Its output goes to files, so a process it leaves behind cannot hold the run open.
pub(crate) fn run_within<I: Interpreter>(
    i: &I,
    command: &mut Command,
    limit: Option<Duration>,
) -> Result<Option<Output>, I::Error> {
    let program = PathBuf::from(command.get_program());
    let os = |error| i.os(error, &program);
    let io = |error| i.io(error);

    for key in DROPPED {
        command.env_remove(key);
    }

    let mut stdout = tempfile::tempfile().map_err(io)?;
    let mut stderr = tempfile::tempfile().map_err(io)?;
    command
        .stdout(stdout.try_clone().map_err(io)?)
        .stderr(stderr.try_clone().map_err(io)?);

    #[cfg(unix)]
    std::os::unix::process::CommandExt::process_group(command, 0);

    let mut child = command.spawn().map_err(os)?;
    let deadline = limit.map(|limit| Instant::now() + limit);

    let status = loop {
        if let Some(status) = child.try_wait().map_err(os)? {
            break status;
        }

        if let Err(error) = interrupted(i) {
            kill(&mut child);
            return Err(error);
        }

        if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
            stop(&mut child);
            return Ok(None);
        }

        thread::sleep(POLL);
    };

    #[cfg(unix)]
    signal_group(&child, rustix::process::Signal::KILL);

    let read = |file: &mut File| {
        let mut out = Vec::new();
        file.seek(SeekFrom::Start(0))?;
        file.read_to_end(&mut out)?;
        Ok(out)
    };

    Ok(Some(Output {
        status,
        stdout: read(&mut stdout).map_err(io)?,
        stderr: read(&mut stderr).map_err(io)?,
    }))
}

/// Ask `child` and what it started to end, then kill them if `child` has not ended [`GRACE`]
/// later.
fn stop(child: &mut Child) {
    #[cfg(unix)]
    signal_group(child, rustix::process::Signal::TERM);

    #[cfg(not(unix))]
    kill(child);

    let deadline = Instant::now() + GRACE;

    while Instant::now() < deadline {
        if !matches!(child.try_wait(), Ok(None)) {
            break;
        }

        thread::sleep(POLL);
    }

    kill(child);
}

/// Kill `child` and what it started, and reap `child`.
fn kill(child: &mut Child) {
    #[cfg(unix)]
    signal_group(child, rustix::process::Signal::KILL);

    // Windows has no process groups to signal; `taskkill /T` ends the whole tree.
    #[cfg(not(unix))]
    let _ = Command::new("taskkill")
        .args(["/T", "/F", "/PID", &child.id().to_string()])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();

    let _ = child.kill();
    let _ = child.wait();
}

/// Send `signal` to the process group that `child` leads.
#[cfg(unix)]
fn signal_group(child: &Child, signal: rustix::process::Signal) {
    let pid = rustix::process::Pid::from_child(child);
    let _ = rustix::process::kill_process_group(pid, signal);
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

    #[test]
    fn background_process_does_not_hold_the_run() {
        let mut command = Command::new("sh");
        command.args(["-c", "sleep 30 & echo done"]);
        let started = Instant::now();

        let output = ran(&mut command).unwrap();

        assert_eq!(output.stdout, b"done\n");
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn stopping_a_command_stops_what_it_started() {
        let folder = tempfile::tempdir().unwrap();
        let pid_file = folder.path().join("pid");
        let mut command = Command::new("sh");
        command
            .args(["-c", "sleep 30 & echo $! > \"$1\"; wait", "sh"])
            .arg(&pid_file);

        let _spawning = spawning();
        let output = run_within(
            &Fake::default(),
            &mut command,
            Some(Duration::from_millis(500)),
        );

        assert!(output.unwrap().is_none());
        let pid: i32 = std::fs::read_to_string(&pid_file)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        assert!(gone(pid), "process {pid} still runs");
    }

    /// Whether process `pid` has ended, waiting up to 2 s for it to be reaped.
    fn gone(pid: i32) -> bool {
        let pid = rustix::process::Pid::from_raw(pid).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);

        while Instant::now() < deadline {
            if rustix::process::test_kill_process(pid).is_err() {
                return true;
            }

            thread::sleep(POLL);
        }

        false
    }
}
