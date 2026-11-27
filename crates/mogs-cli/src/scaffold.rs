//! `mogs init`: a new assignment folder with a notebook to start from.

use std::fmt;
use std::fs;
use std::io::{self, Write};
use std::path::{Component, Path, PathBuf};

use crate::{Call, Interpreter, OsError};

/// The notebook, with `$marimo` and `$name` for the Python literals of marimo's version and the
/// assignment name.
const NOTEBOOK: &str = include_str!("../templates/notebook.py.tmpl");

/// The notebook `init` wrote.
#[derive(Debug, PartialEq, Eq)]
pub struct Created {
    /// The notebook, `<folder>/<name>.py`, as Python shows it.
    pub notebook: PathBuf,
    /// The folder, quoted for a shell.
    pub quoted: String,
}

/// Why `init` failed.
#[derive(Debug)]
pub enum InitError<E> {
    /// A folder that does not fit, with the message v0.7 gave.
    Refused(Refusal),
    /// A file or folder could not be made.
    Os(OsError),
    /// A Python exception, such as the `ConfigError` for a name that does not fit.
    Python(E),
}

impl<E> From<Refusal> for InitError<E> {
    fn from(refusal: Refusal) -> Self {
        Self::Refused(refusal)
    }
}

impl<E> From<OsError> for InitError<E> {
    fn from(error: OsError) -> Self {
        Self::Os(error)
    }
}

/// A folder that `init` does not write into.
#[derive(Debug, PartialEq, Eq)]
pub enum Refusal {
    /// The path is a file.
    NotAFolder(PathBuf),
    /// The folder has files in it.
    NotEmpty(PathBuf),
}

impl fmt::Display for Refusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotAFolder(folder) => write!(f, "{} is not a folder.", folder.display()),
            Self::NotEmpty(folder) => write!(
                f,
                "{} is not empty. Pick a new folder for the assignment.",
                folder.display()
            ),
        }
    }
}

/// Create `folder` with one notebook, `<folder name>.py`, to start an assignment from.
///
/// The notebook holds the assignment cell, a guide for the instructor and two example
/// questions. It builds as it is. The name is the last part of the resolved `folder`, so `.`
/// takes the name of the current folder.
///
/// # Errors
///
/// [`InitError::Python`] with a `ConfigError` if the name does not work as an assignment name,
/// [`InitError::Refused`] if `folder` is a file or a folder with files in it, and
/// [`InitError::Os`] if the folder or the notebook cannot be written.
pub fn init<P: Interpreter>(folder: &Path, python: &P) -> Result<Created, InitError<P::Error>> {
    let folder = python.path(folder).map_err(InitError::Python)?;
    let real = resolved(&folder).map_err(|error| OsError::new(error, None, Call::File))?;
    let name = python
        .check_name(real.file_name().unwrap_or_default())
        .map_err(InitError::Python)?;

    if folder.exists() && !folder.is_dir() {
        return Err(Refusal::NotAFolder(folder).into());
    }

    if folder.is_dir() && has_entries(&folder)? {
        return Err(Refusal::NotEmpty(folder).into());
    }

    make_folder(&folder)?;

    let notebook = python
        .path(&folder.join(format!("{name}.py")))
        .map_err(InitError::Python)?;
    let name = python.literal(&name).map_err(InitError::Python)?;
    let marimo = python
        .marimo_version()
        .and_then(|version| python.literal(&version))
        .map_err(InitError::Python)?;
    write_text(&notebook, &render(&name, &marimo))?;

    let quoted = python.quote(&folder).map_err(InitError::Python)?;

    Ok(Created { notebook, quoted })
}

/// The notebook text for the Python literals `name` and `marimo`.
fn render(name: &str, marimo: &str) -> String {
    let (head, rest) = NOTEBOOK
        .split_once("$marimo")
        .expect("the template has `$marimo`");
    let (middle, tail) = rest.split_once("$name").expect("the template has `$name`");

    format!("{head}{marimo}{middle}{name}{tail}")
}

/// `path` resolved as `Path.resolve()` does: absolute, links followed and `..` taken back.
///
/// Like `resolve()`, it does not require `path` to exist: a part that does not exist stays as
/// it is written.
fn resolved(path: &Path) -> io::Result<PathBuf> {
    let mut resolved = PathBuf::new();

    for part in std::path::absolute(path)?.components() {
        match part {
            Component::Prefix(_) | Component::RootDir => resolved.push(part),
            Component::CurDir => {}
            Component::ParentDir => {
                resolved.pop();
            }
            Component::Normal(name) => {
                resolved.push(name);

                if let Ok(real) = fs::canonicalize(&resolved) {
                    resolved = real;
                }
            }
        }
    }

    Ok(resolved)
}

/// Whether the folder has any entry.
fn has_entries(folder: &Path) -> Result<bool, OsError> {
    let error = |error| OsError::new(error, Some(folder), Call::Folder);
    let mut entries = fs::read_dir(folder).map_err(error)?;

    entries
        .next()
        .transpose()
        .map(|entry| entry.is_some())
        .map_err(error)
}

/// Make `folder` and its missing parents, as `folder.mkdir(parents=True, exist_ok=True)` does.
///
/// Errors name the folder whose `mkdir` failed, as Python's do.
fn make_folder(folder: &Path) -> Result<(), OsError> {
    let error = |error| OsError::new(error, Some(folder), Call::Folder);

    match fs::create_dir(folder) {
        Ok(()) => Ok(()),
        Err(missing) if missing.kind() == io::ErrorKind::NotFound => {
            let parent = parent(folder);

            if parent == folder {
                return Err(error(missing));
            }

            make_folder(&parent)?;

            match fs::create_dir(folder) {
                Ok(()) => Ok(()),
                Err(missing) if missing.kind() == io::ErrorKind::NotFound => Err(error(missing)),
                Err(_) if folder.is_dir() => Ok(()),
                Err(other) => Err(error(other)),
            }
        }
        Err(_) if folder.is_dir() => Ok(()),
        Err(other) => Err(error(other)),
    }
}

/// The parent of `path` as pathlib gives it: `.` for a bare name, and the path itself at the
/// top.
fn parent(path: &Path) -> PathBuf {
    match path.parent() {
        Some(parent) if parent.as_os_str().is_empty() => PathBuf::from("."),
        Some(parent) => parent.to_path_buf(),
        None => path.to_path_buf(),
    }
}

/// Write `text` as `Path.write_text` does: UTF-8, with the platform's line endings.
fn write_text(path: &Path, text: &str) -> Result<(), OsError> {
    let text = if cfg!(windows) {
        text.replace('\n', "\r\n")
    } else {
        text.to_owned()
    };
    let mut file =
        fs::File::create(path).map_err(|error| OsError::new(error, Some(path), Call::File))?;

    file.write_all(text.as_bytes())
        .map_err(|error| OsError::new(error, None, Call::File))
}

#[cfg(test)]
mod tests {
    use std::ffi::OsStr;

    use super::*;

    /// Python as far as `init` needs it: names must not start with `-`, as a `ConfigError`.
    struct FakePython;

    impl Interpreter for FakePython {
        type Error = String;

        fn check_name(&self, name: &OsStr) -> Result<String, String> {
            let name = name.to_str().ok_or("not UTF-8")?;

            if name.starts_with('-') {
                return Err(format!("bad name {name}"));
            }

            Ok(name.to_owned())
        }

        fn literal(&self, text: &str) -> Result<String, String> {
            Ok(format!("<{text}>"))
        }

        fn quote(&self, path: &Path) -> Result<String, String> {
            Ok(format!("'{}'", path.display()))
        }

        fn path(&self, path: &Path) -> Result<PathBuf, String> {
            Ok(path.components().collect())
        }

        fn marimo_version(&self) -> Result<String, String> {
            Ok("0.99".into())
        }
    }

    #[test]
    fn template_has_only_the_two_placeholders() {
        assert_eq!(NOTEBOOK.matches('$').count(), 2);
        assert_eq!(NOTEBOOK.matches("$marimo\n").count(), 1);
        assert_eq!(NOTEBOOK.matches("$name)").count(), 1);
        assert!(NOTEBOOK.ends_with("    app.run()\n"));
    }

    #[test]
    fn render_fills_in_the_literals_once() {
        let text = render("\"$marimo\"", "\"0.1\"");

        assert!(text.contains("__generated_with = \"0.1\"\n"));
        assert!(text.contains("mogs.assignment(name=\"$marimo\")\n"));
        assert!(!text.contains("$name"));
    }

    #[test]
    fn writes_one_notebook_named_after_the_folder() {
        let root = tempfile::tempdir().unwrap();
        let folder = root.path().join("hw");

        let created = init(&folder, &FakePython).unwrap();

        assert_eq!(
            created,
            Created {
                notebook: folder.join("hw.py"),
                quoted: format!("'{}'", folder.display()),
            }
        );
        let text = fs::read_to_string(&created.notebook).unwrap();
        assert!(text.contains("__generated_with = <0.99>\n"));
        assert!(text.contains("mogs.assignment(name=<hw>)\n"));
    }

    #[test]
    fn names_the_notebook_after_the_resolved_folder() {
        let root = tempfile::tempdir().unwrap();
        fs::create_dir(root.path().join("hw")).unwrap();
        let folder = root.path().join("hw").join("new").join("..");

        let created = init(&folder, &FakePython).unwrap();

        assert_eq!(created.notebook, folder.join("hw.py"));
        assert!(root.path().join("hw").join("hw.py").is_file());
    }

    #[test]
    fn makes_missing_parents() {
        let root = tempfile::tempdir().unwrap();
        let folder = root.path().join("a").join("b");

        init(&folder, &FakePython).unwrap();

        assert!(folder.join("b.py").is_file());
    }

    #[test]
    fn writes_into_an_empty_folder() {
        let root = tempfile::tempdir().unwrap();

        assert!(init(root.path(), &FakePython).is_ok());
    }

    #[test]
    fn refuses_a_folder_with_files() {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("notes.txt"), "mine").unwrap();

        let error = init(root.path(), &FakePython).unwrap_err();

        let InitError::Refused(refusal) = error else {
            panic!("{error:?}");
        };
        assert_eq!(
            refusal.to_string(),
            format!(
                "{} is not empty. Pick a new folder for the assignment.",
                root.path().display()
            )
        );
        assert_eq!(
            fs::read_to_string(root.path().join("notes.txt")).unwrap(),
            "mine"
        );
    }

    #[test]
    fn refuses_a_file() {
        let root = tempfile::tempdir().unwrap();
        let file = root.path().join("hw");
        fs::write(&file, "").unwrap();

        let error = init(&file, &FakePython).unwrap_err();

        let InitError::Refused(refusal) = error else {
            panic!("{error:?}");
        };
        assert_eq!(
            refusal.to_string(),
            format!("{} is not a folder.", file.display())
        );
    }

    #[test]
    fn checks_the_name_before_the_folder() {
        let root = tempfile::tempdir().unwrap();
        let folder = root.path().join("-hw");
        fs::write(&folder, "").unwrap();

        let error = init(&folder, &FakePython).unwrap_err();

        assert!(matches!(error, InitError::Python(message) if message == "bad name -hw"));
    }

    #[test]
    fn names_the_folder_it_cannot_make() {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("f"), "").unwrap();
        let folder = root.path().join("f").join("sub");

        let error = init(&folder, &FakePython).unwrap_err();

        let InitError::Os(error) = error else {
            panic!("{error:?}");
        };
        assert_eq!(error.call, Call::Folder);
        let expected = if cfg!(windows) {
            root.path().join("f")
        } else {
            folder
        };
        assert_eq!(error.path, Some(expected));
    }

    #[test]
    fn parent_follows_pathlib() {
        assert_eq!(parent(Path::new("hw")), Path::new("."));
        assert_eq!(parent(Path::new("a/hw")), Path::new("a"));
        assert_eq!(parent(Path::new("/")), Path::new("/"));
    }
}
