//! The result of one test, as `mogs.runner.Outcome` writes it.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

/// Why a test did not pass. The values are `mogs.runner.FailureReason`'s.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum FailureReason {
    /// An `assert` failed.
    Assertion,
    /// The test raised another exception.
    Error,
    /// The test ran past its time limit.
    Timeout,
    /// The process running the test died.
    Killed,
    /// The test ran out of memory.
    OutOfMemory,
    /// The grading run's time budget ran out.
    Budget,
    /// The test's cell, or what it needs, failed to load.
    LoadFailed,
    /// The test never ran.
    NotRun,
}

/// The result of one test.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Outcome {
    /// Test name.
    pub name: String,
    /// Whether the test passed.
    pub passed: bool,
    /// Failure message, empty when passed.
    pub message: String,
    /// Why the test failed, `None` when passed.
    pub reason: Option<FailureReason>,
    /// Wall time spent in the test.
    pub seconds: f64,
}
