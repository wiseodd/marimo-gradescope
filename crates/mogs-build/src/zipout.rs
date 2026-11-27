//! Reproducible zips, byte for byte as `zipfile` writes them for v0.7.
//!
//! Entries are sorted, dated 1980-01-01, deflated, and marked as made on Unix with mode 0755
//! for the scripts and 0644 for the rest. Each local header carries the sizes and the CRC, with
//! no data descriptor and no extra fields, as `zipfile` writes to a seekable file.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use mogs_grader::GradeError;

use crate::{Class, Interpreter};

/// The entries that are executable.
pub const SCRIPTS: [&str; 2] = ["setup.sh", "run_autograder"];

/// `zipfile`'s version 2.0, needed to extract deflate, and to make the zip.
const VERSION: u16 = 20;
/// Made on Unix, so tools read the mode bits.
const UNIX: u8 = 3;
const DEFLATED: u16 = 8;
/// Bit 11: the name is UTF-8.
const UTF8_NAME: u16 = 0x800;
/// 1980-01-01 00:00:00 in MS-DOS format.
const DOS_TIME: u16 = 0;
const DOS_DATE: u16 = 1 << 5 | 1;
const S_IFREG: u32 = 0o100_000;
/// Past this size, `zipfile` writes zip64 records, which this writer does not.
const ZIP64_LIMIT: usize = (1 << 31) - 1;
/// Past this many entries, `zipfile` writes zip64 records.
const ZIP_FILECOUNT_LIMIT: usize = 0xFFFF;

/// Write `entries` to the zip `path`, by name in sorted order.
///
/// # Errors
///
/// `OSError` if the file cannot be written, and `BuildError` for a zip that needs zip64.
pub fn write_zip<I: Interpreter>(
    i: &I,
    path: &Path,
    entries: &BTreeMap<String, Vec<u8>>,
) -> Result<(), I::Error> {
    let mut out = Vec::new();
    let mut central = Vec::new();

    for (name, data) in entries {
        let compressed = i.deflate(data)?;
        let offset = out.len();

        if [data.len(), compressed.len(), offset]
            .iter()
            .any(|&n| n > ZIP64_LIMIT)
        {
            return Err(too_large(i, path));
        }

        let flags = if name.is_ascii() { 0 } else { UTF8_NAME };
        let mode = if SCRIPTS.contains(&name.as_str()) {
            0o755
        } else {
            0o644
        };
        let sizes = Sizes {
            crc: crc32fast::hash(data),
            compressed: as_u32(compressed.len()),
            size: as_u32(data.len()),
            name: u16::try_from(name.len()).map_err(|_| too_large(i, path))?,
        };

        out.extend(b"PK\x03\x04");
        push16(&mut out, VERSION);
        sizes.push(&mut out, flags);
        out.extend(name.as_bytes());
        out.extend(&compressed);

        central.extend(b"PK\x01\x02");
        central.extend([VERSION as u8, UNIX]);
        push16(&mut central, VERSION);
        sizes.push(&mut central, flags);
        // Comment length, disk number and internal attributes.
        central.extend([0; 6]);
        push32(&mut central, (S_IFREG | mode) << 16);
        push32(&mut central, as_u32(offset));
        central.extend(name.as_bytes());
    }

    if entries.len() > ZIP_FILECOUNT_LIMIT || out.len() + central.len() > ZIP64_LIMIT {
        return Err(too_large(i, path));
    }

    let count = entries.len() as u16;
    let start = as_u32(out.len());
    let size = as_u32(central.len());
    out.extend(central);
    out.extend(b"PK\x05\x06");
    // This disk and the disk where the central directory starts.
    out.extend([0; 4]);
    push16(&mut out, count);
    push16(&mut out, count);
    push32(&mut out, size);
    push32(&mut out, start);
    // Comment length.
    push16(&mut out, 0);

    fs::write(path, out).map_err(|error| i.grade_error(GradeError::os(error, path)))
}

/// The fields after the version, which the local and the central header share.
struct Sizes {
    crc: u32,
    compressed: u32,
    size: u32,
    name: u16,
}

impl Sizes {
    /// Flags, method, time, date, CRC, sizes, name length and extra length.
    fn push(&self, out: &mut Vec<u8>, flags: u16) {
        push16(out, flags);
        push16(out, DEFLATED);
        push16(out, DOS_TIME);
        push16(out, DOS_DATE);
        push32(out, self.crc);
        push32(out, self.compressed);
        push32(out, self.size);
        push16(out, self.name);
        push16(out, 0);
    }
}

fn push16(out: &mut Vec<u8>, value: u16) {
    out.extend(value.to_le_bytes());
}

fn push32(out: &mut Vec<u8>, value: u32) {
    out.extend(value.to_le_bytes());
}

/// `n`, checked against `ZIP64_LIMIT` before.
fn as_u32(n: usize) -> u32 {
    u32::try_from(n).expect("checked against ZIP64_LIMIT")
}

fn too_large<I: Interpreter>(i: &I, path: &Path) -> I::Error {
    let mut message = path.as_os_str().to_owned();
    message.push(" would need zip64, which mogs does not write.");
    i.raise(Class::Build, message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fake::Fake;

    fn entries(pairs: &[(&str, &[u8])]) -> BTreeMap<String, Vec<u8>> {
        pairs
            .iter()
            .map(|(name, data)| ((*name).to_string(), data.to_vec()))
            .collect()
    }

    #[test]
    fn writes_zipfiles_layout_byte_for_byte() {
        // The headers `zipfile` writes for `ZipInfo("ab", date_time=(1980, 1, 1, 0, 0, 0))`
        // with `create_system = 3` and mode 0644. `Fake::deflate` keeps the data as it is.
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("a.zip");

        write_zip(&Fake::default(), &path, &entries(&[("ab", b"hi")])).unwrap();

        let crc = crc32fast::hash(b"hi").to_le_bytes();
        let mut expected = Vec::new();
        expected.extend(b"PK\x03\x04\x14\x00\x00\x00\x08\x00\x00\x00\x21\x00");
        expected.extend(crc);
        expected.extend(b"\x02\x00\x00\x00\x02\x00\x00\x00\x02\x00\x00\x00abhi");
        expected.extend(b"PK\x01\x02\x14\x03\x14\x00\x00\x00\x08\x00\x00\x00\x21\x00");
        expected.extend(crc);
        expected.extend(b"\x02\x00\x00\x00\x02\x00\x00\x00\x02\x00\x00\x00\x00\x00");
        expected.extend(b"\x00\x00\x00\x00\x00\x00\xa4\x81\x00\x00\x00\x00ab");
        expected.extend(b"PK\x05\x06\x00\x00\x00\x00\x01\x00\x01\x00\x30\x00\x00\x00");
        expected.extend(b"\x22\x00\x00\x00\x00\x00");

        assert_eq!(fs::read(&path).unwrap(), expected);
    }

    #[test]
    fn sorts_entries_and_makes_the_scripts_executable() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("a.zip");
        let zip = entries(&[("setup.sh", b"#!"), ("bundle.json", b"{}"), ("é.txt", b"")]);

        write_zip(&Fake::default(), &path, &zip).unwrap();

        let data = fs::read(&path).unwrap();
        let start = data.windows(4).position(|w| w == b"PK\x01\x02").unwrap();
        let mut modes = Vec::new();
        let mut at = start;

        while data[at..].starts_with(b"PK\x01\x02") {
            let field = |offset: usize, width: usize| {
                let bytes = &data[at + offset..at + offset + width];
                bytes.iter().rev().fold(0u32, |n, &b| n << 8 | u32::from(b))
            };
            let name_len = field(28, 2) as usize;
            let name = String::from_utf8(data[at + 46..at + 46 + name_len].to_vec()).unwrap();
            modes.push((name, field(38, 4) >> 16, field(8, 2)));
            at += 46 + name_len;
        }

        assert_eq!(
            modes,
            [
                ("bundle.json".to_string(), S_IFREG | 0o644, 0),
                ("setup.sh".to_string(), S_IFREG | 0o755, 0),
                ("é.txt".to_string(), S_IFREG | 0o644, 0x800),
            ]
        );
    }

    #[test]
    fn unwritable_path_is_an_os_error() {
        let error = write_zip(
            &Fake::default(),
            Path::new("/no/such/a.zip"),
            &BTreeMap::new(),
        )
        .unwrap_err();

        assert!(error.starts_with("OSError: "), "{error}");
    }
}
