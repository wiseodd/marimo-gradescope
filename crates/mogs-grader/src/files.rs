//! Data files shipped with a notebook: path checks and folder expansion.
//!
//! Shared by the build and the sandbox.

use std::ffi::OsString;
use std::fs;
use std::path::{Component, Path, PathBuf};

use indexmap::IndexMap;

use crate::PyText;

/// The name the sandbox gives the submission in each phase folder.
pub const SUBMISSION: &str = "submission.py";
/// Folders and files that tools leave behind; never shipped.
pub const JUNK: [&str; 5] = [
    "__pycache__",
    "__marimo__",
    "__MACOSX",
    ".ipynb_checkpoints",
    ".DS_Store",
];

/// An entry of `files` that names no data file to ship.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DataFileError {
    /// The path is absolute, has a `..` part or is empty.
    NotRelative(String),
    /// The path starts with `submission.py`.
    Submission(String),
    /// Nothing is at the path under `root`.
    Missing {
        /// The entry of `files`.
        name: String,
        /// The folder the entries are relative to.
        root: PathBuf,
    },
}

impl DataFileError {
    /// The message, which names the entry as Python's `repr` does, and the folder as its
    /// `str`, even when it is not UTF-8.
    pub fn message(&self, text: &dyn PyText) -> OsString {
        match self {
            DataFileError::NotRelative(name) => format!(
                "a data file must be a relative path inside its folder: {}",
                text.repr(name)
            )
            .into(),
            DataFileError::Submission(name) => format!(
                "a data file must not be named {SUBMISSION}: {}",
                text.repr(name)
            )
            .into(),
            DataFileError::Missing { name, root } => {
                let lead = format!("data file {} not found in ", text.repr(name));
                [lead.as_ref(), root.as_os_str()].into_iter().collect()
            }
        }
    }
}

/// The data files to ship, by POSIX path relative to `root`, with folders expanded.
///
/// Files in `JUNK` folders, and `JUNK` files, are left out of a folder.
///
/// Returns the source path by relative destination, in the order of `files`, folders sorted.
///
/// # Errors
///
/// [`DataFileError`] if a path is absolute, has a `..` part, is empty, starts with
/// `submission.py`, or does not exist.
pub fn data_files(
    root: &Path,
    files: &[String],
) -> Result<IndexMap<String, PathBuf>, DataFileError> {
    let root = normalized(root);
    let mut out = IndexMap::new();

    for name in files {
        let parts: Vec<&str> = name
            .split('/')
            .filter(|part| !part.is_empty() && *part != ".")
            .collect();

        if name.starts_with('/') || parts.contains(&"..") || parts.is_empty() {
            return Err(DataFileError::NotRelative(name.clone()));
        }

        if parts[0] == SUBMISSION {
            return Err(DataFileError::Submission(name.clone()));
        }

        let rel = parts.join("/");
        let source = root.join(&rel);

        if is_file(&source) {
            out.insert(rel, source);
        } else if source.is_dir() {
            let mut found = Vec::new();
            entries(&source, &mut found);
            found.sort();

            for path in found {
                let inner = posix(path.strip_prefix(&root).unwrap_or(&path));
                let junk = inner.split('/').any(|part| JUNK.contains(&part));

                if is_file(&path) && !junk {
                    out.insert(inner, path);
                }
            }
        } else {
            return Err(DataFileError::Missing {
                name: name.clone(),
                root: root.to_path_buf(),
            });
        }
    }

    Ok(out)
}

/// `path` as Python's `pathlib` writes it: without `.` parts, or `.` when nothing is left.
pub fn normalized(path: &Path) -> PathBuf {
    let parts: PathBuf = path
        .components()
        .filter(|part| *part != Component::CurDir)
        .collect();

    match parts.as_os_str().is_empty() {
        true => PathBuf::from("."),
        false => parts,
    }
}

/// Whether `path` is a regular file, following symlinks.
fn is_file(path: &Path) -> bool {
    fs::metadata(path).is_ok_and(|meta| meta.is_file())
}

/// Every path under `folder`, as `Path.rglob("*")` lists it: symlinked folders are listed but
/// not entered.
fn entries(folder: &Path, found: &mut Vec<PathBuf>) {
    let Ok(listing) = fs::read_dir(folder) else {
        return;
    };

    for entry in listing.flatten() {
        let path = entry.path();
        let folder = entry.file_type().is_ok_and(|kind| kind.is_dir());
        found.push(path.clone());

        if folder {
            entries(&path, found);
        }
    }
}

/// A relative path with `/` between its parts.
fn posix(path: &Path) -> String {
    let parts: Vec<String> = path
        .components()
        .map(|part| part.as_os_str().to_string_lossy().into_owned())
        .collect();

    parts.join("/")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fake::FakeHost;

    fn names(files: &[&str]) -> Vec<String> {
        files.iter().map(|name| name.to_string()).collect()
    }

    fn write(root: &Path, name: &str) {
        let path = root.join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, "42").unwrap();
    }

    #[test]
    fn folders_expand_sorted_without_junk() {
        let dir = tempfile::tempdir().unwrap();
        for name in [
            "data/b.txt",
            "data/a/c.txt",
            "data/__pycache__/m.pyc",
            "data/.DS_Store",
        ] {
            write(dir.path(), name);
        }
        write(dir.path(), "x.csv");

        let found = data_files(dir.path(), &names(&["./x.csv", "data/"])).unwrap();

        let keys: Vec<&str> = found.keys().map(String::as_str).collect();
        assert_eq!(keys, ["x.csv", "data/a/c.txt", "data/b.txt"]);
        assert_eq!(found["x.csv"], dir.path().join("x.csv"));
    }

    #[test]
    fn bad_paths_are_named() {
        let dir = tempfile::tempdir().unwrap();
        let host = FakeHost::default();
        let cases = [
            (
                "/etc/passwd",
                "a data file must be a relative path inside its folder: '/etc/passwd'",
            ),
            (
                "a/../../x",
                "a data file must be a relative path inside its folder: 'a/../../x'",
            ),
            (
                ".",
                "a data file must be a relative path inside its folder: '.'",
            ),
            (
                "submission.py/x",
                "a data file must not be named submission.py: 'submission.py/x'",
            ),
        ];

        for (name, message) in cases {
            let error = data_files(dir.path(), &names(&[name])).unwrap_err();
            assert_eq!(error.message(&host), message);
        }

        let error = data_files(dir.path(), &names(&["nope.csv"])).unwrap_err();
        let mut expected = OsString::from("data file 'nope.csv' not found in ");
        expected.push(dir.path());
        assert_eq!(error.message(&host), expected);
    }
}
