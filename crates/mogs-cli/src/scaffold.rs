//! `mogs init`: a new assignment folder with a notebook to start from.

use std::ffi::OsStr;
use std::path::{Path, PathBuf};

use crate::Interpreter;

/// The notebook without its header, with `$marimo` and `$name` for the Python literals of
/// marimo's version and the assignment name.
const NOTEBOOK: &str = include_str!("../templates/notebook.py.tmpl");

/// The requirements file `init` writes next to the notebook.
const REQUIREMENTS: &str = "# Packages for this assignment, one per line, as pip reads them.\n";

/// The notebook `init` wrote.
#[derive(Debug, PartialEq, Eq)]
pub struct Created {
    /// The notebook, `<folder>/<name>.py`, as Python shows it.
    pub notebook: PathBuf,
    /// The folder, as Python shows it.
    pub folder: PathBuf,
}

/// Why `init` failed.
#[derive(Debug)]
pub enum InitError<E> {
    /// A folder that does not fit.
    Refused(Refusal),
    /// A Python exception: the `ConfigError` for a name that does not fit, the `OSError` of a
    /// file operation, or any other.
    Python(E),
}

/// A folder that `init` does not write into, as Python shows it.
#[derive(Debug, PartialEq, Eq)]
pub enum Refusal {
    /// The path is a file.
    NotAFolder(PathBuf),
    /// The folder has files in it.
    NotEmpty(PathBuf),
}

impl Refusal {
    /// The folder.
    pub fn folder(&self) -> &Path {
        match self {
            Self::NotAFolder(folder) | Self::NotEmpty(folder) => folder,
        }
    }

    /// The message after the folder.
    pub fn reason(&self) -> &'static str {
        match self {
            Self::NotAFolder(_) => " is not a folder.",
            Self::NotEmpty(_) => " is not empty. Pick a new folder for the assignment.",
        }
    }
}

/// Create `folder` with one notebook, `<folder name>.py`, to start an assignment from, and an
/// empty `requirements.txt`.
///
/// The notebook's header comes from the requirements file. The notebook holds the assignment cell, a guide for the instructor and two example
/// questions. It builds as it is. The name is the last part of the resolved `folder`, so `.`
/// takes the name of the current folder.
///
/// # Errors
///
/// [`InitError::Python`] with a `ConfigError` if the name does not work as an assignment name,
/// [`InitError::Refused`] if `folder` is a file or a folder with files in it, and
/// [`InitError::Python`] with an `OSError` if the folder or a file cannot be written.
pub fn init<P: Interpreter>(folder: &Path, python: &P) -> Result<Created, InitError<P::Error>> {
    let folder = python.path(folder).map_err(InitError::Python)?;
    let name = python.check_name(&folder).map_err(InitError::Python)?;

    let exists = |path: &Path| python.exists(path).map_err(InitError::Python);
    let is_dir = |path: &Path| python.is_dir(path).map_err(InitError::Python);

    if exists(&folder)? && !is_dir(&folder)? {
        return Err(InitError::Refused(Refusal::NotAFolder(folder)));
    }

    if is_dir(&folder)? && python.has_entries(&folder).map_err(InitError::Python)? {
        return Err(InitError::Refused(Refusal::NotEmpty(folder)));
    }

    python.make_folder(&folder).map_err(InitError::Python)?;

    let mut file = name.clone();
    file.push(".py");
    let notebook = python.path(&folder.join(file)).map_err(InitError::Python)?;
    let name = python.literal(&name).map_err(InitError::Python)?;
    let marimo = python
        .marimo_version()
        .and_then(|version| python.literal(OsStr::new(&version)))
        .map_err(InitError::Python)?;
    let requirements = folder.join("requirements.txt");
    python
        .write_text(&requirements, REQUIREMENTS)
        .and_then(|()| python.write_text(&notebook, &render(&name, &marimo)))
        .and_then(|()| python.write_header(&notebook))
        .map_err(InitError::Python)?;

    Ok(Created { notebook, folder })
}

/// The notebook text for the Python literals `name` and `marimo`.
fn render(name: &str, marimo: &str) -> String {
    let (head, rest) = NOTEBOOK
        .split_once("$marimo")
        .expect("the template has `$marimo`");
    let (middle, tail) = rest.split_once("$name").expect("the template has `$name`");

    format!("{head}{marimo}{middle}{name}{tail}")
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;
    use std::fs;

    use super::*;

    /// Python as far as `init` needs it, on the real file system. Names must not start with
    /// `-`, as a `ConfigError`, and a folder named `locked` cannot be looked at, as on EACCES.
    struct FakePython;

    const LOCKED: &str = "[Errno 13] Permission denied: 'locked'";

    fn look(path: &Path) -> Result<(), String> {
        if path.ends_with("locked") {
            return Err(LOCKED.into());
        }

        Ok(())
    }

    impl Interpreter for FakePython {
        type Error = String;

        fn check_name(&self, folder: &Path) -> Result<OsString, String> {
            let name = folder.file_name().unwrap();

            if name.as_encoded_bytes().starts_with(b"-") {
                return Err(format!("bad name {}", name.display()));
            }

            Ok(name.to_owned())
        }

        fn literal(&self, text: &OsStr) -> Result<String, String> {
            Ok(format!("<{}>", text.display()))
        }

        fn path(&self, path: &Path) -> Result<PathBuf, String> {
            Ok(path.components().collect())
        }

        fn marimo_version(&self) -> Result<String, String> {
            Ok("0.99".into())
        }

        fn has_entries(&self, folder: &Path) -> Result<bool, String> {
            Ok(fs::read_dir(folder).unwrap().next().is_some())
        }

        fn make_folder(&self, folder: &Path) -> Result<(), String> {
            fs::create_dir_all(folder).map_err(|error| error.to_string())
        }

        fn write_text(&self, path: &Path, text: &str) -> Result<(), String> {
            fs::write(path, text).map_err(|error| error.to_string())
        }

        fn write_header(&self, notebook: &Path) -> Result<(), String> {
            let requirements = fs::read_to_string(notebook.with_file_name("requirements.txt"))
                .map_err(|error| error.to_string())?;
            let text = fs::read_to_string(notebook).map_err(|error| error.to_string())?;

            fs::write(notebook, format!("# header from {requirements}{text}"))
                .map_err(|error| error.to_string())
        }

        fn exists(&self, path: &Path) -> Result<bool, String> {
            look(path).map(|()| path.exists())
        }

        fn is_dir(&self, path: &Path) -> Result<bool, String> {
            look(path).map(|()| path.is_dir())
        }
    }

    #[test]
    fn a_folder_it_cannot_look_at_fails_before_anything_is_made() {
        let root = tempfile::tempdir().unwrap();
        let folder = root.path().join("locked");

        let error = init(&folder, &FakePython).unwrap_err();

        assert!(
            matches!(&error, InitError::Python(message) if message == LOCKED),
            "{error:?}"
        );
        assert!(!folder.exists());
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
                folder: folder.clone(),
            }
        );
        let text = fs::read_to_string(&created.notebook).unwrap();
        assert!(text.contains("__generated_with = <0.99>\n"));
        assert!(text.contains("mogs.assignment(name=<hw>)\n"));
    }

    #[test]
    fn writes_requirements_and_the_header_from_them() {
        let root = tempfile::tempdir().unwrap();
        let folder = root.path().join("hw");

        let created = init(&folder, &FakePython).unwrap();

        assert_eq!(
            fs::read_to_string(folder.join("requirements.txt")).unwrap(),
            "# Packages for this assignment, one per line, as pip reads them.\n"
        );
        let text = fs::read_to_string(&created.notebook).unwrap();
        assert!(text.starts_with(
            "# header from # Packages for this assignment, one per line, as pip reads them.\n\
             import marimo\n"
        ));
    }

    // Linux file names may hold any bytes; APFS on macOS refuses them (EILSEQ).
    #[cfg(target_os = "linux")]
    #[test]
    fn keeps_a_name_that_is_not_utf_8() {
        use std::os::unix::ffi::OsStrExt;

        let root = tempfile::tempdir().unwrap();
        let folder = root.path().join(OsStr::from_bytes(b"h\xffw"));

        let created = init(&folder, &FakePython).unwrap();

        assert_eq!(
            created.notebook,
            folder.join(OsStr::from_bytes(b"h\xffw.py"))
        );
        assert!(created.notebook.is_file());
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

        assert!(
            matches!(&error, InitError::Refused(Refusal::NotEmpty(folder)) if folder == root.path()),
            "{error:?}"
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

        assert!(
            matches!(&error, InitError::Refused(Refusal::NotAFolder(folder)) if *folder == file),
            "{error:?}"
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
    fn refusals_give_the_messages_of_v0_7() {
        let not_a_folder = Refusal::NotAFolder("hw".into());
        let not_empty = Refusal::NotEmpty("hw".into());

        assert_eq!(not_a_folder.folder(), Path::new("hw"));
        assert_eq!(not_a_folder.reason(), " is not a folder.");
        assert_eq!(
            not_empty.reason(),
            " is not empty. Pick a new folder for the assignment."
        );
    }
}
