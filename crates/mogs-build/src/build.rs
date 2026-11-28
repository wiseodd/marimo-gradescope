//! Build the student notebook and the Gradescope autograder zip, then self-check them.
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
//! Before it writes anything, it grades the solved copy and the student copy with the real
//! grader, in a fresh environment with Gradescope's requirements. The solved copy must get full
//! marks. The student copy should get none; tests that pass on it come back as warnings.

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;

use mogs_grader::GradeError;
use mogs_schema::{Outcome, Results};

use crate::environment::grading_python;
use crate::templates::{RUN_AUTOGRADER, setup_script};
use crate::zipout::write_zip;
use crate::{Class, Interpreter, Question, grade_locally, process, report, total};

/// The output folder next to the source notebook.
pub const DIST: &str = "dist";
/// The parts of `DIST` a build replaces.
pub const PARTS: [&str; 2] = ["student", "autograder"];
/// Otter's timestamp in the zip name, e.g. 2026_09_30T12_30_41_890583.
const STAMP: &str = "%Y_%m_%dT%H_%M_%S_%f";

/// What a build wrote and how the self-check went.
#[derive(Clone, Debug, PartialEq)]
pub struct Built {
    /// The zip for students: the student notebook, its public tests and the data.
    pub student: PathBuf,
    /// The Gradescope autograder zip.
    pub zip: PathBuf,
    /// Score and maximum of the solved copy.
    pub solution: (f64, f64),
    /// Score and maximum of the student copy.
    pub blank: (f64, f64),
    /// Things the instructor should look at.
    pub warnings: Vec<String>,
    /// Per-question scores, in notebook order.
    pub scores: Vec<Score>,
}

/// Self-check scores of one question.
#[derive(Clone, Debug, PartialEq)]
pub struct Score {
    /// The question.
    pub question: Question,
    /// Score of the solved copy; `None` for a question graded by hand.
    pub solution: Option<f64>,
    /// Score of the student copy; `None` for a question graded by hand.
    pub blank: Option<f64>,
}

/// Build and self-check the assignment in `folder`.
///
/// The output is written in full to a temporary folder first. Only when every step succeeds
/// does it replace `dist/student/` and `dist/autograder/`, so only the newest zip is kept. The
/// caller removes them when the build fails.
///
/// # Errors
///
/// `BuildError` if the notebook is missing or invalid, `marimo check` fails on the student or
/// solved copy, or the solved copy does not get full marks; `ConfigError`, `SourceError` and
/// `RequirementsError` from Python; `OSError` if a file cannot be read or written.
pub fn build<I: Interpreter>(i: &I, folder: &Path) -> Result<Built, I::Error> {
    let assignment = i.read(folder)?;
    let inputs = i.build_inputs(&assignment)?;
    let bundle = &inputs.grading.bundle;
    let config = &bundle.config;
    let os = |error: io::Error, path: &Path| i.grade_error(GradeError::os(error, path));

    let setup = setup_script(i, &config.python)?;
    let mut data = Vec::new();

    for (rel, source) in &inputs.data {
        data.push((rel, fs::read(source).map_err(|e| os(e, source))?));
    }

    let mut entries: BTreeMap<String, &[u8]> = BTreeMap::from([
        ("setup.sh".into(), setup.as_bytes()),
        ("run_autograder".into(), RUN_AUTOGRADER.as_bytes()),
        ("bundle.json".into(), inputs.bundle_json.as_bytes()),
        ("requirements.txt".into(), inputs.requirements.as_bytes()),
    ]);
    entries.extend(
        data.iter()
            .map(|(rel, bytes)| (rel.to_string(), &bytes[..])),
    );

    // As pathlib shows it, so `mogs build .` names `dist`, not `./dist`.
    let dist = i.path(&folder.join(DIST))?;
    let zip_name = format!("{}-autograder-{}.zip", config.name, i.now(STAMP)?);
    let stage = tempfile::Builder::new()
        .prefix("mogs-build-")
        .tempdir()
        .map_err(|error| i.grade_error(GradeError::from(error)))?;
    let stage = stage.path();

    // Self-check, in an environment with what Gradescope installs.
    let python = grading_python(
        i,
        &inputs.grading.requirements,
        &stage.join("env"),
        &config.python,
    )?;
    let check = stage.join("check");
    let student_path = check.join("student").join(&config.notebook);
    let solved_path = check.join("solved").join(&config.notebook);
    fs::create_dir(&check).map_err(|e| os(e, &check))?;

    for (notebook, text) in [
        (&student_path, &inputs.student),
        (&solved_path, &inputs.solved),
    ] {
        let parent = notebook.parent().expect("the notebook is in a folder");
        fs::create_dir(parent).map_err(|e| os(e, parent))?;
        fs::write(notebook, text).map_err(|e| os(e, notebook))?;
    }

    let mut warnings = inputs.warnings.clone();
    warnings.extend(marimo_check(i, &student_path, "student notebook", &python)?);
    warnings.extend(marimo_check(i, &solved_path, "solved notebook", &python)?);

    // The solved copy, not the source: it is what the instructor's test upload sends.
    let solution = grade_locally(
        i,
        bundle,
        &solved_path,
        &check.join("solution"),
        folder,
        &python,
    )?;
    let failed: Vec<Outcome> = solution
        .outcomes
        .values()
        .filter(|outcome| !outcome.passed)
        .cloned()
        .collect();

    if !failed.is_empty() {
        let message = format!(
            "The solved copy of {} does not get full marks. Failing tests:\n{}",
            config.notebook,
            report::failure_report(i, bundle, &failed, true)?.join("\n")
        );
        return Err(i.raise(Class::Build, message.into()));
    }

    let blank = grade_locally(
        i,
        bundle,
        &student_path,
        &check.join("blank"),
        folder,
        &python,
    )?;
    warnings.extend(report::free_points_warnings(bundle, &blank.outcomes));

    // Output, written in full before it replaces the old one.
    let out = stage.join(DIST);
    let student_zip = out.join("student").join(format!("{}.zip", config.name));
    fs::create_dir_all(out.join("student")).map_err(|e| os(e, &out))?;

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
    let mut student: BTreeMap<String, &[u8]> = files
        .iter()
        .map(|(rel, content)| (format!("{}/{rel}", config.name), *content))
        .collect();
    let notebook = format!("{}/{}", config.name, config.notebook);
    student.insert(notebook, inputs.student.as_bytes());
    write_zip(i, &student_zip, &student)?;

    let autograder = out.join("autograder");
    files.insert(&config.notebook, inputs.solved.as_bytes());

    for (rel, content) in &files {
        let path = autograder.join(rel);
        let parent = path.parent().expect("the file is in a folder");
        fs::create_dir_all(parent).map_err(|e| os(e, parent))?;
        fs::write(&path, content).map_err(|e| os(e, &path))?;
    }

    let requirements = autograder.join("requirements.txt");
    fs::write(&requirements, &inputs.requirements).map_err(|e| os(e, &requirements))?;
    write_zip(i, &autograder.join(&zip_name), &entries)?;

    for part in PARTS {
        let target = dist.join(part);

        if target.exists() {
            fs::remove_dir_all(&target).map_err(|e| os(e, &target))?;
        }

        fs::create_dir_all(&dist).map_err(|e| os(e, &dist))?;
        move_folder(&out.join(part), &target).map_err(|e| os(e, &target))?;
    }

    Ok(Built {
        student: dist.join("student").join(format!("{}.zip", config.name)),
        zip: dist.join("autograder").join(zip_name),
        solution: total(i, &solution.results)?,
        blank: total(i, &blank.results)?,
        warnings,
        scores: scores(&inputs.questions, &solution.results, &blank.results),
    })
}

/// Run `marimo check` with `python` on `notebook`, called `label` in messages.
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
    python: &Path,
) -> Result<Vec<String>, I::Error> {
    let mut command = Command::new(python);
    command
        .args(["-m", "marimo", "check"])
        .arg(notebook.file_name().expect("the notebook has a name"))
        .current_dir(notebook.parent().expect("the notebook is in a folder"));
    let output = process::run(i, &mut command)?;
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

/// Each question's scores, from the rows of the two self-check results.
fn scores(questions: &[Question], solution: &Results, blank: &Results) -> Vec<Score> {
    let score = |results: &Results, id: &str| {
        let mut rows = results.tests.iter().rev();
        rows.find(|row| row.number.as_deref() == Some(id))
            .map(|row| row.score)
    };

    questions
        .iter()
        .map(|question| Score {
            question: question.clone(),
            solution: score(solution, &question.id),
            blank: score(blank, &question.id),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fake::{Fake, script, spawning};

    /// A fake Python in `folder` that prints `output` and exits with `code`.
    fn fake_python(folder: &Path, output: &str, code: i32) -> PathBuf {
        let python = folder.join("python");
        script(&python, &format!("printf '{output}'\nexit {code}"));

        python
    }

    #[test]
    fn marimo_check_warnings_are_passed_through() {
        let tmp = tempfile::tempdir().unwrap();
        let python = fake_python(tmp.path(), "warning: something odd\\n", 0);
        let _spawning = spawning();

        let warnings = marimo_check(
            &Fake::default(),
            &tmp.path().join("hw.py"),
            "solved notebook",
            &python,
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
        let python = fake_python(tmp.path(), "", 0);
        let _spawning = spawning();

        let warnings = marimo_check(
            &Fake::default(),
            &tmp.path().join("hw.py"),
            "student notebook",
            &python,
        )
        .unwrap();

        assert!(warnings.is_empty());
    }

    #[test]
    fn failing_marimo_check_fails_the_build() {
        let tmp = tempfile::tempdir().unwrap();
        let python = fake_python(tmp.path(), "error: broken\\n", 1);
        let _spawning = spawning();

        let error = marimo_check(
            &Fake::default(),
            &tmp.path().join("hw.py"),
            "student notebook",
            &python,
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
        let python = tmp.path().join("python");
        script(&python, "echo \"$PWD $*\"");
        let folder = tmp.path().canonicalize().unwrap();
        let _spawning = spawning();

        let warnings = marimo_check(
            &Fake::default(),
            &folder.join("hw.py"),
            "solved notebook",
            &python,
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
}
