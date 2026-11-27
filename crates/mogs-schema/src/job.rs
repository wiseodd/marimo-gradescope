//! What one grading child runs, as `mogs.grader.protocol.Job` writes it.

use std::path::PathBuf;

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use crate::Bundle;

/// What one grading child runs. The parent writes it as JSON to the child's stdin.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Job {
    /// Format version, `SCHEMA`.
    #[schemars(extend("const" = crate::SCHEMA))]
    pub schema: u32,
    /// The bundle of one phase.
    pub bundle: Bundle,
    /// The student's notebook.
    pub submission: PathBuf,
    /// Tests already done, e.g. by an earlier child that died.
    pub skip: Vec<String>,
    /// Why an earlier child died while running the whole notebook. When set, the child does
    /// not run it again.
    pub run_failed: Option<String>,
    /// Whether the child times each test itself with an alarm.
    pub alarm: bool,
}
