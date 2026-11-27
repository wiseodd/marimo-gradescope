//! What the notebook functions need to know about a saved notebook, read without running it.
//!
//! Each field names the Python call site it serves.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use crate::Bundle;

/// A saved notebook, source or student copy, as the Python notebook functions see it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Inspect {
    /// Format version, `SCHEMA`.
    #[serde(deserialize_with = "crate::version")]
    #[schemars(extend("const" = crate::SCHEMA))]
    pub schema: u32,
    /// Why the notebook does not parse (`SourceError`). `assignment.assignment` lists it as
    /// the problem; `export.package` warns that no answer could be read; `selfcheck.main`
    /// exits with it.
    #[serde(deserialize_with = "Option::deserialize")]
    pub source_error: Option<String>,
    /// Why the config calls are invalid (`ConfigError`). `assignment.assignment` lists it as
    /// the problem; `check._written_in_source` raises it.
    #[serde(deserialize_with = "Option::deserialize")]
    pub config_error: Option<String>,
    /// The config, `None` when `config_error` is set. `assignment.summary` titles the panel
    /// with it.
    #[serde(deserialize_with = "Option::deserialize")]
    pub config: Option<InspectConfig>,
    /// What the build would refuse: `source.validate` and the data-file check
    /// (`assignment._data_problems`). `assignment.assignment` lists them.
    pub problems: Vec<String>,
    /// The config's questions in notebook order, empty without a config. `assignment.summary`
    /// has a row per question; `check._written_in_source` reads `manual` by id.
    pub questions: Vec<InspectQuestion>,
    /// Every cell whose name starts with `answer_`, in file order, whatever the grade calls
    /// and the config say. `export.unanswered_lines` and `export.answers_markdown` pick each
    /// manual question's cells from them by name, as `Notebook.answer_cells` does.
    pub answers: Vec<Answer>,
    /// The public test files that grade calls name (`selfcheck.test_files`).
    /// `check.file_outcomes` and `selfcheck.public_results` run them; `selfcheck` fails the
    /// unreadable ones.
    pub test_files: Vec<TestFile>,
    /// What `selfcheck.public_results` runs: the public phase of the notebook's bundle, then a
    /// public cell for each readable test file, labelled with its path. `None` when
    /// `source_error` is set.
    #[serde(deserialize_with = "Option::deserialize")]
    pub public_job: Option<Bundle>,
}

/// The config fields the notebook functions use.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct InspectConfig {
    /// Assignment name. `assignment.summary` and `assignment._problems` title the panel.
    pub name: String,
}

/// One question of the config, with its tests and prompts.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
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

/// One answer cell: a cell whose name starts with `answer_`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Answer {
    /// Cell name, e.g. `answer_q3` or `answer_q3_2`. `export` picks the cells of question
    /// `<qid>` as `Notebook.answer_cells` does, by the name `answer_<qid>` or a name starting
    /// with `answer_<qid>_`, and looks each cell's placeholder up by its name. The name alone
    /// does not tell the question, since ids may hold `_`.
    pub cell: String,
    /// The Markdown of a cell that is just `mo.md("...")`, `None` for other code
    /// (`source.answer_text`). `export` checks it against the placeholder and writes it.
    #[serde(deserialize_with = "Option::deserialize")]
    pub text: Option<String>,
    /// The cell's lines as written (`source.cell_source`). `export.answers_markdown` shows a
    /// code answer, one whose `text` is `None`, as this source.
    pub source: String,
}

/// A file of public tests that a grade call names as `test_file`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
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
    #[serde(deserialize_with = "Option::deserialize")]
    pub error: Option<TestFileError>,
}

/// Why a test file cannot be read or parsed, tagged by `"kind"`. It carries data, not text:
/// each call site words it itself.
///
/// `selfcheck.public_results` fails the file with `cannot read <path> (<summary>)`, where the
/// summary is `runner.exception_summary` of the exception Python would have raised, rebuilt
/// from the variant as each variant says. `<file>` below is the notebook's folder joined with
/// `path`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum TestFileError {
    /// The file does not exist. `check.file_outcomes`: `Cannot find <path> next to
    /// <notebook>. Keep the `tests` folder of the assignment next to the notebook.`
    /// `selfcheck`: the exception is `FileNotFoundError(ENOENT, os.strerror(ENOENT), <file>)`.
    Missing {},
    /// The file exists but cannot be read. `check.file_outcomes`: `Cannot read <path>:
    /// <exception>`. Both call sites rebuild the exception as
    /// `OSError(errno, os.strerror(errno), <file>)`, which picks the subclass, such as
    /// `PermissionError`.
    Unreadable {
        /// The C `errno`, as Python's `OSError.errno` gives it.
        errno: i32,
    },
    /// The file is not UTF-8. `check.file_outcomes`: `Cannot read <path>: <exception>`. Both
    /// call sites rebuild the exception as
    /// `UnicodeDecodeError("utf-8", bytes(start) + bytes, start, start + len(bytes), <reason>)`:
    /// the zero padding puts `bytes` at `start`, so `str()` reads them where Python's decoder
    /// found them.
    NotUtf8 {
        /// Byte offset of the first byte that does not decode, `UnicodeDecodeError.start`.
        start: u64,
        /// The bytes that do not decode, `object[start:end]` of the `UnicodeDecodeError`.
        bytes: Vec<u8>,
        /// Why they do not decode.
        reason: Utf8Reason,
    },
    /// The file is not valid Python. `check.file_outcomes`: `<path> is not valid Python (line
    /// <line>).` `selfcheck`: the exception is `<class>(message, ("<unknown>", line, None,
    /// None))`, as `ast.parse` raises it.
    Syntax {
        /// Which `SyntaxError` class `ast.parse` raised.
        class: SyntaxClass,
        /// Line of the error, from 1, `SyntaxError.lineno`.
        line: u32,
        /// Python's message for it, `SyntaxError.msg`, such as `invalid syntax`.
        message: String,
    },
}

/// The class of a `SyntaxError` that `ast.parse` raises.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum SyntaxClass {
    /// `SyntaxError`.
    SyntaxError,
    /// `IndentationError`.
    IndentationError,
    /// `TabError`.
    TabError,
}

/// Why bytes are not UTF-8: the `reason` of Python's `UnicodeDecodeError`, with spaces as
/// underscores.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum Utf8Reason {
    /// `invalid start byte`.
    InvalidStartByte,
    /// `invalid continuation byte`.
    InvalidContinuationByte,
    /// `unexpected end of data`.
    UnexpectedEndOfData,
}
