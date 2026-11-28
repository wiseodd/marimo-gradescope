//! Grade a submission in child processes that the parent watches and restarts.
//!
//! Each phase runs in its own child, as an unprivileged user when one is given. The parent
//! enforces every deadline itself, since student code can defeat the child's own timer and C
//! code ignores it. When a child dies or misses a deadline, the parent kills it and everything
//! it started, fails the tests it was working on, and starts a new child for the rest, at most
//! `MAX_RESPAWNS` times. Every test gets exactly one outcome. Linux and macOS; a grading user
//! and its process cap are for Gradescope (Linux).
//!
//! A child that dies or hangs while it loads the notebook may be stuck in the whole-notebook
//! run (`loader.bind`). The parent then starts one more child that skips that run, so names
//! that need no run still get graded. Later children and the hidden phase skip it too. If that
//! child also fails to load, every test of the phase fails.
//!
//! The parent keeps the first result per test and ignores results for tests the child has not
//! announced. Code in the child can still write forged events to the channel; the threat model
//! accepts that.

use std::ffi::OsStr;
use std::fs::{self, File};
use std::io;
use std::os::unix::fs::{DirBuilderExt, chown};
use std::path::{Path, PathBuf};
use std::time::Instant;

use indexmap::IndexMap;
use mogs_schema::{Bundle, FailureReason, Job, Outcome, SCHEMA};

use crate::files::{SUBMISSION, data_files};
use crate::process::{Child, check_user, kill_user, launch_options};
use crate::watch::{Ending, Texts, Watch, fail, later};
use crate::{GradeError, GradingUser, Host, Level};

/// Most children started again after one crashed or hung, per phase.
pub const MAX_RESPAWNS: u32 = 3;

/// A grading pass. Each runs in its own child process.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    /// The public test cells.
    Public,
    /// The hidden test cells and the hidden helpers.
    Hidden,
}

impl Phase {
    /// The name of the phase and of its folder.
    pub fn name(self) -> &'static str {
        match self {
            Phase::Public => "public",
            Phase::Hidden => "hidden",
        }
    }
}

/// What `grade` found.
#[derive(Clone, Debug, PartialEq)]
pub struct Graded {
    /// One outcome per test of the bundle: public tests first, then hidden ones.
    pub outcomes: IndexMap<String, Outcome>,
    /// Why the notebook, or the grader's setup code, did not load, if it did not in some
    /// phase. A hidden helper or test cell that fails does not count.
    pub load_error: Option<String>,
}

/// Where the data files come from and how the children run.
pub struct GradeOptions<'a> {
    /// Dedicated grading user to run student code as, or `None` to keep the current one.
    pub user: Option<&'a str>,
    /// Folder the data files are relative to. Required when `files` is given.
    pub files_root: Option<&'a Path>,
    /// Data files or folders for the notebook, as relative paths.
    pub files: &'a [String],
    /// Interpreter for the children. It must have mogs and the notebook's dependencies.
    pub python: &'a OsStr,
}

/// Run the public phase, then the hidden phase, within one time budget.
///
/// Each phase gets a fresh `root_workdir/<phase>/` with a copy of the submission (as
/// `submission.py`) and of each data file `files_root/<rel>` at `<rel>`. With a user, the
/// folder belongs to that user, and every process of the user is killed after each phase. The
/// caller must make sure that the user can enter `root_workdir` but not write to it.
///
/// # Errors
///
/// [`GradeError::Value`] if the user is root or the user running the grader, `submission` is
/// not a regular file, or a data path is absolute, leaves its root or is `submission.py`;
/// [`GradeError::Os`] if a folder or file cannot be made; [`GradeError::Host`] if the host
/// stops the run or has no such user.
pub fn grade(
    bundle: &Bundle,
    submission: &Path,
    root_workdir: &Path,
    options: &GradeOptions,
    host: &dyn Host,
) -> Result<Graded, GradeError> {
    let user = match options.user {
        Some(name) => {
            let user = host.user(name)?;
            check_user(&user, host)?;
            Some(user)
        }
        None => None,
    };

    let meta = fs::symlink_metadata(submission).map_err(|e| GradeError::os(e, submission))?;

    if !meta.file_type().is_file() {
        return Err(GradeError::Value(format!(
            "the submission must be a regular file: {}",
            submission.display()
        )));
    }

    let data = match (options.files, options.files_root) {
        ([], _) => IndexMap::new(),
        (_, None) => return Err(GradeError::Value("data files need a files_root".into())),
        (files, Some(root)) => data_files(root, files, host)?,
    };
    let texts = Texts {
        timeout: host.g(bundle.config.timeout),
        budget: host.g(bundle.config.budget),
    };
    let deadline = later(Instant::now(), bundle.config.budget);
    let mut outcomes = IndexMap::new();
    let mut run_failed = None;
    let mut load_error = None;

    for phase in [Phase::Public, Phase::Hidden] {
        let workdir = prepare(
            &root_workdir.join(phase.name()),
            submission,
            &data,
            user.as_ref(),
        )?;
        let run = PhaseRun {
            bundle: &for_phase(bundle, phase),
            phase,
            submission: &workdir.join(SUBMISSION),
            workdir: &workdir,
            user: user.as_ref(),
            python: options.python,
            deadline,
            texts: &texts,
        };
        let ran = run.run(run_failed, host)?;
        outcomes.extend(ran.outcomes);
        run_failed = ran.run_failed;
        load_error = load_error.or(ran.load_error);

        if let Some(user) = &user {
            kill_user(&user.name)?;
        }
    }

    Ok(Graded {
        outcomes,
        load_error,
    })
}

/// The bundle with only what `phase` runs: the public test cells, or the hidden test cells
/// and the hidden helpers.
fn for_phase(bundle: &Bundle, phase: Phase) -> Bundle {
    let hidden = phase == Phase::Hidden;
    let cells = bundle
        .cells
        .iter()
        .filter(|cell| cell.hidden == hidden)
        .cloned()
        .collect();
    let helpers = if hidden {
        bundle.helpers.clone()
    } else {
        Vec::new()
    };

    Bundle {
        cells,
        helpers,
        ..bundle.clone()
    }
}

/// One phase, run in children started in `workdir`.
struct PhaseRun<'a> {
    bundle: &'a Bundle,
    phase: Phase,
    submission: &'a Path,
    workdir: &'a Path,
    user: Option<&'a GradingUser>,
    python: &'a OsStr,
    deadline: Instant,
    texts: &'a Texts,
}

/// One phase's outcomes, why the whole-notebook run failed, and why loading failed.
struct Ran {
    outcomes: IndexMap<String, Outcome>,
    run_failed: Option<String>,
    load_error: Option<String>,
}

impl PhaseRun<'_> {
    /// Run the phase's tests, starting from why the whole-notebook run failed, if it did.
    fn run(&self, mut run_failed: Option<String>, host: &dyn Host) -> Result<Ran, GradeError> {
        let tests: Vec<String> = self.bundle.tests().cloned().collect();
        let mut done = IndexMap::new();
        let mut respawns = 0;
        let mut load_error = None;

        loop {
            let pending: Vec<String> = tests
                .iter()
                .filter(|name| !done.contains_key(*name))
                .cloned()
                .collect();

            if pending.is_empty() {
                break;
            }

            if respawns > MAX_RESPAWNS {
                let message =
                    format!("not run: the grader restarted {MAX_RESPAWNS} times after crashes");
                fail(
                    &mut done,
                    &pending,
                    &message,
                    FailureReason::NotRun,
                    0.0,
                    host,
                );
                break;
            }

            if Instant::now() >= self.deadline {
                let message = self.texts.budget_message();
                fail(
                    &mut done,
                    &pending,
                    &message,
                    FailureReason::Budget,
                    0.0,
                    host,
                );
                break;
            }

            let job = Job {
                schema: SCHEMA,
                bundle: self.bundle.clone(),
                submission: self.submission.to_path_buf(),
                skip: done.keys().cloned().collect(),
                run_failed: run_failed.clone(),
                alarm: true,
            };
            // JSON strings are Unicode, so a path that is not UTF-8 cannot reach the child.
            // The job's only path is `workdir/submission.py`, so the folder is at fault.
            let job = mogs_schema::encode(&job).map_err(|_| {
                GradeError::Value(format!(
                    "the grading folder must be a UTF-8 path: {}",
                    self.workdir.display()
                ))
            })?;
            let options = launch_options(
                self.workdir,
                self.user,
                self.bundle.config.memory_mb,
                self.python,
            );
            let mut child = Child::spawn(&job, &options)?;
            let timeout = self.bundle.config.timeout;
            let retry_load = run_failed.is_none();
            let mut watch = Watch::new(
                timeout,
                self.texts,
                &pending,
                &mut done,
                self.deadline,
                retry_load,
            );
            let ending = watch.run(&mut child, host);
            let (load_failure, watched_error) = (watch.load_failure, watch.load_error);
            let output = child.close()?;

            if !output.is_empty() {
                let format = "grading child output (%s phase, end):\n%s";
                host.log(Level::Info, format, &[self.phase.name(), &output]);
            }

            load_error = load_error.or(watched_error);

            match ending? {
                Ending::Stop => break,
                Ending::RetryWithoutRun => run_failed = load_failure,
                Ending::Respawn => respawns += 1,
            }
        }

        // Every way out of the loop gives each test an outcome; v0.7 raised `KeyError` otherwise.
        let outcomes = tests
            .iter()
            .map(|name| match done.get(name) {
                Some(outcome) => Ok((name.clone(), outcome.clone())),
                None => Err(GradeError::Key(name.clone())),
            })
            .collect::<Result<_, _>>()?;

        Ok(Ran {
            outcomes,
            run_failed,
            load_error,
        })
    }
}

/// Create `workdir` with copies of the submission and the data files.
///
/// # Errors
///
/// [`GradeError::Os`] if `workdir` exists (each phase needs a fresh one), or a copy fails.
fn prepare(
    workdir: &Path,
    submission: &Path,
    files: &IndexMap<String, PathBuf>,
    user: Option<&GradingUser>,
) -> Result<PathBuf, GradeError> {
    fs::DirBuilder::new()
        .mode(0o700)
        .create(workdir)
        .map_err(|error| GradeError::os(error, workdir))?;
    copy(submission, &workdir.join(SUBMISSION))?;

    for (rel, source) in files {
        let target = workdir.join(rel);
        let parent = target.parent().unwrap_or(workdir);
        fs::create_dir_all(parent).map_err(|error| GradeError::os(error, parent))?;
        copy(source, &target)?;
    }

    if let Some(user) = user {
        hand_over(workdir, user)?;
    }

    Ok(workdir.to_path_buf())
}

/// Copy the content of `source` to a new or emptied `target`, as `shutil.copyfile` does.
fn copy(source: &Path, target: &Path) -> Result<(), GradeError> {
    let mut from = File::open(source).map_err(|error| GradeError::os(error, source))?;
    let mut to = File::create(target).map_err(|error| GradeError::os(error, target))?;
    io::copy(&mut from, &mut to).map_err(|error| GradeError::os(error, target))?;

    Ok(())
}

/// Make `user` the owner of `workdir` and everything in it.
fn hand_over(workdir: &Path, user: &GradingUser) -> Result<(), GradeError> {
    chown(workdir, Some(user.uid), Some(user.gid)).map_err(|e| GradeError::os(e, workdir))?;

    let listing = fs::read_dir(workdir).map_err(|error| GradeError::os(error, workdir))?;

    for entry in listing {
        let path = entry?.path();

        if path.is_dir() && !path.is_symlink() {
            hand_over(&path, user)?;
        } else {
            chown(&path, Some(user.uid), Some(user.gid)).map_err(|e| GradeError::os(e, &path))?;
        }
    }

    Ok(())
}
