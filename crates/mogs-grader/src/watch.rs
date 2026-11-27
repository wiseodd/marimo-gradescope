//! Follow one child's events, enforce its deadlines and fail the tests it is blamed for.

use std::collections::HashSet;
use std::time::{Duration, Instant};

use indexmap::IndexMap;
use mogs_schema::{Event, EventLine, FailureReason, Outcome};

use crate::process::{Child, describe_exit};
use crate::{GradeError, Host, Level, PyText};

/// Seconds a test may run past its timeout before the parent kills the child.
pub const GRACE: f64 = 5.0;
/// Seconds a child that has finished every test may take to exit.
pub const EXIT_GRACE: f64 = 2.0;

/// What `run_phase` does after a child ends.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Ending {
    Stop,
    Respawn,
    RetryWithoutRun,
}

/// What the child is doing, as far as its events tell.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Stage {
    Setup,
    Loading,
    Test,
    Idle,
    Finishing,
}

/// The failure messages that quote the config, formatted once.
pub struct Texts {
    /// `timeout` with `:g`.
    pub timeout: String,
    /// `budget` with `:g`.
    pub budget: String,
}

impl Texts {
    /// The message of the tests that the budget left without a run.
    pub fn budget_message(&self) -> String {
        format!(
            "not run: grading took longer than its {} s budget",
            self.budget
        )
    }
}

/// Follows one child.
///
/// Every test it fails or finishes goes into `done`, which the caller shares across children.
/// With `retry_load`, a child that dies or hangs before it has loaded the notebook fails no
/// test: the watch keeps why in `load_failure` and asks for a retry without the notebook run.
/// Why the notebook failed to load for good, as the child reports it or as the watch sees it,
/// goes into `load_error`.
pub struct Watch<'a> {
    timeout: f64,
    texts: &'a Texts,
    pending: &'a [String],
    done: &'a mut IndexMap<String, Outcome>,
    deadline: Instant,
    started: HashSet<String>,
    stage: Stage,
    unit: Vec<String>,
    since: Instant,
    due: Instant,
    retry_load: bool,
    pub load_failure: Option<String>,
    pub load_error: Option<String>,
}

impl<'a> Watch<'a> {
    pub fn new(
        timeout: f64,
        texts: &'a Texts,
        pending: &'a [String],
        done: &'a mut IndexMap<String, Outcome>,
        deadline: Instant,
        retry_load: bool,
    ) -> Self {
        let since = Instant::now();

        Watch {
            timeout,
            texts,
            pending,
            done,
            deadline,
            started: HashSet::new(),
            stage: Stage::Setup,
            unit: Vec::new(),
            since,
            due: later(since, timeout + GRACE),
            retry_load,
            load_failure: None,
            load_error: None,
        }
    }

    /// Watch `child` until it exits or misses a deadline.
    ///
    /// # Errors
    ///
    /// [`GradeError::Host`] when the host asks to stop, or the error of a failed wait.
    pub fn run(&mut self, child: &mut Child, host: &dyn Host) -> Result<Ending, GradeError> {
        loop {
            let until = self.due.min(self.deadline);

            for event in child.wait(until, host)? {
                self.handle(event);
            }

            if host.interrupted() {
                return Err(GradeError::Host);
            }

            if let Some(error) = &child.error {
                host.log(
                    Level::Warning,
                    "grading child broke the protocol: %s",
                    &[error],
                );
                return Ok(self.crashed("the grader received a malformed event", host));
            }

            if let Some(exit) = child.exit {
                if self.stage == Stage::Finishing {
                    return Ok(Ending::Stop);
                }

                return Ok(self.crashed(&describe_exit(exit, host), host));
            }

            let now = Instant::now();

            if now >= self.deadline {
                if self.stage == Stage::Finishing {
                    return Ok(Ending::Stop);
                }

                let stopped = format!(
                    "stopped: grading ran past its {} s budget",
                    self.texts.budget
                );
                self.fail_unit(&stopped, FailureReason::Budget, 0.0, host);
                self.fail_rest(&self.texts.budget_message(), FailureReason::Budget, host);
                return Ok(Ending::Stop);
            }

            if now >= self.due {
                return Ok(self.overdue(now, host));
            }
        }
    }

    fn handle(&mut self, line: EventLine) {
        match line.event {
            Event::Loaded { error, .. } => {
                if self.stage == Stage::Setup {
                    self.load_error = error;
                    self.enter(Stage::Idle, Vec::new());
                }
            }
            Event::Loading { tests } => {
                let unit = tests.into_iter().filter(|test| self.open(test)).collect();
                self.enter(Stage::Loading, unit);
            }
            Event::Start { test } if self.open(&test) => {
                self.started.insert(test.clone());
                self.enter(Stage::Test, vec![test]);
            }
            Event::Result(outcome)
                if self.started.contains(&outcome.name) && self.open(&outcome.name) =>
            {
                self.done.insert(outcome.name.clone(), outcome);
                self.enter(Stage::Idle, Vec::new());
            }
            Event::Start { .. } | Event::Result(_) => {}
        }

        if self.pending.iter().all(|name| self.done.contains_key(name)) {
            self.enter(Stage::Finishing, Vec::new());
        }
    }

    /// Whether `test` belongs to this phase and has no outcome yet.
    fn open(&self, test: &str) -> bool {
        self.pending.iter().any(|name| name == test) && !self.done.contains_key(test)
    }

    fn enter(&mut self, stage: Stage, unit: Vec<String>) {
        self.stage = stage;
        self.unit = unit;
        self.since = Instant::now();
        let grace = match stage {
            Stage::Finishing => EXIT_GRACE,
            _ => self.timeout + GRACE,
        };
        self.due = later(self.since, grace);
    }

    /// Fail what the child was doing when it died; respawn, or retry setup without the run.
    fn crashed(&mut self, status: &str, text: &dyn PyText) -> Ending {
        match self.stage {
            Stage::Setup if self.retry_load => {
                self.load_failure = Some(status.into());
                return Ending::RetryWithoutRun;
            }
            Stage::Setup => {
                let message = format!("the grader stopped while loading your notebook: {status}");
                self.fail_rest(&message, FailureReason::LoadFailed, text);
                self.load_error = Some(message);
                return Ending::Stop;
            }
            Stage::Loading => {
                let message = format!("the grader stopped while loading the test cell: {status}");
                self.fail_unit(&message, FailureReason::Killed, 0.0, text);
            }
            Stage::Test => {
                let message = format!("the grader stopped during this test: {status}");
                self.fail_unit(&message, FailureReason::Killed, 0.0, text);
            }
            Stage::Idle | Stage::Finishing => {}
        }

        Ending::Respawn
    }

    /// Fail what the child is stuck on; respawn, or retry setup without the run.
    fn overdue(&mut self, now: Instant, text: &dyn PyText) -> Ending {
        let took = format!("it took longer than {} s", self.texts.timeout);

        match self.stage {
            Stage::Setup if self.retry_load => {
                self.load_failure = Some(took);
                return Ending::RetryWithoutRun;
            }
            Stage::Setup => {
                let message = format!("the grader stopped while loading your notebook: {took}");
                self.fail_rest(&message, FailureReason::Timeout, text);
                self.load_error = Some(message);
                return Ending::Stop;
            }
            Stage::Loading => {
                let message = format!("the grader stopped while loading the test cell: {took}");
                self.fail_unit(&message, FailureReason::Timeout, 0.0, text);
            }
            Stage::Test => {
                let message = format!("the test took longer than {} s", self.texts.timeout);
                let seconds = (now - self.since).as_secs_f64();
                self.fail_unit(&message, FailureReason::Timeout, seconds, text);
            }
            Stage::Finishing => return Ending::Stop,
            Stage::Idle => {}
        }

        Ending::Respawn
    }

    fn fail_unit(&mut self, message: &str, reason: FailureReason, seconds: f64, text: &dyn PyText) {
        fail(self.done, &self.unit, message, reason, seconds, text);
    }

    fn fail_rest(&mut self, message: &str, reason: FailureReason, text: &dyn PyText) {
        fail(self.done, self.pending, message, reason, 0.0, text);
    }
}

/// Give each of `names` without an outcome a failed one.
pub fn fail(
    done: &mut IndexMap<String, Outcome>,
    names: &[String],
    message: &str,
    reason: FailureReason,
    seconds: f64,
    text: &dyn PyText,
) {
    for name in names {
        if !done.contains_key(name) {
            let outcome = Outcome {
                name: name.clone(),
                passed: false,
                message: text.cap(message),
                reason: Some(reason),
                seconds,
            };
            done.insert(name.clone(), outcome);
        }
    }
}

/// `seconds` after `at`, at most a century, so an infinite budget works. The config allows
/// only seconds > 0, and JSON has no NaN.
pub fn later(at: Instant, seconds: f64) -> Instant {
    let century = 100.0 * 365.0 * 24.0 * 3600.0;

    at + Duration::from_secs_f64(seconds.clamp(0.0, century))
}
