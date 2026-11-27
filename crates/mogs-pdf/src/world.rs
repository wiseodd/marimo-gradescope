//! The Typst world of one compile: the document, its inputs, the vendored packages and the
//! fonts.
//!
//! The document reads nothing from disk. The packages are in the binary, and the fonts are
//! Typst's embedded ones, plus the system's only when the inputs need them.

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

use typst::diag::{FileError, FileResult, PackageError, SourceDiagnostic};
use typst::foundations::{Bytes, Datetime, Dict, Duration, IntoValue, Str};
use typst::syntax::{FileId, RootedPath, Source, VirtualPath, VirtualRoot};
use typst::text::{Font, FontBook};
use typst::utils::LazyHash;
use typst::{Library, LibraryExt, World};
use typst_kit::fonts::{self, FontStore};
use typst_layout::PagedDocument;
use typst_pdf::PdfOptions;

use crate::formulas::needs_system_fonts;

/// The namespace of the vendored packages.
const NAMESPACE: &str = "preview";

/// One vendored file, as `(package, version, path in the package, content)`.
type Vendored = (&'static str, &'static str, &'static str, &'static [u8]);

macro_rules! vendored {
    ($name:literal, $version:literal, $path:literal) => {
        (
            $name,
            $version,
            $path,
            include_bytes!(concat!(
                "../typst/preview/",
                $name,
                "/",
                $version,
                "/",
                $path
            )),
        )
    };
}

/// Every file of the vendored packages cmarker and mitex, from `typst/preview/`.
pub(crate) const PACKAGES: &[Vendored] = &[
    vendored!("cmarker", "0.1.10", "LICENSE"),
    vendored!("cmarker", "0.1.10", "lib.typ"),
    vendored!("cmarker", "0.1.10", "plugin.wasm"),
    vendored!("cmarker", "0.1.10", "typst.toml"),
    vendored!("mitex", "0.2.7", "LICENSE"),
    vendored!("mitex", "0.2.7", "lib.typ"),
    vendored!("mitex", "0.2.7", "mitex.typ"),
    vendored!("mitex", "0.2.7", "mitex.wasm"),
    vendored!("mitex", "0.2.7", "specs/latex/standard.typ"),
    vendored!("mitex", "0.2.7", "specs/mod.typ"),
    vendored!("mitex", "0.2.7", "specs/prelude.typ"),
    vendored!("mitex", "0.2.7", "typst.toml"),
];

/// Compile the Typst `text`, named `name`, to PDF with `inputs` in `sys.inputs`.
///
/// System fonts are added only when an input holds a script the embedded fonts lack: a
/// system font with an embedded font's family name would replace it.
///
/// Returns the error messages, joined by `; `, when Typst fails.
pub(crate) fn compile(name: &str, text: &str, inputs: &[(&str, &str)]) -> Result<Vec<u8>, String> {
    let document = layout(name, text, inputs)?;

    typst_pdf::pdf(&document, &PdfOptions::default()).map_err(summary)
}

/// The pages of the Typst `text`, as `compile` lays them out.
pub(crate) fn layout(
    name: &str,
    text: &str,
    inputs: &[(&str, &str)],
) -> Result<PagedDocument, String> {
    let world = PdfWorld::new(name, text, inputs);

    typst::compile(&world).output.map_err(summary)
}

/// Whether a compile with `inputs` gets the system fonts too.
pub(crate) fn uses_system_fonts(inputs: &[(&str, &str)]) -> bool {
    inputs.iter().any(|(_, value)| needs_system_fonts(value))
}

/// Typst's embedded fonts, plus the system's when `system`; found once per process.
pub(crate) fn font_store(system: bool) -> &'static FontStore {
    static EMBEDDED: OnceLock<FontStore> = OnceLock::new();
    static WITH_SYSTEM: OnceLock<FontStore> = OnceLock::new();

    let load = move || {
        let mut store = FontStore::new();
        store.extend(fonts::embedded());

        if system {
            store.extend(fonts::system());
        }

        store
    };

    match system {
        true => WITH_SYSTEM.get_or_init(load),
        false => EMBEDDED.get_or_init(load),
    }
}

/// The messages of Typst's errors.
fn summary(errors: impl IntoIterator<Item = SourceDiagnostic>) -> String {
    let messages: Vec<String> = errors
        .into_iter()
        .map(|error| error.message.to_string())
        .collect();

    messages.join("; ")
}

struct PdfWorld {
    library: LazyHash<Library>,
    fonts: &'static FontStore,
    main: Source,
}

impl PdfWorld {
    fn new(name: &str, text: &str, inputs: &[(&str, &str)]) -> Self {
        let dict: Dict = inputs
            .iter()
            .map(|(key, value)| (Str::from(*key), (*value).into_value()))
            .collect();
        let path = VirtualPath::new(name).expect("the main file has a plain name");
        let id = FileId::new(RootedPath::new(VirtualRoot::Project, path));

        Self {
            library: LazyHash::new(Library::builder().with_inputs(dict).build()),
            fonts: font_store(uses_system_fonts(inputs)),
            main: Source::new(id, text.to_owned()),
        }
    }
}

impl World for PdfWorld {
    fn library(&self) -> &LazyHash<Library> {
        &self.library
    }

    fn book(&self) -> &LazyHash<FontBook> {
        self.fonts.book()
    }

    fn main(&self) -> FileId {
        self.main.id()
    }

    fn source(&self, id: FileId) -> FileResult<Source> {
        if id == self.main.id() {
            return Ok(self.main.clone());
        }

        // The vendored files never change, so their parsed sources serve every compile.
        static SOURCES: OnceLock<Mutex<HashMap<FileId, Source>>> = OnceLock::new();
        let mut sources = SOURCES
            .get_or_init(Default::default)
            .lock()
            .unwrap_or_else(|e| e.into_inner());

        if let Some(source) = sources.get(&id) {
            return Ok(source.clone());
        }

        let text = std::str::from_utf8(vendored_file(id)?).map_err(|_| FileError::InvalidUtf8)?;
        let source = Source::new(id, text.to_owned());
        sources.insert(id, source.clone());

        Ok(source)
    }

    fn file(&self, id: FileId) -> FileResult<Bytes> {
        if id == self.main.id() {
            return Ok(Bytes::from_string(self.main.text().to_owned()));
        }

        vendored_file(id).map(Bytes::new)
    }

    fn font(&self, index: usize) -> Option<Font> {
        self.fonts.font(index)
    }

    fn today(&self, _offset: Option<Duration>) -> Option<Datetime> {
        None
    }
}

/// The content of a vendored package file.
fn vendored_file(id: FileId) -> FileResult<&'static [u8]> {
    let not_found = || FileError::NotFound(id.vpath().get_with_slash().into());

    let VirtualRoot::Package(spec) = id.root() else {
        return Err(not_found());
    };

    let version = spec.version.to_string();
    let mut files = PACKAGES
        .iter()
        .filter(|(name, at, _, _)| {
            spec.namespace == NAMESPACE && spec.name == *name && *at == version
        })
        .peekable();

    if files.peek().is_none() {
        return Err(FileError::Package(PackageError::NotFound(spec.clone())));
    }

    files
        .find(|(_, _, path, _)| *path == id.vpath().get_without_slash())
        .map(|(_, _, _, content)| *content)
        .ok_or_else(not_found)
}

#[cfg(test)]
mod tests {
    use std::path::{Path, PathBuf};

    use super::*;

    fn files_under(folder: &Path) -> Vec<PathBuf> {
        let mut found = Vec::new();

        for entry in std::fs::read_dir(folder).unwrap() {
            let path = entry.unwrap().path();

            if path.is_dir() {
                found.extend(files_under(&path));
            } else {
                found.push(path);
            }
        }

        found
    }

    #[test]
    fn every_vendored_file_is_embedded() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("typst/preview");
        let mut on_disk: Vec<String> = files_under(&root)
            .iter()
            .map(|path| {
                let rel = path.strip_prefix(&root).unwrap();
                rel.components()
                    .map(|part| part.as_os_str().to_str().unwrap())
                    .collect::<Vec<_>>()
                    .join("/")
            })
            .collect();
        let mut embedded: Vec<String> = PACKAGES
            .iter()
            .map(|(name, version, path, _)| format!("{name}/{version}/{path}"))
            .collect();
        on_disk.sort();
        embedded.sort();

        assert_eq!(on_disk, embedded);
    }

    #[test]
    fn compiles_with_the_vendored_packages() {
        let text = "#import \"@preview/mitex:0.2.7\": mitex\n#mitex(sys.inputs.formula)\n";

        let pdf = compile("test.typ", text, &[("formula", "x^2")]).unwrap();

        assert!(pdf.starts_with(b"%PDF"));
    }

    #[test]
    fn errors_name_the_problem() {
        let error = compile("test.typ", "#import \"@preview/nothing:0.1.0\"\n", &[]).unwrap_err();

        assert!(error.contains("package not found"), "{error}");
        assert!(
            compile("test.typ", "#read(\"other.typ\")", &[])
                .unwrap_err()
                .contains("not found")
        );
    }

    #[test]
    fn latin_inputs_never_see_system_fonts() {
        // A system font of the same family would replace the embedded one.
        let latin = "Text with `code`, Ünïcödé, Ελληνικά and $\\alpha$ — “quotes”.";

        assert!(!uses_system_fonts(&[("doc", latin), ("display", "false")]));

        let embedded = font_store(false);
        let count = embedded.book().families().count();
        assert!(count > 0);
        assert!(
            (0..)
                .map_while(|at| embedded.source(at))
                .all(|source| { (source as &dyn std::any::Any).is::<Font>() })
        );
    }

    #[test]
    fn other_scripts_get_system_fonts_too() {
        assert!(uses_system_fonts(&[("doc", "中文答案")]));

        let all = font_store(true);
        let system = (0..)
            .map_while(|at| all.source(at))
            .filter(|source| (*source as &dyn std::any::Any).is::<fonts::FontPath>())
            .count();

        // Every CI runner has some system fonts.
        assert!(system > 0);
    }
}
