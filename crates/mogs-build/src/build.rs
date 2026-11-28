//! Build the student notebook and the Gradescope autograder zip, after checking the solution.
//!
//! The build writes, next to the source notebook, the layout Otter uses:
//!
//! - `dist/student/<name>.zip`, for students: one `<name>/` folder with the student notebook,
//!   its public tests in `tests/` and the data files;
//! - `dist/autograder/<name>-autograder-<timestamp>.zip` with `setup.sh`, `run_autograder`,
//!   `bundle.json`, `requirements.txt` and the data files, for Gradescope;
//! - `dist/autograder/<notebook>.py`, the solved copy: the student notebook with the solutions
//!   kept, to upload to Gradescope as a test submission. The public tests, the data files and
//!   `requirements.txt` sit next to it.
//!
//! Before it writes anything, it runs `marimo check` on both copies and every test of the
//! bundle, public and hidden, on the solved copy, as `otter assign` runs the solution's tests.
//! Both run in the notebook's own environment (`environment`), as plain processes; the tests
//! get the assignment's `budget` in all. Every test must pass.

use std::collections::{BTreeMap, HashMap};
use std::ffi::OsString;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use mogs_schema::{Bundle, Outcome};

use crate::environment::notebook_python;
use crate::templates::{RUN_AUTOGRADER, setup_script};
use crate::zipout::{Deflated, write_zip};
use crate::{Class, Interpreter, Question, process, report, text};
use crate::{DIST, PARTS};

/// Otter's timestamp in the zip name, e.g. 2026_09_30T12_30_41_890583.
const STAMP: &str = "%Y_%m_%dT%H_%M_%S_%f";

/// What a build wrote.
#[derive(Clone, Debug, PartialEq)]
pub struct Built {
    /// The zip for students: the student notebook, its public tests and the data.
    pub student: PathBuf,
    /// The Gradescope autograder zip.
    pub zip: PathBuf,
    /// Things the instructor should look at.
    pub warnings: Vec<String>,
    /// The questions, in config order.
    pub questions: Vec<Question>,
}

/// Build the assignment in `folder`, after checking its solution.
///
/// The output is written in full to a temporary folder first. Only when every step succeeds
/// does it replace `dist/student/` and `dist/autograder/`, so only the newest zip is kept. The
/// caller removes them when the build fails.
///
/// # Errors
///
/// `BuildError` if the notebook is missing or invalid, `marimo check` fails on the student or
/// solved copy, or a test fails on the solved copy or they do not finish within the `budget`;
/// `ConfigError`, `SourceError` and `RequirementsError` from Python; `OSError` if a file
/// cannot be read or written.
pub fn build<I: Interpreter>(i: &I, folder: &Path) -> Result<Built, I::Error> {
    let assignment = i.read(folder)?;
    let inputs = i.build_inputs(&assignment)?;
    let bundle = &inputs.bundle;
    let config = &bundle.config;

    let setup = setup_script(&config.python);
    let mut data = Vec::new();

    for (rel, source) in &inputs.data {
        data.push((rel, fs::read(source).map_err(|e| i.os(e, source))?));
    }

    // Each data file goes into both zips, deflated once.
    let deflated = data
        .iter()
        .map(|(rel, bytes)| Ok((rel.as_str(), Deflated::new(i, bytes)?)))
        .collect::<Result<Vec<_>, I::Error>>()?;
    let scripts = [
        ("setup.sh", setup.as_bytes()),
        ("run_autograder", RUN_AUTOGRADER.as_bytes()),
        ("bundle.json", inputs.bundle_json.as_bytes()),
        ("requirements.txt", inputs.requirements.as_bytes()),
    ]
    .into_iter()
    .map(|(name, bytes)| Ok((name, Deflated::new(i, bytes)?)))
    .collect::<Result<Vec<_>, I::Error>>()?;
    let entries: BTreeMap<String, &Deflated> = scripts
        .iter()
        .chain(&deflated)
        .map(|(name, entry)| (name.to_string(), entry))
        .collect();

    // As pathlib shows it, so `mogs build .` names `dist`, not `./dist`.
    let dist = i.path(&folder.join(DIST))?;
    let zip_name = format!("{}-autograder-{}.zip", config.name, i.now(STAMP)?);
    let stage = tempfile::Builder::new()
        .prefix("mogs-build-")
        .tempdir()
        .map_err(|error| i.io(error))?;
    let stage = stage.path();

    // The check, in the notebook's own environment. The solved copy sits with the data files,
    // as on Gradescope.
    let python = notebook_python(i, &inputs.source)?;
    let check = stage.join("check");
    let student_path = check.join("student").join(&config.notebook);
    let solved_path = check.join("solved").join(&config.notebook);
    let bundle_path = check.join("bundle.json");
    fs::create_dir(&check).map_err(|e| i.os(e, &check))?;
    fs::write(&bundle_path, &inputs.bundle_json).map_err(|e| i.os(e, &bundle_path))?;

    for (notebook, text) in [
        (&student_path, &inputs.student),
        (&solved_path, &inputs.solved),
    ] {
        let parent = notebook.parent().expect("the notebook is in a folder");
        fs::create_dir(parent).map_err(|e| i.os(e, parent))?;
        fs::write(notebook, text).map_err(|e| i.os(e, notebook))?;
    }

    for (rel, bytes) in &data {
        let path = check.join("solved").join(rel);
        let parent = path.parent().expect("the file is in a folder");
        fs::create_dir_all(parent).map_err(|e| i.os(e, parent))?;
        fs::write(&path, bytes).map_err(|e| i.os(e, &path))?;
    }

    let mut warnings = inputs.warnings.clone();
    let student_check = marimo_check(i, &student_path, "student notebook", python.command())?;
    warnings.extend(student_check);
    let solved_check = marimo_check(i, &solved_path, "solved notebook", python.command())?;
    warnings.extend(solved_check);

    // The solved copy, not the source: it is what the instructor's test upload sends.
    check_solution(i, &solved_path, &bundle_path, bundle, python.command())?;

    // Output, written in full before it replaces the old one.
    let out = stage.join(DIST);
    let student_name = format!("{}.zip", config.name);
    let student_zip = out.join("student").join(&student_name);
    fs::create_dir_all(out.join("student")).map_err(|e| i.os(e, &out))?;

    let mut files: BTreeMap<&str, &[u8]> = data
        .iter()
        .map(|(rel, bytes)| (rel.as_str(), &bytes[..]))
        .collect();
    files.extend(
        inputs
            .tests
            .iter()
            .map(|(rel, text)| (rel.as_str(), text.as_bytes())),
    );
    let mut texts = inputs
        .tests
        .iter()
        .map(|(rel, text)| Ok((rel.as_str(), Deflated::new(i, text.as_bytes())?)))
        .collect::<Result<Vec<_>, I::Error>>()?;
    texts.push((
        config.notebook.as_str(),
        Deflated::new(i, inputs.student.as_bytes())?,
    ));
    // A test file takes the place of a data file of the same name, as in `files`.
    let mut student: BTreeMap<&str, &Deflated> =
        deflated.iter().map(|(rel, entry)| (*rel, entry)).collect();
    student.extend(texts.iter().map(|(rel, entry)| (*rel, entry)));
    let student: BTreeMap<String, &Deflated> = student
        .into_iter()
        .map(|(rel, entry)| (format!("{}/{rel}", config.name), entry))
        .collect();
    write_zip(i, &student_zip, &student)?;

    let autograder = out.join("autograder");
    files.insert(&config.notebook, inputs.solved.as_bytes());

    for (rel, content) in &files {
        let path = autograder.join(rel);
        let parent = path.parent().expect("the file is in a folder");
        fs::create_dir_all(parent).map_err(|e| i.os(e, parent))?;
        fs::write(&path, content).map_err(|e| i.os(e, &path))?;
    }

    let requirements = autograder.join("requirements.txt");
    fs::write(&requirements, &inputs.requirements).map_err(|e| i.os(e, &requirements))?;
    write_zip(i, &autograder.join(&zip_name), &entries)?;

    for part in PARTS {
        let target = dist.join(part);

        if target.exists() {
            fs::remove_dir_all(&target).map_err(|e| i.os(e, &target))?;
        }

        fs::create_dir_all(&dist).map_err(|e| i.os(e, &dist))?;
        move_folder(&out.join(part), &target).map_err(|e| i.os(e, &target))?;
    }

    Ok(Built {
        student: dist.join("student").join(student_name),
        zip: dist.join("autograder").join(zip_name),
        warnings,
        questions: questions(bundle, &inputs.prompts),
    })
}

/// Run `marimo check` on `notebook`, called `label` in messages, with `python`, a command that
/// runs Python.
///
/// Returns its output as a warning, if it printed anything.
///
/// # Errors
///
/// `BuildError` if it reports errors.
fn marimo_check<I: Interpreter>(
    i: &I,
    notebook: &Path,
    label: &str,
    mut python: Command,
) -> Result<Vec<String>, I::Error> {
    python
        .args(["-m", "marimo", "check"])
        .arg(notebook.file_name().expect("the notebook has a name"))
        .current_dir(notebook.parent().expect("the notebook is in a folder"));
    let output = process::run(i, &mut python)?;
    let text = i.decoded(&[&output.stdout, &output.stderr])?;

    if !output.status.success() {
        let message: OsString = format!("`marimo check` fails on the {label}:\n{text}").into();
        return Err(i.raise(Class::Build, message));
    }

    Ok(match text.is_empty() {
        true => Vec::new(),
        false => vec![format!("`marimo check` on the {label}:\n{text}")],
    })
}

/// Run every test of `bundle`, written to `bundle_path`, on the solved copy `notebook` with
/// `python`, a command that runs Python, for at most the bundle's `budget`.
///
/// # Errors
///
/// `BuildError` if a test fails, the tests do not finish in time, or they cannot run.
fn check_solution<I: Interpreter>(
    i: &I,
    notebook: &Path,
    bundle_path: &Path,
    bundle: &Bundle,
    mut python: Command,
) -> Result<(), I::Error> {
    let config = &bundle.config;
    python
        .args(["-m", "mogs.selfcheck", "--bundle"])
        .arg(bundle_path)
        .arg(notebook);
    // An infinite `budget` sets no limit.
    let limit = Duration::try_from_secs_f64(config.budget).ok();

    let Some(output) = process::run_within(i, &mut python, limit)? else {
        let message = format!(
            "The tests of the solved copy of {} did not finish within the assignment's `budget` \
             of {} s.",
            config.notebook,
            i.host().g(config.budget)
        );
        return Err(i.raise(Class::Build, message.into()));
    };

    let Some(outcomes) = outcomes(&output.stdout).filter(|_| output.status.success()) else {
        let stderr = i.decoded(&[&output.stderr])?;
        let lead = format!(
            "The tests of the solved copy of {} could not run:\n",
            config.notebook
        );
        return Err(i.raise(Class::Build, text(&[lead.as_ref(), stderr.as_ref()])));
    };

    let failed: Vec<&Outcome> = outcomes.iter().filter(|outcome| !outcome.passed).collect();

    if !failed.is_empty() {
        let message = format!(
            "The solved copy of {} does not get full marks. Failing tests:\n{}",
            config.notebook,
            report::failure_report(i, bundle, &failed)?.join("\n")
        );
        return Err(i.raise(Class::Build, message.into()));
    }

    Ok(())
}

/// The outcomes in the last line of `stdout`, which `mogs.selfcheck --bundle` prints.
fn outcomes(stdout: &[u8]) -> Option<Vec<Outcome>> {
    let text = std::str::from_utf8(stdout).ok()?;
    let last = text.lines().rev().find(|line| !line.trim().is_empty())?;
    let mut summary: serde_json::Value = serde_json::from_str(last).ok()?;

    serde_json::from_value(summary.get_mut("outcomes")?.take()).ok()
}

/// Move the folder `from` to `to`, copying it when they are on different file systems.
fn move_folder(from: &Path, to: &Path) -> io::Result<()> {
    match fs::rename(from, to) {
        Err(error) if error.kind() == io::ErrorKind::CrossesDevices => {
            copy_folder(from, to)?;
            fs::remove_dir_all(from)
        }
        moved => moved,
    }
}

fn copy_folder(from: &Path, to: &Path) -> io::Result<()> {
    fs::create_dir(to)?;

    for entry in fs::read_dir(from)? {
        let entry = entry?;
        let target = to.join(entry.file_name());

        if entry.file_type()?.is_dir() {
            copy_folder(&entry.path(), &target)?;
        } else {
            fs::copy(entry.path(), &target)?;
        }
    }

    Ok(())
}

/// The questions of `bundle`, in config order, with their tests counted and the `prompts`
/// of the manual ones.
fn questions(bundle: &Bundle, prompts: &HashMap<String, Vec<String>>) -> Vec<Question> {
    let count = |id: &str, hidden: bool| -> usize {
        let cells = bundle.cells.iter();
        cells
            .filter(|cell| cell.qid == id && cell.hidden == hidden)
            .map(|cell| cell.tests.len())
            .sum()
    };

    bundle
        .config
        .questions
        .iter()
        .map(|question| Question {
            id: question.id.clone(),
            title: question.title.clone(),
            points: question.points,
            public: count(&question.id, false),
            hidden: count(&question.id, true),
            manual: question.manual,
            prompts: prompts.get(&question.id).cloned().unwrap_or_default(),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fake::Fake;

    /// A bundle with the public test `test_q1_a`, the hidden test `test_q1_b` and `budget`.
    fn bundle(budget: f64) -> Bundle {
        mogs_schema::decode(&format!(
            r#"{{"schema": 1, "config": {{"name": "A", "notebook": "hw.py", "files": [],
                "timeout": 1, "budget": {budget}, "memory_mb": 1, "python": "3.13",
                "questions": [
                    {{"id": "q1", "title": "One", "points": 4, "manual": false}},
                    {{"id": "q2", "title": "Two", "points": 5, "manual": true}}]}},
                "setup_code": "", "helpers": [], "cells": [
                    {{"qid": "q1", "label": "q1-tests", "hidden": false, "source": "",
                     "tests": ["test_q1_a"], "uses": [], "needs": []}},
                    {{"qid": "q1", "label": "q1-tests", "hidden": true, "source": "",
                     "tests": ["test_q1_b"], "uses": [], "needs": []}}]}}"#
        ))
        .unwrap()
    }

    #[test]
    fn questions_count_the_tests_of_the_bundle() {
        let prompts = HashMap::from([("q2".to_string(), vec!["## Two".to_string()])]);

        let found = questions(&bundle(1.0), &prompts);

        let counted: Vec<_> = found
            .iter()
            .map(|q| {
                (
                    q.id.as_str(),
                    q.points,
                    q.public,
                    q.hidden,
                    q.manual,
                    q.prompts.clone(),
                )
            })
            .collect();
        assert_eq!(
            counted,
            [
                ("q1", 4.0, 1, 1, false, vec![]),
                ("q2", 5.0, 0, 0, true, vec!["## Two".to_string()]),
            ]
        );
    }

    #[test]
    fn outcomes_come_from_the_last_line() {
        let stdout = b"noise\n{\"outcomes\": [{\"name\": \"test_q1_a\", \"passed\": true, \
                       \"message\": \"\", \"reason\": null, \"seconds\": 0.5}]}\n\n";

        let found = outcomes(stdout).unwrap();

        assert_eq!(found.len(), 1);
        assert_eq!(found[0].name, "test_q1_a");
        assert!(found[0].passed);
        assert!(outcomes(b"{\"outcomes\": 1}\n").is_none());
        assert!(outcomes(b"").is_none());
    }

    #[test]
    fn move_folder_copies_across_file_systems() {
        let tmp = tempfile::tempdir().unwrap();
        let from = tmp.path().join("from");
        fs::create_dir_all(from.join("sub")).unwrap();
        fs::write(from.join("sub/a.txt"), "a").unwrap();

        copy_folder(&from, &tmp.path().join("to")).unwrap();
        move_folder(&from, &tmp.path().join("moved")).unwrap();

        assert_eq!(
            fs::read_to_string(tmp.path().join("to/sub/a.txt")).unwrap(),
            "a"
        );
        assert_eq!(
            fs::read_to_string(tmp.path().join("moved/sub/a.txt")).unwrap(),
            "a"
        );
        assert!(!from.exists());
    }

    #[cfg(unix)]
    mod with_python {
        use std::time::Instant;

        use super::*;
        use crate::fake::{script, spawning};

        /// A fake Python in `folder` that runs the shell `body`.
        fn fake_python(folder: &Path, body: &str) -> Command {
            let python = folder.join("python");
            script(&python, body);

            Command::new(python)
        }

        /// What the fake selfcheck prints for test `name`.
        fn outcome(name: &str, passed: bool, message: &str) -> String {
            let reason = if passed { "null" } else { "\"assertion\"" };
            format!(
                "{{\"name\": \"{name}\", \"passed\": {passed}, \"message\": \"{message}\", \
                 \"reason\": {reason}, \"seconds\": 0.0}}"
            )
        }

        fn checked(tmp: &Path, body: &str, budget: f64) -> Result<(), String> {
            let python = fake_python(tmp, body);
            let _spawning = spawning();

            check_solution(
                &Fake::default(),
                &tmp.join("hw.py"),
                &tmp.join("bundle.json"),
                &bundle(budget),
                python,
            )
        }

        #[test]
        fn marimo_check_warnings_are_passed_through() {
            let tmp = tempfile::tempdir().unwrap();
            let python = fake_python(tmp.path(), "printf 'warning: something odd\\n'");
            let _spawning = spawning();

            let warnings = marimo_check(
                &Fake::default(),
                &tmp.path().join("hw.py"),
                "solved notebook",
                python,
            )
            .unwrap();

            assert_eq!(
                warnings,
                ["`marimo check` on the solved notebook:\nwarning: something odd"]
            );
        }

        #[test]
        fn silent_marimo_check_gives_no_warning() {
            let tmp = tempfile::tempdir().unwrap();
            let python = fake_python(tmp.path(), "exit 0");
            let _spawning = spawning();

            let warnings = marimo_check(
                &Fake::default(),
                &tmp.path().join("hw.py"),
                "student notebook",
                python,
            )
            .unwrap();

            assert!(warnings.is_empty());
        }

        #[test]
        fn failing_marimo_check_fails_the_build() {
            let tmp = tempfile::tempdir().unwrap();
            let python = fake_python(tmp.path(), "printf 'error: broken\\n'\nexit 1");
            let _spawning = spawning();

            let error = marimo_check(
                &Fake::default(),
                &tmp.path().join("hw.py"),
                "student notebook",
                python,
            )
            .unwrap_err();

            assert_eq!(
                error,
                "Build: `marimo check` fails on the student notebook:\nerror: broken"
            );
        }

        #[test]
        fn marimo_check_runs_in_the_notebook_folder() {
            let tmp = tempfile::tempdir().unwrap();
            let python = fake_python(tmp.path(), "echo \"$PWD $*\"");
            let folder = tmp.path().canonicalize().unwrap();
            let _spawning = spawning();

            let warnings = marimo_check(
                &Fake::default(),
                &folder.join("hw.py"),
                "solved notebook",
                python,
            )
            .unwrap();

            assert_eq!(
                warnings,
                [format!(
                    "`marimo check` on the solved notebook:\n{} -m marimo check hw.py",
                    folder.display()
                )]
            );
        }

        #[test]
        fn passing_solution_is_checked_with_the_bundle() {
            let tmp = tempfile::tempdir().unwrap();
            let args = tmp.path().join("args");
            let body = format!(
                "echo \"$*\" > '{}'\necho '{{\"outcomes\": [{}, {}]}}'",
                args.display(),
                outcome("test_q1_a", true, ""),
                outcome("test_q1_b", true, ""),
            );

            checked(tmp.path(), &body, 60.0).unwrap();

            assert_eq!(
                fs::read_to_string(&args).unwrap(),
                format!(
                    "-m mogs.selfcheck --bundle {} {}\n",
                    tmp.path().join("bundle.json").display(),
                    tmp.path().join("hw.py").display()
                )
            );
        }

        #[test]
        fn failing_hidden_test_fails_the_build_naming_it() {
            let tmp = tempfile::tempdir().unwrap();
            let body = format!(
                "echo 'a print'\necho '{{\"outcomes\": [{}, {}]}}'",
                outcome("test_q1_a", true, ""),
                outcome("test_q1_b", false, "assert 1 == 2"),
            );

            let error = checked(tmp.path(), &body, 60.0).unwrap_err();

            assert_eq!(
                error,
                "Build: The solved copy of hw.py does not get full marks. Failing tests:\n\
                 - test_q1_b: assert 1 == 2"
            );
        }

        #[test]
        fn hanging_solution_hits_the_budget() {
            let tmp = tempfile::tempdir().unwrap();
            let started = Instant::now();

            let error = checked(tmp.path(), "exec sleep 30", 0.3).unwrap_err();

            assert_eq!(
                error,
                "Build: The tests of the solved copy of hw.py did not finish within the \
                 assignment's `budget` of 0.3 s."
            );
            assert!(started.elapsed() < Duration::from_secs(5));
        }

        #[test]
        fn tests_that_cannot_run_show_stderr() {
            let tmp = tempfile::tempdir().unwrap();

            let error =
                checked(tmp.path(), "echo 'No module named mogs' >&2\nexit 1", 60.0).unwrap_err();

            assert_eq!(
                error,
                "Build: The tests of the solved copy of hw.py could not run:\n\
                 No module named mogs"
            );
        }
    }
}
