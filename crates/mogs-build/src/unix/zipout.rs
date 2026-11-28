//! Reproducible zips, byte for byte as `zipfile` writes them for v0.7.
//!
//! Entries are sorted, dated 1980-01-01, deflated, and marked as made on Unix with mode 0755
//! for the scripts and 0644 for the rest. Each local header carries the sizes and the CRC, with
//! no data descriptor and no extra fields, as `zipfile` writes to a seekable file.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::Path;

use crate::unix::{Class, Interpreter};

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

/// The content of one entry, deflated.
pub struct Deflated {
    /// CRC-32 of the content.
    crc: u32,
    /// Size of the content.
    size: usize,
    /// The deflated content.
    bytes: Vec<u8>,
}

impl Deflated {
    /// `data`, deflated with Python's zlib.
    ///
    /// # Errors
    ///
    /// Any Python exception.
    pub fn new<I: Interpreter>(i: &I, data: &[u8]) -> Result<Self, I::Error> {
        Ok(Self {
            crc: crc32fast::hash(data),
            size: data.len(),
            bytes: i.deflate(data)?,
        })
    }
}

/// Write `entries` to the zip `path`, by name in sorted order.
///
/// # Errors
///
/// `OSError` if the file cannot be written, and `BuildError` for a zip that needs zip64.
pub fn write_zip<I: Interpreter>(
    i: &I,
    path: &Path,
    entries: &BTreeMap<String, &Deflated>,
) -> Result<(), I::Error> {
    let os = |error| i.os(error, path);
    let mut out = BufWriter::new(File::create(path).map_err(os)?);
    let mut offset = 0;
    let mut central = Vec::new();

    for (name, entry) in entries {
        let compressed = &entry.bytes;

        if needs_zip64(entry.size, compressed.len(), offset) {
            return Err(too_large(i, path));
        }

        let flags = if name.is_ascii() { 0 } else { UTF8_NAME };
        let mode = if SCRIPTS.contains(&name.as_str()) {
            0o755
        } else {
            0o644
        };
        let sizes = Sizes {
            crc: entry.crc,
            compressed: as_u32(compressed.len()),
            size: as_u32(entry.size),
            name: u16::try_from(name.len()).map_err(|_| too_large(i, path))?,
        };

        let mut local = Vec::from(*b"PK\x03\x04");
        push16(&mut local, VERSION);
        sizes.push(&mut local, flags);
        local.extend(name.as_bytes());
        out.write_all(&local).map_err(os)?;
        out.write_all(compressed).map_err(os)?;

        central.extend(b"PK\x01\x02");
        central.extend([VERSION as u8, UNIX]);
        push16(&mut central, VERSION);
        sizes.push(&mut central, flags);
        // Comment length, disk number and internal attributes.
        central.extend([0; 6]);
        push32(&mut central, (S_IFREG | mode) << 16);
        push32(&mut central, as_u32(offset));
        central.extend(name.as_bytes());
        offset += local.len() + compressed.len();
    }

    if entries.len() > ZIP_FILECOUNT_LIMIT || offset + central.len() > ZIP64_LIMIT {
        return Err(too_large(i, path));
    }

    let count = entries.len() as u16;
    let mut end = Vec::from(*b"PK\x05\x06");
    // This disk and the disk where the central directory starts.
    end.extend([0; 4]);
    push16(&mut end, count);
    push16(&mut end, count);
    push32(&mut end, as_u32(central.len()));
    push32(&mut end, as_u32(offset));
    // Comment length.
    push16(&mut end, 0);
    out.write_all(&central).map_err(os)?;
    out.write_all(&end).map_err(os)?;

    out.flush().map_err(os)
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

/// Whether `zipfile` would write zip64 fields for an entry of `size` bytes, `compressed` to
/// that, at `offset`. Its local header gets them once the data could pass the limit, at 1.05
/// times the size.
fn needs_zip64(size: usize, compressed: usize, offset: usize) -> bool {
    size as f64 * 1.05 > ZIP64_LIMIT as f64 || compressed > ZIP64_LIMIT || offset > ZIP64_LIMIT
}

fn too_large<I: Interpreter>(i: &I, path: &Path) -> I::Error {
    let message = [
        path.as_os_str(),
        " would need zip64, which mogs does not write.".as_ref(),
    ];
    i.raise(Class::Build, message.into_iter().collect())
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::*;
    use crate::unix::fake::Fake;

    /// `pairs` deflated by `Fake`, which keeps the data as it is.
    fn deflated(pairs: &[(&str, &[u8])]) -> Vec<(String, Deflated)> {
        pairs
            .iter()
            .map(|(name, data)| {
                (
                    (*name).to_string(),
                    Deflated::new(&Fake::default(), data).unwrap(),
                )
            })
            .collect()
    }

    fn entries(deflated: &[(String, Deflated)]) -> BTreeMap<String, &Deflated> {
        deflated
            .iter()
            .map(|(name, entry)| (name.clone(), entry))
            .collect()
    }

    #[test]
    fn writes_zipfiles_layout_byte_for_byte() {
        // The headers `zipfile` writes for `ZipInfo("ab", date_time=(1980, 1, 1, 0, 0, 0))`
        // with `create_system = 3` and mode 0644. `Fake::deflate` keeps the data as it is.
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("a.zip");

        let zip = deflated(&[("ab", b"hi")]);
        write_zip(&Fake::default(), &path, &entries(&zip)).unwrap();

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
        let zip = deflated(&[("setup.sh", b"#!"), ("bundle.json", b"{}"), ("é.txt", b"")]);

        write_zip(&Fake::default(), &path, &entries(&zip)).unwrap();

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
    fn zip64_starts_where_zipfile_starts_it() {
        let below = (ZIP64_LIMIT as f64 / 1.05) as usize;

        assert!(!needs_zip64(below, 0, 0));
        assert!(needs_zip64(below + 1, 0, 0));
        assert!(needs_zip64(0, ZIP64_LIMIT + 1, 0));
        assert!(needs_zip64(0, 0, ZIP64_LIMIT + 1));
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
