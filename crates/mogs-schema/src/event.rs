//! The lines a grading child writes, as `mogs.grader.protocol.encode` writes them.

use std::collections::BTreeMap;

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use crate::Outcome;

/// One event line: the format version and the event.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct EventLine {
    /// Format version, `SCHEMA`.
    #[schemars(extend("const" = crate::SCHEMA))]
    pub schema: u32,
    /// The event.
    #[serde(flatten)]
    pub event: Event,
}

/// What a grading child reports, tagged by `"event"`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum Event {
    /// Once, after setup and binding: the child is ready, or failed to set up.
    Loaded {
        /// Why setup failed, `None` when it worked.
        error: Option<String>,
        /// Each name the tests use that the submission could not provide, with why.
        unavailable: BTreeMap<String, String>,
    },
    /// Before loading a test cell and the hidden helpers it needs.
    Loading {
        /// The cell's tests that will run.
        tests: Vec<String>,
    },
    /// Before each test.
    Start {
        /// The test.
        test: String,
    },
    /// After each test, with the outcome's fields next to `event`.
    Result(Outcome),
}
