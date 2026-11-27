//! Gradescope's `results.json`, as `mogs.grader.results` writes it. It has no `schema` key.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

/// The content of `results.json`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Results {
    /// One row per entry Gradescope shows.
    pub tests: Vec<Row>,
    /// Who sees the grader's stdout.
    pub stdout_visibility: String,
    /// The total, set only when `tests` is empty.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub score: Option<f64>,
    /// The score summary for students.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub output: Option<String>,
    /// Format of `output`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub output_format: Option<String>,
}

/// One entry of `tests` in `results.json`, with its keys in the order `results.row` adds them.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Row {
    /// Row title.
    pub name: String,
    /// Points earned.
    pub score: f64,
    /// Points possible.
    pub max_score: f64,
    /// Text under the row.
    pub output: String,
    /// Format of `output`.
    pub output_format: String,
    /// Who sees the row.
    pub visibility: String,
    /// Question id shown next to the title.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub number: Option<String>,
    /// `failed` marks the row as failed.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
}
