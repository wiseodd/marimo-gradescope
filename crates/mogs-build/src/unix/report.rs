//! What `build` and `grade` print, and the failure lists of their errors.

use std::env;
use std::ffi::OsString;
use std::path::{Path, PathBuf};

use indexmap::{IndexMap, IndexSet};
use mogs_grader::GradeError;
use mogs_schema::{Bundle, FailureReason, Outcome};

use crate::unix::build::Built;
use crate::unix::regrade::Regraded;
use crate::unix::{Interpreter, text};

/// The files to upload and distribute, then the self-check scores and the warnings.
///
/// Each written question is listed with the first line of each of its prompt cells, so the
/// instructor can check which text goes into the PDF.
///
/// # Errors
///
/// Any Python exception.
pub fn report<I: Interpreter>(i: &I, built: &Built) -> Result<OsString, I::Error> {
    let mut rows = vec![["Question", "Public", "Hidden", "Solution", "Blank"].map(String::from)];
    let mut manual = Vec::new();

    for score in &built.scores {
        let question = &score.question;

        let (Some(solution), Some(blank)) = (score.solution, score.blank) else {
            let how = if question.points == 0.0 {
                "collected in the PDF, not graded".into()
            } else {
                format!("{}, graded by hand", points(i, question.points))
            };
            manual.push(format!("- {} {}: {how}", question.id, question.title));
            manual.extend(question.prompts.iter().map(|line| format!("    {line}")));
            continue;
        };

        rows.push([
            question.title.clone(),
            question.public.to_string(),
            question.hidden.to_string(),
            fraction(i, solution, question.points),
            fraction(i, blank, question.points),
        ]);
    }

    let (solution, blank) = (built.solution, built.blank);
    rows.push([
        "Total".into(),
        String::new(),
        String::new(),
        fraction(i, solution.0, solution.1),
        fraction(i, blank.0, blank.1),
    ]);
    let width = width(rows.iter().map(|row| &row[0]));
    let mut lines: Vec<String> = rows
        .iter()
        .map(|[name, public, hidden, solution, blank]| {
            format!("{name:<width$}  {public:>6}  {hidden:>6}  {solution:>9}  {blank:>9}")
        })
        .collect();

    if !manual.is_empty() {
        lines.extend(["".into(), "Not in the autograder total:".into()]);
        lines.extend(manual);
    }

    if !built.warnings.is_empty() {
        lines.extend(["".into(), "Warnings:".into()]);
        lines.extend(built.warnings.iter().map(|warning| format!("- {warning}")));
    }

    Ok(text(&[
        "All done!\nUpload to Gradescope  : ".as_ref(),
        shown(i, &built.zip)?.as_os_str(),
        "\nDistribute to students: ".as_ref(),
        shown(i, &built.student)?.as_os_str(),
        "\n\n".as_ref(),
        lines.join("\n").as_ref(),
    ]))
}

/// Per-question scores and the failing tests, hidden ones marked.
///
/// # Errors
///
/// Any Python exception.
pub fn grade_report<I: Interpreter>(i: &I, regraded: &Regraded) -> Result<OsString, I::Error> {
    let mut rows = vec![["Question".to_string(), "Score".to_string()]];

    for row in &regraded.graded.results.tests {
        let Some(number) = &row.number else {
            continue;
        };
        let title = regraded
            .bundle
            .question(number)
            .map(|question| question.title.clone())
            .expect("each numbered row is a question of the bundle");
        rows.push([title, fraction(i, row.score, row.max_score)]);
    }

    let (score, max) = regraded.score;
    rows.push(["Total".into(), fraction(i, score, max)]);
    let width = width(rows.iter().map(|row| &row[0]));
    let mut lines: Vec<String> = rows
        .iter()
        .map(|[name, score]| format!("{name:<width$}  {score:>9}"))
        .collect();

    let hidden: IndexSet<&str> = regraded
        .bundle
        .cells
        .iter()
        .filter(|cell| cell.hidden)
        .flat_map(|cell| cell.tests.iter().map(String::as_str))
        .collect();
    let failed: Vec<Outcome> = regraded
        .graded
        .outcomes
        .values()
        .filter(|outcome| !outcome.passed)
        .map(|outcome| match hidden.contains(outcome.name.as_str()) {
            true => Outcome {
                name: format!("{} (hidden)", outcome.name),
                ..outcome.clone()
            },
            false => outcome.clone(),
        })
        .collect();

    if !failed.is_empty() {
        lines.extend(["".into(), "Failing tests:".into()]);
        lines.extend(failure_report(i, &regraded.bundle, &failed, false)?);
    }

    Ok(text(&[
        "Graded:  ".as_ref(),
        regraded.submission.as_os_str(),
        "\n\n".as_ref(),
        lines.join("\n").as_ref(),
    ]))
}

/// One line per distinct failure message, naming the tests that share it.
///
/// With `count_load_failures`, the tests that failed to load are counted and their questions
/// named instead, since a failing setup cell fails every test.
///
/// # Errors
///
/// Any Python exception.
pub fn failure_report<I: Interpreter>(
    i: &I,
    bundle: &Bundle,
    failed: &[Outcome],
    count_load_failures: bool,
) -> Result<Vec<String>, I::Error> {
    let mut by_message: IndexMap<(String, bool), Vec<&str>> = IndexMap::new();

    for outcome in failed {
        let counted = count_load_failures && outcome.reason == Some(FailureReason::LoadFailed);
        let message = i.strip(&outcome.message)?;
        by_message
            .entry((message, counted))
            .or_default()
            .push(&outcome.name);
    }

    Ok(by_message
        .iter()
        .map(|((message, load_failed), names)| {
            let tests = failed_tests(bundle, names, *load_failed);
            format!("- {tests}: {}", message.replace('\n', "\n    "))
        })
        .collect())
}

/// A warning naming the tests that pass on the blank notebook, in questions worth points.
pub fn free_points_warnings(bundle: &Bundle, blank: &IndexMap<String, Outcome>) -> Vec<String> {
    let passing: Vec<&str> = blank
        .values()
        .filter(|outcome| outcome.passed)
        .filter(|outcome| {
            let qid = question_id(bundle, &outcome.name);
            let question = bundle.question(qid);
            question.is_some_and(|question| question.points > 0.0)
        })
        .map(|outcome| outcome.name.as_str())
        .collect();

    if passing.is_empty() {
        return Vec::new();
    }

    vec![format!(
        "These tests pass on the blank student notebook, so they give points for no work: {}",
        passing.join(", ")
    )]
}

fn failed_tests(bundle: &Bundle, names: &[&str], load_failed: bool) -> String {
    if !load_failed {
        return names.join(", ");
    }

    let questions: IndexSet<&str> = names.iter().map(|name| question_id(bundle, name)).collect();
    let tests = match names.len() {
        1 => "1 test".to_string(),
        n => format!("{n} tests"),
    };

    format!("{tests} of {}", Vec::from_iter(questions).join(", "))
}

/// The question of the test `name`: the one of the bundle's cell that has it.
fn question_id<'a>(bundle: &'a Bundle, name: &str) -> &'a str {
    bundle
        .cells
        .iter()
        .find(|cell| cell.tests.iter().any(|test| test == name))
        .map(|cell| cell.qid.as_str())
        .expect("each outcome is of a test of the bundle")
}

/// `path` as Python shows it, relative to the working directory when it is inside it.
pub(crate) fn shown<I: Interpreter>(i: &I, path: &Path) -> Result<PathBuf, I::Error> {
    let path = i.path(path)?;
    let cwd = env::current_dir().map_err(|error| i.grade_error(GradeError::from(error)))?;

    Ok(match path.strip_prefix(&cwd) {
        Ok(relative) => relative.to_path_buf(),
        Err(_) => path,
    })
}

/// The width of the longest name, in characters as Python counts them.
fn width<'a>(names: impl Iterator<Item = &'a String>) -> usize {
    names.map(|name| name.chars().count()).max().unwrap_or(0)
}

fn fraction<I: Interpreter>(i: &I, score: f64, points: f64) -> String {
    format!("{}/{}", i.host().g(score), i.host().g(points))
}

/// `25 pts`, `2.5 pts` or `1 pt`.
fn points<I: Interpreter>(i: &I, points: f64) -> String {
    let shown = i.host().g(points);

    if points == 1.0 {
        format!("{shown} pt")
    } else {
        format!("{shown} pts")
    }
}

#[cfg(test)]
mod tests {
    use mogs_schema::{BundleConfig, BundleQuestion, Results, Row, TestCell};

    use super::*;
    use crate::unix::build::Score;
    use crate::unix::fake::Fake;

    /// `text`, which is UTF-8 in tests.
    fn utf8(text: &std::ffi::OsStr) -> &str {
        text.to_str().expect("UTF-8 in tests")
    }
    use crate::unix::{Graded, Question};

    fn question(id: &str, title: &str, points: f64) -> Question {
        Question {
            id: id.into(),
            title: title.into(),
            points,
            public: 0,
            hidden: 0,
            prompts: Vec::new(),
        }
    }

    fn graded(question: Question) -> Score {
        let points = question.points;
        Score {
            question,
            solution: Some(points),
            blank: Some(0.0),
        }
    }

    fn by_hand(question: Question, prompts: &[&str]) -> Score {
        Score {
            question: Question {
                prompts: prompts.iter().map(|line| (*line).into()).collect(),
                ..question
            },
            solution: None,
            blank: None,
        }
    }

    fn built(dist: &Path, scores: Vec<Score>, warnings: &[&str]) -> Built {
        Built {
            student: dist.join("student/hw.zip"),
            zip: dist.join("autograder/hw-autograder-2026_09_30T12_30_41_890583.zip"),
            solution: (4.0, 4.0),
            blank: (0.0, 4.0),
            warnings: warnings.iter().map(|warning| (*warning).into()).collect(),
            scores,
        }
    }

    fn report_text(built: &Built) -> String {
        utf8(&report(&Fake::default(), built).unwrap()).to_string()
    }

    fn outcome(name: &str, passed: bool, message: &str, reason: Option<FailureReason>) -> Outcome {
        Outcome {
            name: name.into(),
            passed,
            message: message.into(),
            reason,
            seconds: 0.0,
        }
    }

    fn cell(qid: &str, hidden: bool, tests: &[impl AsRef<str>]) -> TestCell {
        TestCell {
            qid: qid.into(),
            label: format!("{qid}-tests"),
            hidden,
            source: String::new(),
            tests: tests.iter().map(|test| test.as_ref().into()).collect(),
            uses: Vec::new(),
            needs: Vec::new(),
        }
    }

    fn bundle(questions: &[(&str, &str, f64)], cells: Vec<TestCell>) -> Bundle {
        let questions = questions
            .iter()
            .map(|(id, title, points)| BundleQuestion {
                id: (*id).into(),
                title: (*title).into(),
                points: *points,
                manual: false,
            })
            .collect();
        Bundle {
            schema: mogs_schema::SCHEMA,
            config: BundleConfig {
                name: "hw".into(),
                notebook: "hw.py".into(),
                questions,
                files: Vec::new(),
                timeout: 60.0,
                budget: 600.0,
                memory_mb: 2048,
                python: "3.13".into(),
            },
            setup_code: String::new(),
            cells,
            helpers: Vec::new(),
        }
    }

    #[test]
    fn report_starts_with_what_to_do() {
        let cwd = env::current_dir().unwrap();

        let text = report_text(&built(&cwd.join("demo/dist"), Vec::new(), &[]));

        assert_eq!(
            text.lines().take(4).collect::<Vec<_>>(),
            [
                "All done!",
                "Upload to Gradescope  : demo/dist/autograder/hw-autograder-2026_09_30T12_30_41_890583.zip",
                "Distribute to students: demo/dist/student/hw.zip",
                "",
            ]
        );
    }

    #[test]
    fn report_shows_absolute_paths_outside_the_working_directory() {
        let text = report_text(&built(Path::new("/elsewhere/demo/dist"), Vec::new(), &[]));

        assert_eq!(
            text.lines().nth(2),
            Some("Distribute to students: /elsewhere/demo/dist/student/hw.zip")
        );
    }

    #[test]
    fn report_shows_scores_manual_questions_and_warnings() {
        let scores = vec![
            graded(question("q1", "Average", 4.0)),
            by_hand(question("q3", "Proof", 5.0), &["## Question 3", "(b) Then"]),
            by_hand(question("q4", "Essay", 1.0), &["no question text"]),
        ];

        let text = report_text(&built(Path::new("/x/dist"), scores, &["careful"]));

        assert_eq!(
            text.split_once("\n\n").unwrap().1,
            "Question  Public  Hidden   Solution      Blank\n\
             Average        0       0        4/4        0/4\n\
             Total                           4/4        0/4\n\
             \n\
             Not in the autograder total:\n\
             - q3 Proof: 5 pts, graded by hand\n    ## Question 3\n    (b) Then\n\
             - q4 Essay: 1 pt, graded by hand\n    no question text\n\
             \n\
             Warnings:\n\
             - careful"
        );
    }

    #[test]
    fn report_says_a_zero_point_manual_question_is_not_graded() {
        let scores = vec![by_hand(question("q5", "Proof", 0.0), &["Prove it."])];

        let text = report_text(&built(Path::new("/x/dist"), scores, &[]));

        assert!(text.ends_with(
            "Not in the autograder total:\n- q5 Proof: collected in the PDF, not graded\n    \
             Prove it."
        ));
    }

    #[test]
    fn report_shows_public_and_hidden_test_counts() {
        let scores = vec![graded(Question {
            public: 2,
            hidden: 3,
            ..question("q1", "Average", 4.0)
        })];

        let text = report_text(&built(Path::new("/x/dist"), scores, &[]));
        let row = text
            .lines()
            .find(|line| line.starts_with("Average"))
            .unwrap();

        assert_eq!(
            row.split_whitespace().collect::<Vec<_>>(),
            ["Average", "2", "3", "4/4", "0/4"]
        );
    }

    #[test]
    fn failure_report_merges_identical_messages() {
        let bundle = bundle(&[], vec![cell("q1", false, &["test_q1_a", "test_q1_b"])]);
        let failed = [
            outcome(
                "test_q1_a",
                false,
                "not run: crash",
                Some(FailureReason::NotRun),
            ),
            outcome(
                "test_q1_b",
                false,
                "not run: crash\n",
                Some(FailureReason::NotRun),
            ),
            outcome(
                "test_q2_a",
                false,
                "boom\nline two",
                Some(FailureReason::Error),
            ),
        ];

        let lines = failure_report(&Fake::default(), &bundle, &failed, true).unwrap();

        assert_eq!(
            lines,
            [
                "- test_q1_a, test_q1_b: not run: crash",
                "- test_q2_a: boom\n    line two"
            ]
        );
    }

    #[test]
    fn failure_report_names_questions_for_load_failures() {
        let setup = "the grader's setup code failed (ModuleNotFoundError: No module named 'torch')";
        let q1: Vec<String> = (0..20).map(|n| format!("test_q1_{n}")).collect();
        let q2: Vec<String> = (0..10).map(|n| format!("test_q2_{n}")).collect();
        let bundle = bundle(
            &[],
            vec![
                cell("q1", false, &q1),
                cell("q2", true, &q2),
                cell("q3", false, &["test_q3_a"]),
            ],
        );
        let mut failed: Vec<Outcome> = q1
            .iter()
            .chain(&q2)
            .map(|name| outcome(name, false, setup, Some(FailureReason::LoadFailed)))
            .collect();
        failed.push(outcome(
            "test_q3_a",
            false,
            "boom",
            Some(FailureReason::Error),
        ));
        let single = [outcome(
            "test_q3_a",
            false,
            "x",
            Some(FailureReason::LoadFailed),
        )];

        let lines = failure_report(&Fake::default(), &bundle, &failed, true).unwrap();
        let one = failure_report(&Fake::default(), &bundle, &single, true).unwrap();
        let named = failure_report(&Fake::default(), &bundle, &single, false).unwrap();

        assert_eq!(
            lines,
            [
                format!("- 30 tests of q1, q2: {setup}"),
                "- test_q3_a: boom".into()
            ]
        );
        assert_eq!(one, ["- 1 test of q3: x"]);
        assert_eq!(named, ["- test_q3_a: x"]);
    }

    #[test]
    fn free_points_warning_names_tests_that_pass_blank() {
        let bundle = bundle(
            &[("q1", "Q", 4.0), ("q2", "Practice", 0.0)],
            vec![
                cell("q1", false, &["test_q1_a", "test_q1_b", "test_q1_c"]),
                cell("q2", false, &["test_q2_a"]),
            ],
        );
        let outcomes: IndexMap<String, Outcome> = [
            outcome("test_q1_a", true, "", None),
            outcome("test_q1_b", false, "no", Some(FailureReason::Assertion)),
            outcome("test_q1_c", true, "", None),
            outcome("test_q2_a", true, "", None),
        ]
        .into_iter()
        .map(|outcome| (outcome.name.clone(), outcome))
        .collect();

        assert_eq!(
            free_points_warnings(&bundle, &outcomes),
            [
                "These tests pass on the blank student notebook, so they give points for no work: \
              test_q1_a, test_q1_c"
            ]
        );
        assert!(free_points_warnings(&bundle, &IndexMap::new()).is_empty());
    }

    #[test]
    fn grade_report_marks_hidden_failures() {
        let bundle = bundle(
            &[("q1", "Average", 4.0), ("q2", "Reading scores", 6.0)],
            vec![
                cell("q1", false, &["test_q1_a"]),
                cell("q1", true, &["test_q1_b"]),
                cell("q2", false, &["test_q2_a"]),
            ],
        );
        let row = |name: &str, number: Option<&str>, score: f64, max_score: f64| Row {
            name: name.into(),
            score,
            max_score,
            output: String::new(),
            output_format: "md".into(),
            visibility: "hidden".into(),
            number: number.map(String::from),
            status: None,
        };
        let outcomes = [
            outcome("test_q1_a", true, "", None),
            outcome(
                "test_q1_b",
                false,
                "assert 0 == -2\n",
                Some(FailureReason::Assertion),
            ),
            outcome("test_q2_a", true, "", None),
        ];
        let regraded = Regraded {
            submission: "/up/hw.py".into(),
            bundle,
            graded: Graded {
                outcomes: outcomes
                    .into_iter()
                    .map(|outcome| (outcome.name.clone(), outcome))
                    .collect(),
                load_error: None,
                results: Results {
                    tests: vec![
                        row("Public tests", None, 0.0, 0.0),
                        row("Average", Some("q1"), 2.0, 4.0),
                        row("Reading scores", Some("q2"), 6.0, 6.0),
                    ],
                    stdout_visibility: "hidden".into(),
                    score: None,
                    output: None,
                    output_format: None,
                },
            },
            score: (8.0, 10.0),
        };

        let text = grade_report(&Fake::default(), &regraded).unwrap();

        assert_eq!(
            utf8(&text),
            "Graded:  /up/hw.py\n\
             \n\
             Question            Score\n\
             Average               2/4\n\
             Reading scores        6/6\n\
             Total                8/10\n\
             \n\
             Failing tests:\n\
             - test_q1_b (hidden): assert 0 == -2"
        );
    }
}
