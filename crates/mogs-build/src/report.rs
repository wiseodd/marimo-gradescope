//! What `build` prints, and the failure lists of its errors.

use std::env;
use std::ffi::OsString;
use std::path::{Path, PathBuf};

use indexmap::{IndexMap, IndexSet};
use mogs_schema::{Bundle, FailureReason, Outcome};

use crate::build::Built;
use crate::{Interpreter, text};

/// The files to upload and distribute, then the questions with their tests and points, and
/// the warnings.
///
/// Each written question is listed with the first line of each of its prompt cells, so the
/// instructor can check which text goes into the PDF.
///
/// # Errors
///
/// Any Python exception.
pub fn report<I: Interpreter>(i: &I, built: &Built) -> Result<OsString, I::Error> {
    let mut rows = vec![["Question", "Public", "Hidden", "Points"].map(String::from)];
    let mut manual = Vec::new();
    let mut graded = Vec::new();

    for question in &built.questions {
        if question.manual {
            let how = if question.points == 0.0 {
                "collected in the PDF, not graded".into()
            } else {
                format!("{}, graded by hand", points(i, question.points))
            };
            manual.push(format!("- {} {}: {how}", question.id, question.title));
            manual.extend(question.prompts.iter().map(|line| format!("    {line}")));
            continue;
        }

        graded.push(question.points);
        rows.push([
            question.title.clone(),
            question.public.to_string(),
            question.hidden.to_string(),
            i.host().g(question.points),
        ]);
    }

    let total = i.host().g(i.sum(&graded)?);
    rows.push(["Total".into(), String::new(), String::new(), total]);
    let width = width(rows.iter().map(|row| &row[0]));
    let mut lines: Vec<String> = rows
        .iter()
        .map(|[name, public, hidden, points]| {
            format!("{name:<width$}  {public:>6}  {hidden:>6}  {points:>6}")
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

/// One line per distinct failure message, naming the tests that share it.
///
/// Tests that failed to load are counted, with their questions named, since a failing setup
/// cell fails every test.
///
/// # Errors
///
/// Any Python exception.
pub fn failure_report<I: Interpreter>(
    i: &I,
    bundle: &Bundle,
    failed: &[&Outcome],
) -> Result<Vec<String>, I::Error> {
    let mut by_message: IndexMap<(String, bool), Vec<String>> = IndexMap::new();

    for outcome in failed {
        let counted = outcome.reason == Some(FailureReason::LoadFailed);
        let message = i.strip(&outcome.message)?;
        by_message
            .entry((message, counted))
            .or_default()
            .push(outcome.name.clone());
    }

    Ok(by_message
        .iter()
        .map(|((message, load_failed), names)| {
            let tests = failed_tests(bundle, names, *load_failed);
            format!("- {tests}: {}", message.replace('\n', "\n    "))
        })
        .collect())
}

fn failed_tests(bundle: &Bundle, names: &[String], load_failed: bool) -> String {
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
    let cwd = env::current_dir().map_err(|error| i.io(error))?;

    Ok(match path.strip_prefix(&cwd) {
        Ok(relative) => relative.to_path_buf(),
        Err(_) => path,
    })
}

/// The width of the longest name, in characters as Python counts them.
fn width<'a>(names: impl Iterator<Item = &'a String>) -> usize {
    names.map(|name| name.chars().count()).max().unwrap_or(0)
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
    use mogs_schema::{BundleConfig, BundleQuestion, TestCell};

    use super::*;
    use crate::Question;
    use crate::fake::Fake;

    /// `text`, which is UTF-8 in tests.
    fn utf8(text: &std::ffi::OsStr) -> &str {
        text.to_str().expect("UTF-8 in tests")
    }

    fn question(id: &str, title: &str, points: f64) -> Question {
        Question {
            id: id.into(),
            title: title.into(),
            points,
            public: 0,
            hidden: 0,
            manual: false,
            prompts: Vec::new(),
        }
    }

    fn by_hand(question: Question, prompts: &[&str]) -> Question {
        Question {
            manual: true,
            prompts: prompts.iter().map(|line| (*line).into()).collect(),
            ..question
        }
    }

    fn built(dist: &Path, questions: Vec<Question>, warnings: &[&str]) -> Built {
        Built {
            student: dist.join("student/hw.zip"),
            zip: dist.join("autograder/hw-autograder-2026_09_30T12_30_41_890583.zip"),
            warnings: warnings.iter().map(|warning| (*warning).into()).collect(),
            questions,
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

        let zip = native("demo/dist/autograder/hw-autograder-2026_09_30T12_30_41_890583.zip");
        let student = native("demo/dist/student/hw.zip");

        assert_eq!(
            text.lines().take(4).collect::<Vec<_>>(),
            [
                "All done!",
                &format!("Upload to Gradescope  : {zip}"),
                &format!("Distribute to students: {student}"),
                "",
            ]
        );
    }

    #[test]
    fn report_shows_absolute_paths_outside_the_working_directory() {
        let text = report_text(&built(Path::new("/elsewhere/demo/dist"), Vec::new(), &[]));

        let student = native("/elsewhere/demo/dist/student/hw.zip");

        assert_eq!(
            text.lines().nth(2),
            Some(format!("Distribute to students: {student}").as_str())
        );
    }

    /// `path` with the separators of this platform, as the host shows paths.
    fn native(path: &str) -> String {
        Path::new(path)
            .components()
            .collect::<PathBuf>()
            .display()
            .to_string()
    }

    #[test]
    fn report_shows_points_manual_questions_and_warnings() {
        let questions = vec![
            question("q1", "Average", 4.0),
            question("q2", "Reading scores", 2.5),
            by_hand(question("q3", "Proof", 5.0), &["## Question 3", "(b) Then"]),
            by_hand(question("q4", "Essay", 1.0), &["no question text"]),
        ];

        let text = report_text(&built(Path::new("/x/dist"), questions, &["careful"]));

        assert_eq!(
            text.split_once("\n\n").unwrap().1,
            "Question        Public  Hidden  Points\n\
             Average              0       0       4\n\
             Reading scores       0       0     2.5\n\
             Total                              6.5\n\
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
        let questions = vec![by_hand(question("q5", "Proof", 0.0), &["Prove it."])];

        let text = report_text(&built(Path::new("/x/dist"), questions, &[]));

        assert!(text.ends_with(
            "Not in the autograder total:\n- q5 Proof: collected in the PDF, not graded\n    \
             Prove it."
        ));
    }

    #[test]
    fn report_shows_public_and_hidden_test_counts() {
        let questions = vec![Question {
            public: 2,
            hidden: 3,
            ..question("q1", "Average", 4.0)
        }];

        let text = report_text(&built(Path::new("/x/dist"), questions, &[]));
        let row = text
            .lines()
            .find(|line| line.starts_with("Average"))
            .unwrap();

        assert_eq!(
            row.split_whitespace().collect::<Vec<_>>(),
            ["Average", "2", "3", "4"]
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

        let failed: Vec<&Outcome> = failed.iter().collect();
        let lines = failure_report(&Fake::default(), &bundle, &failed).unwrap();

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

        let failed: Vec<&Outcome> = failed.iter().collect();
        let single: Vec<&Outcome> = single.iter().collect();
        let lines = failure_report(&Fake::default(), &bundle, &failed).unwrap();
        let one = failure_report(&Fake::default(), &bundle, &single).unwrap();

        assert_eq!(
            lines,
            [
                format!("- 30 tests of q1, q2: {setup}"),
                "- test_q3_a: boom".into()
            ]
        );
        assert_eq!(one, ["- 1 test of q3: x"]);
    }
}
