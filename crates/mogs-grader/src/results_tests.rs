//! `build_results` and `failure_results`: rows, scores and what students see.

use indexmap::IndexMap;
use mogs_schema::{Bundle, FailureReason, Outcome, Results, Row};

use crate::PyText;
use crate::fake::FakeHost;
use crate::results::{build_results, failure_results};

const PUBLIC: [&str; 2] = ["test_q1_a", "test_q1_b"];
const HIDDEN: [&str; 2] = ["test_q1_secret_x", "test_q1_secret_y"];

/// A question: id, title, points and whether it is graded by hand.
type Question<'a> = (&'a str, &'a str, f64, bool);
/// A test cell: question, whether it is hidden, and its tests.
type Cell<'a> = (&'a str, bool, &'a [&'a str]);

fn bundle_of(questions: &[Question], cells: &[Cell]) -> Bundle {
    let listed: Vec<_> = questions
        .iter()
        .map(|(id, title, _, manual)| {
            serde_json::json!({"id": id, "title": title, "points": 0.0, "manual": manual})
        })
        .collect();
    let cells: Vec<_> = cells
        .iter()
        .map(|(qid, hidden, tests)| {
            serde_json::json!({
                "qid": qid, "label": format!("{qid}-tests"), "hidden": hidden, "source": "",
                "tests": tests, "uses": [], "needs": [],
            })
        })
        .collect();
    let mut bundle: Bundle = serde_json::from_value(serde_json::json!({
        "schema": 1,
        "config": {
            "name": "A", "notebook": "hw.py", "files": [], "timeout": 60.0, "budget": 600.0,
            "memory_mb": 1024, "python": "3.13", "questions": listed,
        },
        "setup_code": "",
        "cells": cells,
        "helpers": [],
    }))
    .unwrap();

    // Points go in after JSON, which has no infinity.
    for (question, (_, _, points, _)) in bundle.config.questions.iter_mut().zip(questions) {
        question.points = *points;
    }

    bundle
}

/// Addition (25 points) with public and hidden tests, and a manual Essay (5 points).
fn bundle() -> Bundle {
    bundle_of(
        &[("q1", "Addition", 25.0, false), ("q2", "Essay", 5.0, true)],
        &[("q1", false, &PUBLIC), ("q1", true, &HIDDEN)],
    )
}

fn passed(name: &str) -> Outcome {
    Outcome {
        name: name.into(),
        passed: true,
        message: String::new(),
        reason: None,
        seconds: 0.0,
    }
}

fn all_passed(names: &[&str]) -> IndexMap<String, Outcome> {
    names
        .iter()
        .map(|name| ((*name).into(), passed(name)))
        .collect()
}

/// Every test of `bundle()` passed but `failing`, which failed with `message`.
fn outcomes(failing: &str, message: &str) -> IndexMap<String, Outcome> {
    let mut result = all_passed(&[PUBLIC, HIDDEN].concat());
    result.insert(
        failing.into(),
        Outcome {
            passed: false,
            message: message.into(),
            reason: Some(FailureReason::Assertion),
            ..passed(failing)
        },
    );

    result
}

fn usual() -> IndexMap<String, Outcome> {
    outcomes("test_q1_secret_y", "SECRET hidden detail")
}

fn results(bundle: &Bundle, outcomes: &IndexMap<String, Outcome>, notices: &[&str]) -> Results {
    let notices: Vec<String> = notices.iter().map(|notice| (*notice).into()).collect();

    build_results(bundle, outcomes, &notices, &FakeHost::default()).unwrap()
}

fn question_row<'a>(results: &'a Results, qid: &str) -> &'a Row {
    let rows: Vec<&Row> = results
        .tests
        .iter()
        .filter(|row| row.number.as_deref() == Some(qid))
        .collect();
    let [row] = rows[..] else {
        panic!("one row of {qid}: {rows:?}");
    };

    row
}

fn public_row(results: &Results) -> &Row {
    let rows: Vec<&Row> = results
        .tests
        .iter()
        .filter(|row| row.name == "Public tests")
        .collect();
    let [row] = rows[..] else {
        panic!("one public row: {rows:?}");
    };

    row
}

#[test]
fn infinite_points_show_as_python_does() {
    let grading = bundle_of(
        &[("q1", "Addition", f64::INFINITY, false)],
        &[("q1", true, &PUBLIC)],
    );

    let results = results(&grading, &all_passed(&PUBLIC), &[]);

    let row = question_row(&results, "q1");
    assert_eq!((row.score, row.max_score), (f64::INFINITY, f64::INFINITY));
    assert_eq!(
        results.output.as_deref(),
        Some("Total: inf / inf\n\n- Addition: inf / inf")
    );
}

#[test]
fn three_of_four_tests_give_three_quarters_of_the_points() {
    let results = results(&bundle(), &usual(), &[]);

    let row = question_row(&results, "q1");
    assert_eq!(row.name, "Addition");
    assert_eq!((row.score, row.max_score), (18.75, 25.0));
    assert_eq!(row.output_format, "md");
    assert_eq!(results.stdout_visibility, "hidden");
}

#[test]
fn score_is_rounded() {
    let tests = ["test_q1_a", "test_q1_b", "test_q1_c"];
    let grading = bundle_of(&[("q1", "Q", 1.0, false)], &[("q1", false, &tests)]);
    let mut outcomes = all_passed(&tests);
    for name in ["test_q1_b", "test_q1_c"] {
        outcomes[name].passed = false;
    }

    assert_eq!(
        question_row(&results(&grading, &outcomes, &[]), "q1").score,
        0.3333
    );
}

#[test]
fn manual_questions_get_no_row_and_question_rows_are_hidden() {
    let results = results(&bundle(), &usual(), &[]);

    let numbers: Vec<Option<&str>> = results.tests.iter().map(|r| r.number.as_deref()).collect();
    assert_eq!(numbers, [None, Some("q1")]);
    assert_eq!(question_row(&results, "q1").visibility, "hidden");
}

#[test]
fn hidden_names_and_messages_reach_only_hidden_rows() {
    let results = results(&bundle(), &usual(), &[]);

    let shown: Vec<&Row> = results
        .tests
        .iter()
        .filter(|r| r.visibility != "hidden")
        .collect();
    let text = format!("{:?} {shown:?}", results.output);
    for secret in ["SECRET", "secret", "Hidden"] {
        assert!(!text.contains(secret), "{text}");
    }

    let output = &question_row(&results, "q1").output;
    assert!(output.contains("✗ `test_q1_secret_y`"), "{output}");
    assert!(output.contains("SECRET hidden detail"), "{output}");
}

#[test]
fn public_row_is_visible_and_worth_nothing() {
    let results = results(
        &bundle(),
        &outcomes("test_q1_b", "AssertionError: nope"),
        &[],
    );

    let row = public_row(&results);
    assert_eq!(row.visibility, "visible");
    assert_eq!((row.score, row.max_score), (0.0, 0.0));
    assert_eq!(row.number, None);
    for part in [
        "Addition",
        "✓ `test_q1_a`",
        "✗ `test_q1_b`",
        "AssertionError: nope",
    ] {
        assert!(row.output.contains(part), "{}", row.output);
    }
}

#[test]
fn public_row_status_follows_public_tests_only() {
    let public_failed = results(&bundle(), &outcomes("test_q1_b", "no"), &[]);
    let hidden_failed = results(&bundle(), &usual(), &[]);

    assert_eq!(public_row(&public_failed).status.as_deref(), Some("failed"));
    assert_eq!(public_row(&hidden_failed).status, None);
}

#[test]
fn question_row_lists_public_and_hidden_tests() {
    let results = results(&bundle(), &usual(), &[]);

    let output = &question_row(&results, "q1").output;
    for name in [PUBLIC, HIDDEN].concat() {
        assert!(output.contains(&format!("`{name}`")), "{output}");
    }
}

#[test]
fn all_passing_tests_are_summed_up() {
    let results = results(&bundle(), &all_passed(&[PUBLIC, HIDDEN].concat()), &[]);

    assert!(public_row(&results).output.contains("All tests passed"));
    assert!(
        question_row(&results, "q1")
            .output
            .contains("All tests passed")
    );
    assert!(!public_row(&results).output.contains("test_q1_a"));
}

#[test]
fn summary_shows_the_total_and_each_question_score() {
    let results = results(&bundle(), &usual(), &[]);

    let output = results.output.unwrap();
    assert_eq!(results.output_format.as_deref(), Some("md"));
    assert!(output.contains("Total: 18.75 / 25"), "{output}");
    assert!(output.contains("Addition: 18.75 / 25"), "{output}");
    assert!(!output.contains("Essay"), "{output}");
}

#[test]
fn question_with_only_hidden_tests_is_left_out_of_the_public_row() {
    let grading = bundle_of(
        &[
            ("q1", "Addition", 25.0, false),
            ("q2", "Secret", 5.0, false),
        ],
        &[("q1", false, &PUBLIC), ("q2", true, &["test_q2_x"])],
    );
    let outcomes = all_passed(&["test_q1_a", "test_q1_b", "test_q2_x"]);

    let results = results(&grading, &outcomes, &[]);

    assert!(!public_row(&results).output.contains("Secret"));
    assert_eq!(question_row(&results, "q2").score, 5.0);
}

#[test]
fn no_public_tests_means_no_public_row() {
    let grading = bundle_of(&[("q1", "Q", 1.0, false)], &[("q1", true, &HIDDEN)]);

    let results = results(&grading, &all_passed(&HIDDEN), &[]);

    let numbers: Vec<Option<&str>> = results.tests.iter().map(|r| r.number.as_deref()).collect();
    assert_eq!(numbers, [Some("q1")]);
}

#[test]
fn message_cannot_close_its_code_fence() {
    let message = "x\n````\n# Injected heading\n````";

    let results = results(&bundle(), &outcomes("test_q1_b", message), &[]);

    let fence = "`````";
    let output = &public_row(&results).output;
    assert!(
        output.contains(&format!("{fence}\n{message}\n{fence}")),
        "{output}"
    );
}

/// `FakeHost`, but `cap` keeps only the first 10 characters.
struct Capping(FakeHost);

impl PyText for Capping {
    fn g(&self, x: f64) -> String {
        self.0.g(x)
    }

    fn repr(&self, text: &str) -> String {
        self.0.repr(text)
    }

    fn round(&self, x: f64, digits: i32) -> f64 {
        self.0.round(x, digits)
    }

    fn cap(&self, text: &str) -> String {
        text.chars().take(10).collect()
    }

    fn casefold(&self, text: &str) -> String {
        self.0.casefold(text)
    }

    fn signal_name(&self, number: i32) -> String {
        self.0.signal_name(number)
    }
}

#[test]
fn messages_and_notices_are_capped() {
    let huge = "y".repeat(1_000_000);
    let notices = ["No PDF found.".to_string(), "z".repeat(1_000_000)];

    let results = build_results(
        &bundle(),
        &outcomes("test_q1_b", &huge),
        &notices,
        &Capping(FakeHost::default()),
    )
    .unwrap();

    assert!(
        public_row(&results)
            .output
            .contains(&format!("```\n{}\n```", "y".repeat(10)))
    );
    assert!(
        results.tests[0].output.len() < 100,
        "{}",
        results.tests[0].output
    );
}

#[test]
fn notices_come_first_in_a_zero_point_row() {
    let results = results(&bundle(), &usual(), &["No PDF found."]);

    let [notice, public, question] = &results.tests[..] else {
        panic!("three rows: {:?}", results.tests);
    };
    assert_eq!(notice.name, "Submission");
    assert_eq!((notice.score, notice.max_score), (0.0, 0.0));
    assert!(notice.output.contains("No PDF found."));
    assert_eq!(notice.status.as_deref(), Some("failed"));
    assert_eq!(public.name, "Public tests");
    assert_eq!(question.status, None);
}

#[test]
fn top_level_score_only_for_an_all_manual_assignment() {
    let manual = bundle_of(&[("q1", "Essay", 5.0, true)], &[]);

    let results_manual = results(&manual, &IndexMap::new(), &[]);
    let graded = results(&bundle(), &usual(), &[]);

    assert!(results_manual.tests.is_empty());
    assert_eq!(results_manual.score, Some(0.0));
    assert_eq!(results_manual.output, None);
    assert_eq!(graded.score, None);
}

#[test]
fn failure_rows_show_as_failed() {
    for grading in [Some(bundle()), None] {
        let results = failure_results(grading.as_ref(), "RuntimeError");

        assert!(!results.tests.is_empty());
        assert!(
            results
                .tests
                .iter()
                .all(|row| row.status.as_deref() == Some("failed"))
        );
        assert!(results.tests.iter().all(|row| row.visibility == "visible"));
    }
}
