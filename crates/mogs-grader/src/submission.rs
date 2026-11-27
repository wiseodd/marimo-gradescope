//! Find the notebook and the written answers in an upload.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

use crate::PyText;
use crate::files::normalized;
use crate::results::inline_code;

/// Folders that are never searched.
pub const SKIPPED_DIRS: [&str; 2] = ["__MACOSX", "__pycache__"];
/// The folder of the public tests that the student copy ships next to the notebook.
pub const TESTS_FOLDER: &str = "tests";

/// The files found in an upload.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Submission {
    /// The notebook to grade, or `None` when there is none or it is ambiguous.
    pub py: Option<PathBuf>,
    /// The written answers: a PDF, else the export's Markdown fallback
    /// `<notebook stem>_answers.md`, else `None`. Only tells whether the student sent any;
    /// graders see every uploaded file.
    pub answers: Option<PathBuf>,
    /// Why `py` is `None`, ready to show the student.
    pub error: Option<String>,
}

/// Find the notebook and the written answers anywhere under `sub_dir`.
///
/// Skips `__MACOSX`, `__pycache__`, dot files and dot folders, and never follows or picks a
/// symlink. Public tests, the `.py` files in a `tests/` folder next to another `.py` file, are
/// never the notebook. Of the rest, a file named `notebook` wins; otherwise the only `.py` file
/// is taken. Any PDF counts as the written answers. Of Markdown files, only the export's
/// fallback `<name>_answers.md` (any case) does, so a stray `README.md` does not. `name`
/// defaults to the notebook's stem.
pub fn find_submission(
    sub_dir: &Path,
    notebook: &str,
    name: Option<&str>,
    text: &dyn PyText,
) -> Submission {
    let root = normalized(sub_dir);
    let mut files = Vec::new();
    regular_files(&root, &mut files);
    files.sort();

    let scripts: Vec<&PathBuf> = files.iter().filter(|path| suffix(path) == ".py").collect();
    // Not the public tests the student copy ships in `tests/`, next to the notebook.
    let folders: HashSet<&Path> = scripts.iter().filter_map(|path| path.parent()).collect();
    let others: Vec<&PathBuf> = scripts
        .iter()
        .copied()
        .filter(|path| {
            let parent = path.parent().unwrap_or(Path::new(""));
            let in_tests = parent.file_name().is_some_and(|name| name == TESTS_FOLDER);
            let beside = parent.parent().is_some_and(|up| folders.contains(up));
            !in_tests || !beside
        })
        .collect();
    let exact: Vec<&PathBuf> = others
        .iter()
        .copied()
        .filter(|path| path.file_name().is_some_and(|file| file == notebook))
        .collect();

    let stem = Path::new(notebook)
        .file_stem()
        .map(|stem| stem.to_string_lossy().into_owned())
        .unwrap_or_default();
    let fallback = text.casefold(&format!(
        "{}_answers.md",
        name.filter(|name| !name.is_empty()).unwrap_or(&stem)
    ));
    let pdf = files
        .iter()
        .find(|path| suffix(path).to_lowercase() == ".pdf");
    let markdown = files.iter().find(|path| {
        path.file_name()
            .and_then(|file| file.to_str())
            .is_some_and(|file| text.casefold(file) == fallback)
    });
    let answers = pdf.or(markdown).cloned();
    let wanted = inline_code(notebook);

    let error = match (exact.as_slice(), others.as_slice()) {
        ([py], _) | ([], [py]) => {
            return Submission {
                py: Some(py.to_path_buf()),
                answers,
                error: None,
            };
        }
        ([], []) => format!("No `.py` file found in your upload. Upload {wanted}."),
        ([], several) => format!(
            "No {wanted} in your upload, and several other `.py` files: {}. Upload {wanted}, \
             or a single `.py` file.",
            names(several, &root)
        ),
        (several, _) => format!(
            "Found {wanted} more than once: {}. Upload one.",
            names(several, &root)
        ),
    };

    Submission {
        py: None,
        answers,
        error: Some(error),
    }
}

/// The paths relative to `root`, as inline code, comma separated.
fn names(paths: &[&PathBuf], root: &Path) -> String {
    let names: Vec<String> = paths
        .iter()
        .map(|path| {
            let rel = path.strip_prefix(root).unwrap_or(path);
            inline_code(&rel.to_string_lossy())
        })
        .collect();

    names.join(", ")
}

/// `PurePath.suffix`: the last `.` part of the file name, or empty.
fn suffix(path: &Path) -> String {
    match path.extension() {
        Some(extension) => format!(".{}", extension.to_string_lossy()),
        None => String::new(),
    }
}

/// Regular files under `folder`, skipping junk folders, dot names and symlinks.
fn regular_files(folder: &Path, found: &mut Vec<PathBuf>) {
    let Ok(listing) = fs::read_dir(folder) else {
        return;
    };

    for entry in listing.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();

        if name.starts_with('.') {
            continue;
        }

        match entry.file_type() {
            Ok(kind) if kind.is_dir() && !SKIPPED_DIRS.contains(&name.as_ref()) => {
                regular_files(&entry.path(), found);
            }
            Ok(kind) if kind.is_file() => found.push(entry.path()),
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fake::FakeHost;

    fn upload(names: &[&str]) -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();

        for name in names {
            let path = dir.path().join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, "").unwrap();
        }

        dir
    }

    #[test]
    fn public_tests_beside_the_notebook_are_skipped() {
        let dir = upload(&["hw.py", "tests/q1.py", "answers.PDF", ".hidden.py"]);

        let found = find_submission(dir.path(), "other.py", None, &FakeHost::default());

        assert_eq!(found.py, Some(dir.path().join("hw.py")));
        assert_eq!(found.answers, Some(dir.path().join("answers.PDF")));
        assert_eq!(found.error, None);
    }

    #[test]
    fn several_notebooks_are_named() {
        let dir = upload(&["b/x.py", "a.py"]);

        let found = find_submission(dir.path(), "hw.py", Some("A"), &FakeHost::default());

        assert_eq!(found.py, None);
        assert_eq!(
            found.error.as_deref(),
            Some(
                "No `hw.py` in your upload, and several other `.py` files: `a.py`, `b/x.py`. \
                 Upload `hw.py`, or a single `.py` file."
            )
        );
    }
}
