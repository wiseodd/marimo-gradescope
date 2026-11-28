//! Turn test outcomes into Gradescope's `results.json`.
//!
//! Students see the score summary, the public test results and the notices. Each question's
//! row, which also lists its hidden tests, is hidden from them. Text a student controls
//! (failure messages, file names) is capped and quoted so it cannot change the layout around
//! it.

use indexmap::IndexMap;
use mogs_schema::{Bundle, Outcome, Results, Row};

use crate::{GradeError, PyText};

/// Title of the row with the notices about the upload.
pub const NOTICE_ROW: &str = "Submission";
/// Title of the row with the public tests.
pub const PUBLIC_ROW: &str = "Public tests";
/// The output of a row whose tests all passed.
pub const ALL_PASSED: &str = "All tests passed";

/// Gradescope's `results.json` content.
///
/// Each auto-graded question gets a row worth `points × passed / total`, hidden from students,
/// that lists all its tests with their failure messages. A visible 0-point "Public tests" row,
/// marked failed when one of them fails, lists the public tests. The top-level output gives
/// the total and each question's score. Notices go first, in a 0-point "Submission" row marked
/// failed, so Gradescope does not show it as passed. Manual questions get no row; with no rows
/// at all, the results carry a top-level score of 0.
///
/// # Errors
///
/// [`GradeError::Key`] if a test has no outcome, [`GradeError::Value`] if an auto-graded
/// question has no tests.
pub fn build_results(
    bundle: &Bundle,
    outcomes: &IndexMap<String, Outcome>,
    notices: &[String],
    text: &dyn PyText,
) -> Result<Results, GradeError> {
    let mut public_parts = Vec::new();
    let mut public_failed = false;
    let mut question_rows = Vec::new();

    for question in bundle.config.questions.iter().filter(|q| !q.manual) {
        let cells: Vec<_> = bundle
            .cells
            .iter()
            .filter(|cell| cell.qid == question.id)
            .collect();
        let pick = |hidden: bool| -> Result<Vec<&Outcome>, GradeError> {
            cells
                .iter()
                .filter(|cell| cell.hidden == hidden)
                .flat_map(|cell| &cell.tests)
                .map(|name| outcomes.get(name).ok_or(GradeError::Key(name.clone())))
                .collect()
        };
        let public = pick(false)?;
        let hidden = pick(true)?;
        let total = public.len() + hidden.len();
        let capped: IndexMap<&str, String> = public
            .iter()
            .chain(&hidden)
            .filter(|outcome| !outcome.passed && !outcome.message.is_empty())
            .map(|outcome| (outcome.name.as_str(), text.cap(&outcome.message)))
            .collect();

        if total == 0 {
            return Err(GradeError::Value(
                format!("question {} has no tests", question.id).into(),
            ));
        }

        if !public.is_empty() {
            let output = output(&public, &[], &capped);
            public_parts.push(format!("**{}**\n\n{output}", question.title));
            public_failed |= !public.iter().all(|outcome| outcome.passed);
        }

        let passed = public.iter().chain(&hidden).filter(|o| o.passed).count();
        let score = text.round(question.points * passed as f64 / total as f64, 4);
        let output = output(&public, &hidden, &capped);
        question_rows.push(Row {
            number: Some(question.id.clone()),
            visibility: "hidden".into(),
            ..row(&question.title, score, question.points, output)
        });
    }

    let mut rows = Vec::new();

    if !notices.is_empty() {
        rows.push(notice_row(notices, text));
    }

    if !public_parts.is_empty() {
        let row = row(PUBLIC_ROW, 0.0, 0.0, public_parts.join("\n\n"));
        rows.push(if public_failed { failed(row) } else { row });
    }

    if question_rows.is_empty() {
        return Ok(Results {
            tests: rows,
            stdout_visibility: "hidden".into(),
            score: Some(0.0),
            output: None,
            output_format: None,
        });
    }

    let summary = summary(&question_rows, text);
    rows.extend(question_rows);

    Ok(Results {
        tests: rows,
        stdout_visibility: "hidden".into(),
        score: None,
        output: Some(summary),
        output_format: Some("md".into()),
    })
}

/// Results for a grader crash: every question at 0, or one row without a bundle.
///
/// Every row is marked failed. Only the exception's class name `error` is shown, since its
/// text may quote hidden test source.
pub fn failure_results(bundle: Option<&Bundle>, error: &str) -> Results {
    let message = format!(
        "The autograder failed ({error}), so nothing was graded. Please tell your instructor."
    );

    let tests = match bundle {
        None => vec![failed(row("Autograder", 0.0, 0.0, message))],
        Some(bundle) => bundle
            .config
            .questions
            .iter()
            .filter(|q| !q.manual)
            .map(|q| {
                failed(Row {
                    number: Some(q.id.clone()),
                    ..row(&q.title, 0.0, q.points, message.clone())
                })
            })
            .collect(),
    };

    Results {
        tests,
        stdout_visibility: "hidden".into(),
        score: None,
        output: None,
        output_format: None,
    }
}

/// The 0-point row that shows messages about the upload.
pub fn notice_row(notices: &[String], text: &dyn PyText) -> Row {
    let capped: Vec<String> = notices.iter().map(|notice| text.cap(notice)).collect();

    failed(row(NOTICE_ROW, 0.0, 0.0, capped.join("\n\n")))
}

/// One visible row of `results.json`, without a number or status.
fn row(name: &str, score: f64, max_score: f64, output: String) -> Row {
    Row {
        name: name.into(),
        score,
        max_score,
        output,
        output_format: "md".into(),
        visibility: "visible".into(),
        number: None,
        status: None,
    }
}

/// `row` marked failed.
fn failed(row: Row) -> Row {
    Row {
        status: Some("failed".into()),
        ..row
    }
}

/// The total and each question's score, in Markdown.
fn summary(rows: &[Row], text: &dyn PyText) -> String {
    let score = rows.iter().fold(0.0, |sum, row| sum + row.score);
    let max_score = rows.iter().fold(0.0, |sum, row| sum + row.max_score);
    let mut lines = vec![
        format!("Total: {}", fraction(score, max_score, text)),
        String::new(),
    ];

    for row in rows {
        let shown = fraction(row.score, row.max_score, text);
        lines.push(format!("- {}: {shown}", row.name));
    }

    lines.join("\n")
}

/// `18.75 / 25`.
fn fraction(score: f64, max_score: f64, text: &dyn PyText) -> String {
    format!("{} / {}", text.g(text.round(score, 4)), text.g(max_score))
}

/// Markdown for tests: `All tests passed`, or each test with its failure message, as
/// `capped` holds it by test name.
fn output(public: &[&Outcome], hidden: &[&Outcome], capped: &IndexMap<&str, String>) -> String {
    if public.iter().chain(hidden).all(|outcome| outcome.passed) {
        return ALL_PASSED.into();
    }

    let labeled = public
        .iter()
        .map(|outcome| (outcome, ""))
        .chain(hidden.iter().map(|outcome| (outcome, " (hidden)")));
    let mut parts = Vec::new();

    for (outcome, label) in labeled {
        let mark = if outcome.passed { "✓" } else { "✗" };
        parts.push(format!("{mark} {}{label}", inline_code(&outcome.name)));

        if let Some(message) = capped.get(outcome.name.as_str()) {
            parts.push(code_block(message));
        }
    }

    parts.join("\n\n")
}

/// A fenced code block holding `text` verbatim; the fence outgrows every backtick run.
pub fn code_block(text: &str) -> String {
    let fence = "`".repeat((longest_backtick_run(text) + 1).max(3));

    format!("{fence}\n{text}\n{fence}")
}

/// `text` as one line of inline code that its own backticks cannot end.
///
/// Control characters, newlines included, become `?`.
pub fn inline_code(text: &str) -> String {
    let text: String = text
        .chars()
        .map(|c| match c {
            '\x00'..='\x1f' | '\x7f' => '?',
            c => c,
        })
        .collect();
    let fence = "`".repeat(longest_backtick_run(&text) + 1);

    match text.contains('`') {
        true => format!("{fence} {text} {fence}"),
        false => format!("{fence}{text}{fence}"),
    }
}

fn longest_backtick_run(text: &str) -> usize {
    text.split(|c| c != '`').map(str::len).max().unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fake::FakeHost;

    #[test]
    fn inline_code_outgrows_backticks_and_hides_control_characters() {
        assert_eq!(inline_code("a"), "`a`");
        assert_eq!(inline_code("a`b"), "`` a`b ``");
        assert_eq!(inline_code("x\ny\x7f"), "`x?y?`");
    }

    #[test]
    fn code_block_fence_outgrows_the_longest_run() {
        assert_eq!(code_block("a"), "```\na\n```");
        assert_eq!(code_block("````"), "`````\n````\n`````");
    }

    #[test]
    fn failure_results_without_a_bundle_have_one_row() {
        let results = failure_results(None, "RuntimeError");

        assert_eq!(results.tests.len(), 1);
        assert_eq!(results.tests[0].status.as_deref(), Some("failed"));
        assert!(results.tests[0].output.contains("(RuntimeError)"));
    }

    #[test]
    fn missing_outcome_is_a_key_error() {
        let bundle: Bundle = serde_json::from_value(serde_json::json!({
            "schema": 1,
            "config": {
                "name": "A", "notebook": "hw.py", "files": [], "timeout": 1.0, "budget": 9.0,
                "memory_mb": 1, "python": "3.13",
                "questions": [{"id": "q1", "title": "Q", "points": 1.0, "manual": false}],
            },
            "setup_code": "",
            "cells": [{
                "qid": "q1", "label": "q1-tests", "hidden": false, "source": "",
                "tests": ["test_q1"], "uses": [], "needs": [],
            }],
            "helpers": [],
        }))
        .unwrap();

        let error = build_results(&bundle, &IndexMap::new(), &[], &FakeHost::default());

        assert!(matches!(error, Err(GradeError::Key(name)) if name == "test_q1"));
    }
}
