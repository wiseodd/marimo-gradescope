//! Gradescope's side: grade the upload and build `results.json`.

use std::ffi::OsStr;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use indexmap::IndexMap;
use mogs_schema::{Bundle, FailureReason, Results};

use crate::results::{build_results, inline_code};
use crate::sandbox::{GradeOptions, grade};
use crate::submission::{Answers, find_submission};
use crate::watch::fail;
use crate::{GradeError, Host, STUDENT_USER};

/// Find the upload, grade it in the sandbox and build the results.
///
/// As root, student code runs as `STUDENT_USER`.
///
/// # Arguments
///
/// * `source` - Folder with the data files, at their paths relative to the notebook.
/// * `submission` - The unpacked upload.
/// * `workdir` - Folder for the sandbox's phase folders; replaced if it exists.
/// * `python` - Interpreter for the grading children.
///
/// # Errors
///
/// What `grade` and `build_results` fail with, and [`GradeError::Os`] if `workdir` cannot be
/// made.
pub fn run(
    bundle: &Bundle,
    source: &Path,
    submission: &Path,
    workdir: &Path,
    python: &OsStr,
    host: &dyn Host,
) -> Result<Results, GradeError> {
    let config = &bundle.config;
    let found = find_submission(submission, &config.notebook, Some(&config.name), host);
    let mut notices: Vec<String> = found.py.as_ref().err().cloned().into_iter().collect();

    if config.questions.iter().any(|question| question.manual) {
        notices.extend(answers_notice(found.answers.as_ref()));
    }

    let outcomes = match found.py {
        Err(_) => {
            let tests: Vec<String> = bundle.tests().cloned().collect();
            let mut outcomes = IndexMap::new();
            let reason = FailureReason::NotRun;
            fail(
                &mut outcomes,
                &tests,
                "not run: no notebook",
                reason,
                0.0,
                host,
            );
            outcomes
        }
        Ok(py) => {
            fresh_workdir(workdir)?;
            let root = rustix::process::geteuid().is_root();
            let options = GradeOptions {
                user: root.then_some(STUDENT_USER),
                files_root: Some(source),
                files: &config.files,
                python,
            };
            grade(bundle, &py, workdir, &options, host)?.outcomes
        }
    };

    build_results(bundle, &outcomes, &notices, host)
}

/// The warning about the written answers for manual questions, if any.
fn answers_notice(answers: Option<&Answers>) -> Option<String> {
    match answers {
        None => Some(
            "No PDF of your written answers found. Questions graded by hand need it: upload \
             the zip from the notebook's export button."
                .into(),
        ),
        Some(Answers::Pdf(_)) => None,
        Some(Answers::Markdown(markdown)) => {
            let name = markdown.file_name().unwrap_or_default().to_string_lossy();
            Some(format!(
                "No PDF of your written answers found; graders will read {} instead.",
                inline_code(&name)
            ))
        }
    }
}

/// Create an empty `workdir` that others can enter but not write to.
fn fresh_workdir(workdir: &Path) -> Result<(), GradeError> {
    if workdir.exists() {
        fs::remove_dir_all(workdir).map_err(|error| GradeError::os(error, workdir))?;
    }

    fs::create_dir_all(workdir).map_err(|error| GradeError::os(error, workdir))?;
    fs::set_permissions(workdir, fs::Permissions::from_mode(0o755))
        .map_err(|error| GradeError::os(error, workdir))?;

    Ok(())
}
