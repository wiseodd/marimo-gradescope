//! The Typst world of one compile: the document, its inputs, the Typst Universe packages and
//! the fonts.
//!
//! The document reads nothing from disk. The packages come from Typst's package folders, and
//! `fetch_packages` downloads the preamble's into Typst's package cache first. The fonts are
//! Typst's embedded ones, plus the system's only when the inputs need them.

use std::any::Any;
use std::collections::HashMap;
use std::env;
use std::io::{self, Read};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration as Timeout;

use typst::diag::{FileError, FileResult, PackageError, SourceDiagnostic};
use typst::foundations::{Bytes, Datetime, Dict, Duration, IntoValue, Str};
use typst::syntax::package::PackageSpec;
use typst::syntax::{FileId, RootedPath, Source, VirtualPath, VirtualRoot};
use typst::text::{Font, FontBook};
use typst::utils::LazyHash;
use typst::{Library, LibraryExt, World};
use typst_kit::downloader::Downloader;
use typst_kit::fonts::{self, FontStore};
use typst_kit::packages::{FsPackages, SystemPackages, UniversePackages};
use typst_layout::PagedDocument;
use typst_pdf::PdfOptions;
use ureq::tls::{RootCerts, TlsConfig};

use crate::formulas::needs_system_fonts;

/// How long one package download may take in all.
const DOWNLOAD_TIMEOUT: Timeout = Timeout::from_secs(60);

/// How long the connection of a package download may take.
const CONNECT_TIMEOUT: Timeout = Timeout::from_secs(15);

/// The packages and math helpers every document starts with.
const PREAMBLE: &str = include_str!("preamble.typ");

/// The Typst Universe packages the preamble imports, in its order.
pub(crate) fn imports() -> Vec<PackageSpec> {
    PREAMBLE
        .lines()
        .filter_map(|line| line.strip_prefix("#import \""))
        .filter_map(|rest| rest.split('"').next())
        .filter(|spec| spec.starts_with('@'))
        .map(|spec| {
            spec.parse()
                .expect("the preamble imports packages by full spec")
        })
        .collect()
}

/// Download the packages the preamble imports into Typst's package cache, unless there.
///
/// Returns one line that names the package and the cause when a download fails.
pub(crate) fn fetch_packages() -> Result<(), String> {
    fetch(&packages(), &imports())
}

fn fetch(packages: &SystemPackages, specs: &[PackageSpec]) -> Result<(), String> {
    for spec in specs {
        packages
            .obtain(spec)
            .map_err(|error| failure(spec, error))?;
    }

    Ok(())
}

/// One line that names `spec` and the cause of `error`, with its whitespace collapsed.
fn failure(spec: &PackageSpec, error: PackageError) -> String {
    let cause = match error {
        PackageError::NetworkFailed(Some(cause)) => cause.to_string(),
        other => other.to_string(),
    };
    let cause = cause.split_whitespace().collect::<Vec<_>>().join(" ");

    format!("downloading the Typst package {spec} failed ({cause})")
}

/// Typst's package folders, as the Typst CLI finds them.
///
/// `TYPST_PACKAGE_PATH` and `TYPST_PACKAGE_CACHE_PATH` replace the system's data and cache
/// folders. The environment is read on every call, so a change decides where the next fetch
/// looks and stores. Files a compile already read stay cached in memory for the process.
fn packages() -> SystemPackages {
    let folder = |variable: &str, system: fn() -> Option<FsPackages>| {
        env::var_os(variable).map(FsPackages::new).or_else(system)
    };

    SystemPackages::from_parts(
        folder("TYPST_PACKAGE_PATH", FsPackages::system_data),
        folder("TYPST_PACKAGE_CACHE_PATH", FsPackages::system_cache),
        UniversePackages::new(Http),
    )
}

/// An HTTP client over rustls with ring that trusts the OS certificate store, like the Typst
/// CLI, and goes through the proxy the environment names.
///
/// A new one per download, so it reads the proxy variables as they are then.
fn agent() -> ureq::Agent {
    let tls = TlsConfig::builder()
        .root_certs(RootCerts::PlatformVerifier)
        .unversioned_rustls_crypto_provider(Arc::new(rustls::crypto::ring::default_provider()))
        .build();

    ureq::Agent::config_builder()
        .user_agent(concat!("mogs/", env!("CARGO_PKG_VERSION")))
        .timeout_global(Some(DOWNLOAD_TIMEOUT))
        .timeout_connect(Some(CONNECT_TIMEOUT))
        .tls_config(tls)
        .build()
        .into()
}

/// Downloads with `agent`.
struct Http;

impl Downloader for Http {
    fn stream(&self, _key: &dyn Any, url: &str) -> io::Result<(Option<usize>, Box<dyn Read>)> {
        let response = agent().get(url).call().map_err(|error| match error {
            ureq::Error::StatusCode(404) => io::Error::new(io::ErrorKind::NotFound, "not found"),
            other => io::Error::other(other.to_string()),
        })?;
        let size = response
            .body()
            .content_length()
            .and_then(|length| usize::try_from(length).ok());

        Ok((size, Box::new(response.into_body().into_reader())))
    }
}

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

        // A package version never changes, so its parsed sources serve every compile.
        static SOURCES: OnceLock<Mutex<HashMap<FileId, Source>>> = OnceLock::new();
        let sources = SOURCES.get_or_init(Default::default);

        if let Some(source) = sources.lock().unwrap_or_else(|e| e.into_inner()).get(&id) {
            return Ok(source.clone());
        }

        let bytes = package_file(id)?;
        let text = std::str::from_utf8(&bytes).map_err(|_| FileError::InvalidUtf8)?;
        let source = Source::new(id, text.to_owned());

        sources
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(id, source.clone());

        Ok(source)
    }

    fn file(&self, id: FileId) -> FileResult<Bytes> {
        if id == self.main.id() {
            return Ok(Bytes::from_string(self.main.text().to_owned()));
        }

        package_file(id)
    }

    fn font(&self, index: usize) -> Option<Font> {
        self.fonts.font(index)
    }

    fn today(&self, _offset: Option<Duration>) -> Option<Datetime> {
        None
    }
}

/// The content of a package file, from Typst's package folders; read once per process.
fn package_file(id: FileId) -> FileResult<Bytes> {
    let VirtualRoot::Package(spec) = id.root() else {
        return Err(FileError::NotFound(id.vpath().get_with_slash().into()));
    };

    static FILES: OnceLock<Mutex<HashMap<FileId, Bytes>>> = OnceLock::new();
    let files = FILES.get_or_init(Default::default);

    if let Some(bytes) = files.lock().unwrap_or_else(|e| e.into_inner()).get(&id) {
        return Ok(bytes.clone());
    }

    let root = packages().obtain(spec).map_err(FileError::Package)?;
    let bytes = root.load(id.vpath())?;

    files
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .insert(id, bytes.clone());

    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::*;

    /// Typst's package folders under `folder`, with the registry at `url`.
    fn packages_in(folder: &Path, url: &str) -> SystemPackages {
        SystemPackages::from_parts(
            None,
            Some(FsPackages::new(folder.join("cache"))),
            UniversePackages::with_url(Http, url),
        )
    }

    #[test]
    fn imports_are_the_preamble_s_packages() {
        let names: Vec<String> = imports().iter().map(ToString::to_string).collect();

        assert_eq!(names, ["@preview/cmarker:0.1.10", "@preview/mitex:0.2.7"]);
    }

    #[test]
    fn a_failure_with_line_breaks_stays_on_one_line() {
        let spec: PackageSpec = "@preview/mitex:0.2.7".parse().unwrap();
        let error =
            PackageError::NetworkFailed(Some("tls:\n  invalid\tpeer  certificate\r\n".into()));

        assert_eq!(
            failure(&spec, error),
            "downloading the Typst package @preview/mitex:0.2.7 failed \
             (tls: invalid peer certificate)"
        );
    }

    #[test]
    fn downloads_trust_the_os_certificate_store() {
        let agent = agent();

        assert!(matches!(
            agent.config().tls_config().root_certs(),
            ureq::tls::RootCerts::PlatformVerifier
        ));
    }

    #[test]
    fn a_failed_download_names_the_package_and_the_cause() {
        let folder = tempfile::tempdir().unwrap();
        // Nothing listens on port 1.
        let packages = packages_in(folder.path(), "http://127.0.0.1:1");

        let error = fetch(&packages, &imports()).unwrap_err();

        let prefix = "downloading the Typst package @preview/cmarker:0.1.10 failed (";
        assert!(error.starts_with(prefix), "{error}");
        assert!(error.ends_with(')'), "{error}");
        assert!(!error.contains('\n'), "{error}");
        assert!(error.contains("refused"), "{error}");
    }

    #[test]
    fn fetched_packages_are_in_the_cache() {
        let folder = tempfile::tempdir().unwrap();
        let packages = packages_in(folder.path(), "https://packages.typst.org");

        fetch(&packages, &imports()).unwrap();

        for spec in imports() {
            let root = folder
                .path()
                .join(format!("cache/preview/{}/{}", spec.name, spec.version));
            assert!(root.join("typst.toml").is_file(), "{spec}");
        }
    }

    #[test]
    fn compiles_with_the_packages() {
        let text = "#import \"@preview/mitex:0.2.7\": mitex\n#mitex(sys.inputs.formula)\n";

        let pdf = compile("test.typ", text, &[("formula", "x^2")]).unwrap();

        assert!(pdf.starts_with(b"%PDF"));
    }

    #[test]
    fn errors_name_the_problem() {
        // Only the `preview` namespace is downloaded.
        let error = compile("test.typ", "#import \"@local/nothing:0.1.0\"\n", &[]).unwrap_err();

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
    fn a_world_gets_system_fonts_only_when_its_inputs_need_them() {
        let cjk = PdfWorld::new(
            "test.typ",
            "",
            &[("doc", "Answer: 中文答案"), ("display", "false")],
        );
        let latin = PdfWorld::new("test.typ", "", &[("doc", "Ünïcödé Ελληνικά — “x” 𝛼")]);

        assert!(std::ptr::eq(cjk.fonts, font_store(true)));
        assert!(std::ptr::eq(latin.fonts, font_store(false)));
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
