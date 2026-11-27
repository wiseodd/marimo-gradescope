//! The tests the grader runs, as `mogs.grader.bundle.GradingBundle` writes them.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

/// Everything a grading child needs besides the submission.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Bundle {
    /// Format version, `SCHEMA`.
    #[schemars(extend("const" = crate::SCHEMA))]
    pub schema: u32,
    /// The assignment config the grader needs.
    pub config: BundleConfig,
    /// Body of the notebook's setup cell, run before the test cells.
    pub setup_code: String,
    /// Test cells in file order.
    pub cells: Vec<TestCell>,
    /// Hidden cells without tests, in file order.
    pub helpers: Vec<HiddenHelper>,
}

/// The assignment config, without the host path of `requirements`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct BundleConfig {
    /// Assignment name.
    pub name: String,
    /// File name of the source notebook.
    pub notebook: String,
    /// Questions in notebook order.
    pub questions: Vec<BundleQuestion>,
    /// Data files and folders shipped with the notebook, relative to it.
    pub files: Vec<String>,
    /// Seconds per test.
    pub timeout: f64,
    /// Seconds for the whole grading run.
    pub budget: f64,
    /// MB per grading process.
    pub memory_mb: u32,
    /// Python version on Gradescope.
    pub python: String,
}

/// One question of the config.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct BundleQuestion {
    /// Question id, e.g. `q1`.
    pub id: String,
    /// Question title.
    pub title: String,
    /// Points for the question.
    pub points: f64,
    /// Whether the question is graded by hand.
    pub manual: bool,
}

/// One test cell of the source notebook.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TestCell {
    /// Question id of the cell's tests.
    pub qid: String,
    /// The file name the runner compiles the cell under, which tracebacks and syntax errors
    /// show: `<qid>-tests` for a cell of the notebook, the path relative to the notebook for
    /// a public test file (as `selfcheck` runs it).
    pub label: String,
    /// Whether the cell is marked `# HIDDEN`.
    pub hidden: bool,
    /// Cell body, run as a module.
    pub source: String,
    /// Test function names, in file order.
    pub tests: Vec<String>,
    /// Student names the cell refers to, bound from the submission.
    pub uses: Vec<String>,
    /// Names the cell reads from hidden helpers. Empty for public cells.
    pub needs: Vec<String>,
}

/// A `# HIDDEN` cell without tests, such as data or helpers for hidden tests.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct HiddenHelper {
    /// Cell body, run as a module.
    pub source: String,
    /// Names the cell binds at top level.
    pub defines: Vec<String>,
    /// Student names the cell refers to, bound from the submission.
    pub uses: Vec<String>,
    /// Names the cell reads from other hidden helpers.
    pub needs: Vec<String>,
}
