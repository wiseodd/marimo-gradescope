//! What the notebook functions need to know about a saved notebook, read without running it.
//!
//! Each field names the Python call site it serves.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use crate::Bundle;

/// A saved notebook, source or student copy, as the Python notebook functions see it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Inspect {
    /// Format version, `SCHEMA`.
    #[schemars(extend("const" = crate::SCHEMA))]
    pub schema: u32,
    /// Why the notebook does not parse (`SourceError`). `assignment.assignment` lists it as
    /// the problem; `export.package` warns that no answer could be read; `selfcheck.main`
    /// exits with it.
    pub source_error: Option<String>,
    /// Why the config calls are invalid (`ConfigError`). `assignment.assignment` lists it as
    /// the problem; `check._written_in_source` raises it.
    pub config_error: Option<String>,
    /// The config, `None` when `config_error` is set. `assignment.summary` titles the panel
    /// with it.
    pub config: Option<InspectConfig>,
    /// What the build would refuse: `source.validate` and the data-file check
    /// (`assignment._data_problems`). `assignment.assignment` lists them.
    pub problems: Vec<String>,
    /// The config's questions in notebook order, empty without a config. `assignment.summary`
    /// has a row per question; `check._written_in_source` reads `manual` by id.
    pub questions: Vec<InspectQuestion>,
    /// The answer cells of each question a grade call names, in file order
    /// (`Notebook.answer_cells`). `export.unanswered_lines` and `export.answers_markdown`
    /// read them.
    pub answers: Vec<Answer>,
    /// The public test files that grade calls name (`selfcheck.test_files`).
    /// `check.file_outcomes` and `selfcheck.public_results` run them; `selfcheck` fails the
    /// unreadable ones.
    pub test_files: Vec<TestFile>,
    /// What `selfcheck.public_results` runs: the public phase of the notebook's bundle, then a
    /// public cell for each readable test file, in `test_files` order. `None` when
    /// `source_error` is set.
    pub public_job: Option<Bundle>,
}

/// The config fields the notebook functions use.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct InspectConfig {
    /// Assignment name. `assignment.summary` and `assignment._problems` title the panel.
    pub name: String,
}

/// One question of the config, with its tests and prompts.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct InspectQuestion {
    /// Question id, e.g. `q1`.
    pub id: String,
    /// Question title, shown by `assignment.summary`.
    pub title: String,
    /// Points, shown and added up by `assignment.summary`.
    pub points: f64,
    /// Whether it is graded by hand. `assignment.summary` shows it as written;
    /// `check._written_in_source` reads it.
    pub manual: bool,
    /// Public test names in file order (`Notebook.tests_by_question`), counted by
    /// `assignment.summary`.
    pub public: Vec<String>,
    /// Hidden test names in file order, counted by `assignment.summary`.
    pub hidden: Vec<String>,
    /// The first line of each prompt cell of a written question (`source.prompt_previews`),
    /// listed by `assignment.summary`.
    pub prompts: Vec<String>,
}

/// One answer cell of a written question.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Answer {
    /// Question id.
    pub qid: String,
    /// Cell name, e.g. `answer_q3` or `answer_q3_2`, which `export` looks its placeholder up
    /// by.
    pub cell: String,
    /// The Markdown of a cell that is just `mo.md("...")`, `None` for other code
    /// (`source.answer_text`). `export` checks it against the placeholder and writes it.
    pub text: Option<String>,
    /// The cell's lines as written (`source.cell_source`). `export.answers_markdown` shows a
    /// code answer, one whose `text` is `None`, as this source.
    pub source: String,
}

/// A file of public tests that a grade call names as `test_file`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TestFile {
    /// Question id of the grade call.
    pub qid: String,
    /// The path as the grade call gives it, relative to the notebook's folder.
    pub path: String,
    /// Test function names in file order (`source.module_tests`), which `check.file_outcomes`
    /// runs.
    pub tests: Vec<String>,
    /// Why the file cannot be read or parsed, `None` when it can. `check.file_outcomes` shows
    /// it; `selfcheck.public_results` fails the file with it.
    pub error: Option<String>,
}
