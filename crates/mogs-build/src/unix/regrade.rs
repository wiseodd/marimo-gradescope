//! Grade one submission locally, as Gradescope would, with every test's outcome.
//!
//! For regrade requests and for finding out why a submission lost points. Unlike Gradescope's
//! results, the report names failing hidden tests and shows their messages.

use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

use mogs_grader::GradeError;
use mogs_grader::submission::find_submission;
use mogs_schema::Bundle;

use crate::unix::environment::grading_python;
use crate::unix::{Class, Graded, Interpreter, grade_locally, text, total};

/// How one submission did.
#[derive(Clone, Debug, PartialEq)]
pub struct Regraded {
    /// The notebook that was graded, as Python shows it. For a zip, the path of the notebook
    /// in the zip, joined onto the zip's path.
    pub submission: PathBuf,
    /// The grading bundle it was graded with.
    pub bundle: Bundle,
    /// The outcomes, the results and why the notebook did not load, if it did not. A hidden
    /// helper or test cell that fails is a test failure and leaves the load error `None`.
    pub graded: Graded,
    /// Score and maximum over the tested questions.
    pub score: (f64, f64),
}

impl Regraded {
    /// `<submission> failed to load: <error>`, when the notebook did not load.
    pub fn load_failure(&self) -> Option<OsString> {
        let error = self.graded.load_error.as_ref()?;

        Some(text(&[
            self.submission.as_os_str(),
            " failed to load: ".as_ref(),
            error.as_ref(),
        ]))
    }
}

/// Grade `submission` against the source notebook in `folder`.
///
/// The grader runs in a fresh environment with the pinned requirements, as in `build`. The
/// submission is a student notebook, a folder with one (such as a downloaded upload), or a zip
/// with one (such as the one the export button makes). A zip is unpacked into a temporary
/// folder and searched like a folder, as Gradescope does with uploads.
///
/// # Errors
///
/// `BuildError` if the source notebook is missing or invalid, or the submission is missing,
/// of another type, an unreadable or unsafe zip, or holds no single notebook; `ConfigError`,
/// `SourceError` and `RequirementsError` from Python.
pub fn grade<I: Interpreter>(
    i: &I,
    folder: &Path,
    submission: &Path,
) -> Result<Regraded, I::Error> {
    let assignment = i.read(folder)?;
    let stage = tempfile::Builder::new()
        .prefix("mogs-grade-")
        .tempdir()
        .map_err(|error| i.grade_error(GradeError::from(error)))?;
    let stage = stage.path();

    let notebook = i.notebook(&assignment)?;
    let (path, submission) = find_notebook(i, submission, &notebook, &stage.join("upload"))?;
    let inputs = i.grade_inputs(&assignment)?;
    let config = &inputs.bundle.config;
    let python = grading_python(i, &inputs.requirements, &stage.join("env"), &config.python)?;
    let graded = grade_locally(
        i,
        &inputs.bundle,
        &path,
        &stage.join("grade"),
        folder,
        &python,
    )?;
    let score = total(i, &graded.results)?;

    Ok(Regraded {
        submission: i.path(&submission)?,
        bundle: inputs.bundle,
        graded,
        score,
    })
}

/// The notebook to grade, and the path to show for it.
///
/// A `.py` file is graded as it is. A folder is searched with `find_submission`. A `.zip` is
/// unpacked into `upload` first, then searched the same way. A symlink to a file is followed,
/// since the grader takes only regular files.
fn find_notebook<I: Interpreter>(
    i: &I,
    submission: &Path,
    name: &str,
    upload: &Path,
) -> Result<(PathBuf, PathBuf), I::Error> {
    if submission.is_dir() {
        let notebook = search(i, submission, submission, name)?;
        return Ok((notebook.clone(), notebook));
    }

    if !submission.is_file() {
        let message = text(&[submission.as_ref(), " is not a file or folder.".as_ref()]);
        return Err(i.raise(Class::Build, message));
    }

    let suffix = submission
        .extension()
        .map(|e| e.to_string_lossy().to_ascii_lowercase());

    match suffix.as_deref() {
        Some("py") => {
            let notebook = fs::canonicalize(submission)
                .map_err(|error| i.grade_error(GradeError::os(error, submission)))?;
            Ok((notebook.clone(), notebook))
        }
        Some("zip") => {
            i.unpack(submission, upload)?;
            let notebook = search(i, upload, submission, name)?;
            let inside = notebook.strip_prefix(upload).expect("found in the upload");
            Ok((notebook.clone(), submission.join(inside)))
        }
        _ => {
            let message = text(&[
                submission.as_ref(),
                " is not a notebook. Pass a `.py` notebook, a `.zip` of the submission, or a \
                 folder with one."
                    .as_ref(),
            ]);
            Err(i.raise(Class::Build, message))
        }
    }
}

/// The one notebook in `folder`; `submission` names the input in the error.
fn search<I: Interpreter>(
    i: &I,
    folder: &Path,
    submission: &Path,
    name: &str,
) -> Result<PathBuf, I::Error> {
    find_submission(folder, name, None, i.host())
        .py
        .map_err(|error| {
            let message = text(&[submission.as_ref(), ": ".as_ref(), error.as_ref()]);
            i.raise(Class::Build, message)
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::unix::fake::Fake;

    #[test]
    fn folder_is_searched() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(tmp.path().join("hw.py"), "").unwrap();

        let (path, shown) =
            find_notebook(&Fake::default(), tmp.path(), "hw.py", Path::new("/unused")).unwrap();

        assert_eq!(path, tmp.path().join("hw.py"));
        assert_eq!(shown, path);
    }

    #[test]
    fn notebook_is_resolved() {
        let tmp = tempfile::tempdir().unwrap();
        let notebook = tmp.path().join("hw.PY");
        fs::write(&notebook, "").unwrap();
        let link = tmp.path().join("link.py");
        std::os::unix::fs::symlink(&notebook, &link).unwrap();

        let (path, shown) =
            find_notebook(&Fake::default(), &link, "hw.py", Path::new("/unused")).unwrap();

        assert_eq!(path, notebook.canonicalize().unwrap());
        assert_eq!(shown, path);
    }

    #[test]
    fn missing_and_other_submissions_are_refused() {
        let tmp = tempfile::tempdir().unwrap();
        let other = tmp.path().join("hw.txt");
        fs::write(&other, "").unwrap();
        let fake = Fake::default();
        let upload = Path::new("/unused");

        let missing = find_notebook(&fake, &tmp.path().join("nope"), "hw.py", upload);
        let refused = find_notebook(&fake, &other, "hw.py", upload);

        assert_eq!(
            missing.unwrap_err(),
            format!(
                "Build: {}/nope is not a file or folder.",
                tmp.path().display()
            )
        );
        assert_eq!(
            refused.unwrap_err(),
            format!(
                "Build: {} is not a notebook. Pass a `.py` notebook, a `.zip` of the \
                 submission, or a folder with one.",
                other.display()
            )
        );
    }
}
