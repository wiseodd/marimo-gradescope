//! Operating-system side of the sandbox: start, watch and kill one grading child.
//!
//! Linux and macOS. The child runs in its own session, so killing its process group also kills
//! the processes it started, unless they left the group; with a dedicated user, `pkill -u` gets
//! those too. macOS rejects `RLIMIT_AS`, so there the child has no memory cap.

use std::ffi::{OsStr, OsString};
use std::fs::File;
use std::io::{self, Read, Seek, Write};
use std::os::fd::{AsFd, AsRawFd, OwnedFd};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use mogs_schema::{EventLine, WireError};
use rustix::event::{PollFd, PollFlags, Timespec, poll};
use rustix::fs::{OFlags, fcntl_getfl, fcntl_setfl};
use rustix::io::{Errno, FdFlags, fcntl_setfd};
use rustix::process::{
    Pid, Resource, Rlimit, Signal, WaitId, WaitIdOptions, getrlimit, kill_process_group, setrlimit,
    setsid, waitid,
};

use crate::{GradeError, GradingUser, PyText};

/// Bytes of a child's stdout and stderr that are kept for the log.
pub const OUTPUT_LIMIT: usize = 64 * 1024;
/// Longest event line a child may write.
pub const LINE_LIMIT: usize = 1024 * 1024;
/// Most processes a grading user may run.
pub const NPROC_LIMIT: u64 = 512;
/// Longest wait for events before the deadlines are checked again.
pub const POLL: Duration = Duration::from_millis(50);
/// Most `pkill` passes over a grading user's processes.
pub const KILL_PASSES: usize = 5;
/// How a child that `SIGKILL` ended is described: the kernel's OOM killer sends it.
pub const OOM_MESSAGE: &str =
    "killed, probably out of memory: raise the Gradescope container memory";

/// How long the output thread waits for data before it checks whether to stop.
const DRAIN_POLL: Duration = Duration::from_millis(100);
const CHUNK: usize = 65536;

/// A resource limit set in the child before it starts.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Limit {
    /// The resource.
    pub resource: Resource,
    /// Soft and hard limit, at most the current hard limit.
    pub value: u64,
}

/// How to start a child, apart from what every child shares.
#[derive(Clone, Debug)]
pub struct LaunchOptions {
    /// Working directory.
    pub cwd: PathBuf,
    /// Environment.
    pub env: Vec<(OsString, OsString)>,
    /// User to run as, or `None` to keep the current one. Supplementary groups are dropped.
    pub user: Option<GradingUser>,
    /// Resource limits set before the child starts.
    pub limits: Vec<Limit>,
    /// Interpreter to run the child with.
    pub python: OsString,
}

/// The command line of a grading child that runs on `python` and writes events to `event_fd`.
pub fn child_command(event_fd: i32, python: &OsStr) -> Vec<OsString> {
    let args = ["-m", "mogs.grader.child", &event_fd.to_string()].map(OsString::from);

    std::iter::once(python.to_os_string()).chain(args).collect()
}

/// `base`, the current environment, made fit for student code that runs in `workdir`.
///
/// It drops `PYTEST_CURRENT_TEST`, which makes marimo act as if under test, and sets
/// `MOGS_GRADING=1`, so `check` and `export` cells do nothing when the notebook runs.
pub fn child_env(
    base: impl IntoIterator<Item = (OsString, OsString)>,
    workdir: &Path,
) -> Vec<(OsString, OsString)> {
    let workdir = workdir.as_os_str();
    let set: [(&str, &OsStr); 6] = [
        ("MOGS_GRADING", OsStr::new("1")),
        ("MPLBACKEND", OsStr::new("Agg")),
        ("OMP_NUM_THREADS", OsStr::new("4")),
        ("HOME", workdir),
        ("MPLCONFIGDIR", workdir),
        ("PYTHONDONTWRITEBYTECODE", OsStr::new("1")),
    ];
    let mut env: Vec<(OsString, OsString)> = base
        .into_iter()
        .filter(|(key, _)| key != "PYTEST_CURRENT_TEST" && !set.iter().any(|(k, _)| key == k))
        .collect();

    env.extend(set.map(|(key, value)| (key.into(), value.to_os_string())));
    env
}

/// Resource limits for a child.
///
/// The process cap counts every process of the real user, so it applies only to a dedicated
/// grading user; on a developer machine it would count the developer's own processes. The
/// memory cap is Linux only: macOS fails `setrlimit(RLIMIT_AS, ...)`, so a self-check on macOS
/// runs without one.
pub fn limits(memory_mb: u32, user: bool) -> Vec<Limit> {
    limits_on(cfg!(target_os = "linux"), memory_mb, user)
}

fn limits_on(linux: bool, memory_mb: u32, user: bool) -> Vec<Limit> {
    let mut result = Vec::new();

    if linux {
        let value = u64::from(memory_mb) * 1024 * 1024;
        result.push(Limit {
            resource: Resource::As,
            value,
        });
    }

    if user {
        result.push(Limit {
            resource: Resource::Nproc,
            value: NPROC_LIMIT,
        });
    }

    result
}

/// Soft and hard limits of `value` for `resource`, at most the current hard limit.
pub fn clamped(resource: Resource, value: u64) -> Rlimit {
    let value = match getrlimit(resource).maximum {
        Some(hard) => value.min(hard),
        None => value,
    };

    Rlimit {
        current: Some(value),
        maximum: Some(value),
    }
}

/// Check that `user` may run student code.
///
/// # Errors
///
/// [`GradeError::Value`] if `user` is root or the user running the grader, since `pkill -u`
/// would then kill the grader, and student code would get the grader's rights.
pub fn check_user(user: &GradingUser, text: &dyn PyText) -> Result<(), GradeError> {
    let name = text.repr(&user.name);

    if user.uid == 0 {
        return Err(GradeError::Value(format!(
            "the grading user must not be root: {name} has uid 0"
        )));
    }

    if user.uid == rustix::process::getuid().as_raw() {
        return Err(GradeError::Value(format!(
            "the grading user must not be the user running the grader: {name}"
        )));
    }

    Ok(())
}

/// Options for a child that runs on `python` in `workdir`, as `user` when set.
pub fn launch_options(
    workdir: &Path,
    user: Option<&GradingUser>,
    memory_mb: u32,
    python: &OsStr,
) -> LaunchOptions {
    LaunchOptions {
        cwd: workdir.to_path_buf(),
        env: child_env(std::env::vars_os(), workdir),
        user: user.cloned(),
        limits: limits(memory_mb, user.is_some()),
        python: python.to_os_string(),
    }
}

/// How a child ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Exit {
    /// It exited with this code.
    Code(i32),
    /// This signal killed it.
    Signal(i32),
}

impl From<ExitStatus> for Exit {
    fn from(status: ExitStatus) -> Self {
        match (status.code(), status.signal()) {
            (Some(code), _) => Exit::Code(code),
            (None, Some(signal)) => Exit::Signal(signal),
            (None, None) => Exit::Code(-1),
        }
    }
}

/// How a child ended, for a failure message.
pub fn describe_exit(exit: Exit, text: &dyn PyText) -> String {
    match exit {
        Exit::Signal(signal) if signal == Signal::KILL.as_raw() => OOM_MESSAGE.into(),
        Exit::Signal(signal) => format!("killed by signal {}", text.signal_name(signal)),
        Exit::Code(code) => format!("exited with code {code}"),
    }
}

/// Kill every process of `user` with `pkill`.
///
/// Repeats, up to `KILL_PASSES` times, while `pgrep` still finds processes, which a fork
/// bomb can start between two passes.
///
/// # Errors
///
/// If `pkill` or `pgrep` cannot run.
pub fn kill_user(user: &str) -> Result<(), GradeError> {
    kill_user_with(user, |argv| {
        let status = Command::new(argv[0])
            .args(&argv[1..])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map_err(|error| GradeError::os(error, argv[0]))?;

        Ok(status.code())
    })
}

/// `kill_user`, running each command with `run`, which gives its exit code.
fn kill_user_with(
    user: &str,
    mut run: impl FnMut(&[&str]) -> Result<Option<i32>, GradeError>,
) -> Result<(), GradeError> {
    for _ in 0..KILL_PASSES {
        run(&["pkill", "-KILL", "-u", user])?;

        // `pgrep` exits with 1 when no process matches.
        if run(&["pgrep", "-u", user])? == Some(1) {
            return Ok(());
        }
    }

    Ok(())
}

/// A running grading child: its process, its event channel and the tail of its output.
///
/// Events are read with `poll` and deadlines, never up to end of file: a process the child
/// forks keeps the write end open after the child is gone.
pub struct Child {
    process: std::process::Child,
    user: Option<String>,
    events: Option<OwnedFd>,
    buffer: Vec<u8>,
    output: Option<Tail>,
    /// How the child ended, once it has.
    pub exit: Option<Exit>,
    /// Why its event channel broke the protocol, if it did.
    pub error: Option<String>,
}

impl Child {
    /// Start a child that reads `job`, a `Job` as JSON, from stdin.
    ///
    /// # Errors
    ///
    /// If a pipe or the job file cannot be made, or the child cannot start.
    pub fn spawn(job: &str, options: &LaunchOptions) -> Result<Child, GradeError> {
        let (read, write) = io::pipe()?;
        let (output, output_write) = io::pipe()?;
        let read = OwnedFd::from(read);
        let write = OwnedFd::from(write);
        set_nonblocking(&read)?;

        // Stdin is a file, so the parent never blocks on a job the child does not read.
        let mut stdin = tempfile::tempfile()?;
        stdin.write_all(job.as_bytes())?;
        stdin.rewind()?;

        let argv = child_command(write.as_raw_fd(), &options.python);
        let mut command = Command::new(&argv[0]);
        command
            .args(&argv[1..])
            .stdin(stdin)
            .stdout(output_write.try_clone()?)
            .stderr(output_write)
            .current_dir(&options.cwd)
            .env_clear()
            .envs(options.env.iter().map(|(key, value)| (key, value)));

        if let Some(user) = &options.user {
            // As root, std drops the supplementary groups when it changes the user.
            command.uid(user.uid).gid(user.gid);
        }

        let limits = options.limits.clone();
        let keep = write.as_raw_fd();
        let last = open_fd_bound();
        // SAFETY: the hook runs between fork and exec, where only async-signal-safe calls are
        // allowed. It makes only raw system calls (setsid, getrlimit, setrlimit, fcntl,
        // close_range) and allocates nothing: `limits`, `keep`, `last` and `write` were moved
        // in before the fork. `fcntl` on a descriptor that is not open fails with EBADF, which
        // is ignored.
        unsafe {
            command.pre_exec(move || {
                setsid()?;

                for limit in &limits {
                    setrlimit(limit.resource, clamped(limit.resource, limit.value))?;
                }

                // Only stdin, stdout, stderr and the event channel reach the child, as with
                // `close_fds=True`. Close on exec rather than close now, so std still hears
                // back from a failed exec.
                #[cfg(target_os = "linux")]
                let marked = {
                    let flags = libc::CLOSE_RANGE_CLOEXEC;
                    let range = |first: libc::c_uint, last: libc::c_uint| {
                        first > last
                            || libc::syscall(libc::SYS_close_range, first, last, flags) == 0
                    };
                    // `keep` is never below 3 in practice; the ranges skip stdio either way.
                    match libc::c_uint::try_from(keep) {
                        Ok(keep) if keep >= 3 => {
                            range(3, keep - 1) && range(keep + 1, libc::c_uint::MAX)
                        }
                        _ => range(3, libc::c_uint::MAX),
                    }
                };
                #[cfg(not(target_os = "linux"))]
                let marked = false;

                if !marked {
                    for fd in (3..last).filter(|fd| *fd != keep) {
                        libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC);
                    }
                }

                // Keep the event channel open across exec, as `pass_fds` does.
                fcntl_setfd(&write, FdFlags::empty())?;
                Ok(())
            });
        }

        let process = command
            .spawn()
            .map_err(|error| GradeError::os(error, &options.python))?;
        // Close the parent's copies of the write ends.
        drop(command);

        Ok(Child {
            process,
            user: options.user.as_ref().map(|user| user.name.clone()),
            events: Some(read),
            buffer: Vec::new(),
            output: Some(Tail::start(OwnedFd::from(output))?),
            exit: None,
            error: None,
        })
    }

    /// Events that arrive within `POLL`, or before `until`.
    ///
    /// Sets `exit` once the child has ended. When the child writes a line that is not an
    /// event, sets `error` and returns the events before that line; later lines are dropped.
    ///
    /// # Errors
    ///
    /// If the child's process group cannot be killed once it has ended.
    pub fn wait(
        &mut self,
        until: Instant,
        text: &dyn PyText,
    ) -> Result<Vec<EventLine>, GradeError> {
        let wait = POLL.min(until.saturating_duration_since(Instant::now()));

        match &self.events {
            None => thread::sleep(wait),
            Some(fd) => {
                let mut fds = [PollFd::new(fd, PollFlags::IN)];
                let timeout = Timespec::try_from(wait).unwrap_or(Timespec {
                    tv_sec: 0,
                    tv_nsec: 0,
                });

                match poll(&mut fds, Some(&timeout)) {
                    Ok(_) | Err(Errno::INTR) => {}
                    Err(errno) => return Err(errno.into()),
                }
            }
        }

        let mut events = self.read(text)?;

        if self.ended()? {
            // Until reaped, the child is a zombie and its process group id stays reserved.
            self.kill_group()?;
            self.exit = Some(self.process.wait()?.into());
            events.extend(self.read(text)?);
        }

        Ok(events)
    }

    /// Kill the child and everything it started, and release its resources.
    ///
    /// Returns the last `OUTPUT_LIMIT` bytes of its stdout and stderr, decoded.
    ///
    /// # Errors
    ///
    /// If its processes cannot be killed.
    pub fn close(&mut self) -> Result<String, GradeError> {
        // Once the child is reaped, its process group id may belong to someone else.
        if self.exit.is_none() {
            self.kill_group()?;
        }

        if let Some(user) = &self.user {
            kill_user(user)?;
        }

        self.exit = Some(self.process.wait()?.into());
        self.events = None;

        match self.output.take() {
            Some(output) => output.close(),
            None => Ok(String::new()),
        }
    }

    fn read(&mut self, text: &dyn PyText) -> Result<Vec<EventLine>, GradeError> {
        let Some(fd) = &self.events else {
            return Ok(Vec::new());
        };

        if self.error.is_some() {
            return Ok(Vec::new());
        }

        // Bounded, so a child that writes without end cannot keep the parent from its deadlines.
        let mut chunk = vec![0; CHUNK];
        let mut closed = false;

        for _ in 0..LINE_LIMIT / CHUNK {
            match rustix::io::read(fd, &mut chunk) {
                Ok(0) => {
                    closed = true;
                    break;
                }
                Ok(count) => self.buffer.extend_from_slice(&chunk[..count]),
                Err(Errno::INTR) => {}
                Err(Errno::AGAIN) => break,
                Err(errno) => return Err(errno.into()),
            }
        }

        if closed {
            self.events = None;
        }

        let mut events = Vec::new();
        // The complete lines, without the newline that ends the last one.
        let complete: Option<Vec<u8>> = self
            .buffer
            .iter()
            .rposition(|&byte| byte == b'\n')
            .map(|end| self.buffer.drain(..=end).take(end).collect());
        let lines = complete
            .iter()
            .flat_map(|lines| lines.split(|&byte| byte == b'\n'));

        // Events before a bad line still count, so blame goes to what the child was doing.
        for line in lines {
            match decode(&String::from_utf8_lossy(line), text) {
                Ok(event) => events.push(event),
                Err(error) => {
                    self.error = Some(error);
                    return Ok(events);
                }
            }
        }

        if self.buffer.len() > LINE_LIMIT {
            self.error = Some(format!("an event line is longer than {LINE_LIMIT} bytes"));
        }

        Ok(events)
    }

    /// Whether the child has ended, without reaping it.
    fn ended(&self) -> Result<bool, GradeError> {
        ended(Pid::from_child(&self.process))
    }

    /// Kill the child's process group, if anything is left in it.
    fn kill_group(&self) -> Result<(), GradeError> {
        kill_group_with(Pid::from_child(&self.process), |pid| {
            kill_process_group(pid, Signal::KILL)
        })
    }
}

impl Drop for Child {
    fn drop(&mut self) {
        if self.exit.is_none() {
            let _ = self.close();
        }
    }
}

/// One past the highest descriptor number this process may have open: the soft `RLIMIT_NOFILE`,
/// at most `2^20` when it is unlimited.
fn open_fd_bound() -> i32 {
    let most = 1 << 20;

    getrlimit(Resource::Nofile)
        .current
        .map_or(most, |soft| soft.min(most as u64) as i32)
}

/// Whether the process `pid` has ended, without reaping it.
fn ended(pid: Pid) -> Result<bool, GradeError> {
    let options = WaitIdOptions::EXITED | WaitIdOptions::NOHANG | WaitIdOptions::NOWAIT;

    match waitid(WaitId::Pid(pid), options) {
        Ok(found) => Ok(found.is_some()),
        // Already reaped.
        Err(Errno::CHILD) => Ok(true),
        Err(errno) => Err(errno.into()),
    }
}

/// Kill the process group `pid` with `kill`, ignoring a group that is gone.
fn kill_group_with(pid: Pid, kill: impl Fn(Pid) -> Result<(), Errno>) -> Result<(), GradeError> {
    match kill(pid) {
        Ok(()) | Err(Errno::SRCH) => Ok(()),
        // macOS answers EPERM, not ESRCH, when every process left in the group is a zombie,
        // as when the ended child is not reaped yet. While the child runs, EPERM is a real
        // problem: the parent may not signal a process in the group.
        Err(Errno::PERM) if ended(pid)? => Ok(()),
        Err(errno) => Err(errno.into()),
    }
}

/// Parse one event line.
///
/// The line may come from hostile code, so any failure to parse or validate it gives the
/// reason, for the log.
fn decode(line: &str, text: &dyn PyText) -> Result<EventLine, String> {
    let excerpt = || {
        let end = line
            .char_indices()
            .nth(200)
            .map_or(line.len(), |(at, _)| at);
        let more = if end < line.len() { "..." } else { "" };
        text.repr(&line[..end]) + more
    };

    match mogs_schema::decode::<EventLine>(line) {
        Ok(event) => Ok(event),
        Err(WireError::Json(error)) if error.is_syntax() || error.is_eof() => {
            Err(format!("not JSON: {}", excerpt()))
        }
        Err(WireError::Json(error)) => Err(format!("not an event ({error}): {}", excerpt())),
        Err(error) => Err(error.to_string()),
    }
}

fn set_nonblocking(fd: &impl AsFd) -> Result<(), GradeError> {
    let flags = fcntl_getfl(fd)?;
    fcntl_setfl(fd, flags | OFlags::NONBLOCK)?;

    Ok(())
}

/// Drains a pipe on a thread and keeps its last `OUTPUT_LIMIT` bytes.
struct Tail {
    stop: Arc<AtomicBool>,
    thread: JoinHandle<io::Result<Vec<u8>>>,
}

impl Tail {
    fn start(pipe: OwnedFd) -> Result<Tail, GradeError> {
        set_nonblocking(&pipe)?;
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = Arc::clone(&stop);
        let thread = thread::spawn(move || drain(&File::from(pipe), &stopped));

        Ok(Tail { stop, thread })
    }

    /// Stop draining, close the pipe and return what was kept.
    ///
    /// A panic of the drain thread goes on in the caller.
    fn close(self) -> Result<String, GradeError> {
        self.stop.store(true, Ordering::Relaxed);
        let data = match self.thread.join() {
            Ok(data) => data?,
            Err(panic) => std::panic::resume_unwind(panic),
        };

        Ok(String::from_utf8_lossy(&data).into_owned())
    }
}

fn drain(mut pipe: &File, stop: &AtomicBool) -> io::Result<Vec<u8>> {
    let mut data = Vec::new();
    let mut chunk = vec![0; CHUNK];
    let timeout = Timespec::try_from(DRAIN_POLL).expect("a short timeout fits");

    while !stop.load(Ordering::Relaxed) {
        let mut fds = [PollFd::new(&pipe, PollFlags::IN)];

        match poll(&mut fds, Some(&timeout)) {
            Ok(0) | Err(Errno::INTR) => continue,
            Ok(_) => {}
            Err(errno) => return Err(errno.into()),
        }

        match pipe.read(&mut chunk) {
            Ok(0) => break,
            Ok(count) => data.extend_from_slice(&chunk[..count]),
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) =>
            {
                continue;
            }
            Err(error) => return Err(error),
        }

        if data.len() > OUTPUT_LIMIT {
            data.drain(..data.len() - OUTPUT_LIMIT);
        }
    }

    Ok(data)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fake::FakeHost;

    #[test]
    fn exit_status_messages() {
        let host = FakeHost::default();

        assert_eq!(describe_exit(Exit::Signal(9), &host), OOM_MESSAGE);
        assert_eq!(
            describe_exit(Exit::Signal(11), &host),
            "killed by signal SIGSEGV"
        );
        assert_eq!(describe_exit(Exit::Code(3), &host), "exited with code 3");
        assert_eq!(describe_exit(Exit::Code(0), &host), "exited with code 0");
    }

    #[test]
    fn limits_cap_memory_on_linux_and_processes_for_a_user() {
        let memory = Limit {
            resource: Resource::As,
            value: 512 * 1024 * 1024,
        };
        let processes = Limit {
            resource: Resource::Nproc,
            value: 512,
        };

        assert_eq!(limits_on(true, 512, false), [memory]);
        assert_eq!(limits_on(true, 512, true), [memory, processes]);
        // macOS rejects RLIMIT_AS in setrlimit.
        assert_eq!(limits_on(false, 512, false), []);
        assert_eq!(limits_on(false, 512, true), [processes]);
    }

    #[test]
    fn limits_never_exceed_the_hard_limit() {
        // macOS leaves the hard limit on open files unlimited; Linux sets one.
        match getrlimit(Resource::Nofile).maximum {
            Some(hard) => assert_eq!(clamped(Resource::Nofile, hard + 1).current, Some(hard)),
            None => assert_eq!(clamped(Resource::Nofile, 1 << 40).current, Some(1 << 40)),
        }

        assert_eq!(clamped(Resource::Nofile, 16).current, Some(16));
        assert_eq!(clamped(Resource::Nofile, 16).maximum, Some(16));
    }

    #[test]
    fn child_environment() {
        let base = [
            ("PYTEST_CURRENT_TEST", "x"),
            ("MOGS_KEPT", "1"),
            ("HOME", "/home/me"),
        ]
        .map(|(key, value)| (OsString::from(key), OsString::from(value)));

        let env = child_env(base, Path::new("/work"));

        let get = |key: &str| {
            let found: Vec<&OsString> = env
                .iter()
                .filter(|(k, _)| k == key)
                .map(|(_, v)| v)
                .collect();
            match found.as_slice() {
                [value] => Some(value.to_str().unwrap().to_string()),
                [] => None,
                _ => panic!("{key} is set twice"),
            }
        };
        assert_eq!(get("PYTEST_CURRENT_TEST"), None);
        assert_eq!(get("MOGS_KEPT").as_deref(), Some("1"));
        assert_eq!(get("MPLBACKEND").as_deref(), Some("Agg"));
        assert_eq!(get("OMP_NUM_THREADS").as_deref(), Some("4"));
        assert_eq!(get("HOME").as_deref(), Some("/work"));
        assert_eq!(get("MPLCONFIGDIR").as_deref(), Some("/work"));
        assert_eq!(get("PYTHONDONTWRITEBYTECODE").as_deref(), Some("1"));
        assert_eq!(get("MOGS_GRADING").as_deref(), Some("1"));
    }

    #[test]
    fn launch_without_user() {
        let options = launch_options(
            Path::new("/work"),
            None,
            1024,
            OsStr::new("/env/bin/python"),
        );

        assert_eq!(options.cwd, Path::new("/work"));
        assert_eq!(options.python, "/env/bin/python");
        assert_eq!(options.user, None);
        assert_eq!(options.limits, limits(1024, false));
        assert_eq!(
            child_command(7, OsStr::new("/env/bin/python")),
            ["/env/bin/python", "-m", "mogs.grader.child", "7"]
        );
    }

    #[test]
    fn launch_with_user() {
        let user = GradingUser {
            name: "grader".into(),
            uid: 4242,
            gid: 4343,
        };

        let options = launch_options(Path::new("/work"), Some(&user), 1024, OsStr::new("py"));

        assert_eq!(options.user, Some(user));
        assert_eq!(options.limits, limits(1024, true));
    }

    #[test]
    fn root_and_own_user_are_refused() {
        let host = FakeHost::default();
        let root = GradingUser {
            name: "root".into(),
            uid: 0,
            gid: 0,
        };
        let me = GradingUser {
            name: "me".into(),
            uid: rustix::process::getuid().as_raw(),
            gid: 0,
        };
        let other = GradingUser {
            name: "other".into(),
            uid: 4242,
            gid: 4242,
        };

        let error = check_user(&root, &host).unwrap_err().to_string();
        assert_eq!(error, "the grading user must not be root: 'root' has uid 0");

        if me.uid != 0 {
            let error = check_user(&me, &host).unwrap_err().to_string();
            assert_eq!(
                error,
                "the grading user must not be the user running the grader: 'me'"
            );
        }

        assert!(check_user(&other, &host).is_ok());
    }

    #[test]
    fn kill_user_repeats_until_no_process_is_left() {
        let mut calls = Vec::new();
        let mut left = [0, 0, 1].into_iter();

        kill_user_with("grader", |argv| {
            calls.push(argv.join(" "));
            Ok(Some(if argv[0] == "pgrep" {
                left.next().unwrap()
            } else {
                0
            }))
        })
        .unwrap();

        assert_eq!(
            calls,
            ["pkill -KILL -u grader", "pgrep -u grader"].repeat(3)
        );
    }

    #[test]
    fn kill_user_gives_up_after_a_few_passes() {
        let mut calls = 0;

        kill_user_with("grader", |_| {
            calls += 1;
            Ok(Some(0))
        })
        .unwrap();

        assert_eq!(calls, 2 * KILL_PASSES);
    }

    fn refuse(_: Pid) -> Result<(), Errno> {
        Err(Errno::PERM)
    }

    #[test]
    fn kill_group_ignores_eperm_once_the_child_has_ended() {
        let mut process = Command::new("true").process_group(0).spawn().unwrap();
        let pid = Pid::from_child(&process);
        let options = WaitIdOptions::EXITED | WaitIdOptions::NOWAIT;
        waitid(WaitId::Pid(pid), options).unwrap();

        let killed = kill_group_with(pid, refuse);

        process.wait().unwrap();
        assert!(killed.is_ok());
    }

    #[test]
    fn kill_group_raises_eperm_while_the_child_runs() {
        let mut process = Command::new("sleep")
            .arg("30")
            .process_group(0)
            .spawn()
            .unwrap();

        let killed = kill_group_with(Pid::from_child(&process), refuse);

        process.kill().unwrap();
        process.wait().unwrap();
        let error = killed.unwrap_err();
        assert!(
            matches!(&error, GradeError::Os { error, .. } if error.raw_os_error() == Some(1)),
            "{error}"
        );
    }

    #[test]
    fn decode_rejects_lines_that_are_not_events() {
        let host = FakeHost::default();
        let start = r#"{"schema": 1, "event": "start", "test": "t"}"#;

        assert!(decode(start, &host).is_ok());
        assert_eq!(
            decode("not json", &host).unwrap_err(),
            "not JSON: 'not json'"
        );
        assert_eq!(
            decode(r#"{"schema": 2, "event": "start", "test": "t"}"#, &host).unwrap_err(),
            "schema version 2, expected 1"
        );
        assert!(decode(&"[".repeat(200_000), &host).is_err());
    }
}
