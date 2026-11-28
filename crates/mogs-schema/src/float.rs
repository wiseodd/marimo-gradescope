//! Numbers that are not finite, written and read as Python's `json` does.
//!
//! The config accepts infinity (such as `budget=1e999`), and a score can be infinite or NaN.
//! Python's `json` writes these as `Infinity`, `-Infinity` and `NaN`, which JSON lacks:
//! serde_json writes them as `null` and rejects them on reading. So every `f64` of a message
//! is marked `serialize_with = "crate::float::number"` (or [`optional`] for an `Option`) and
//! `deserialize_with` the same, and messages cross through [`encode`](crate::encode) and
//! [`decode`](crate::decode). The test `tests/floats.rs` fails for an `f64` left unmarked.
//!
//! Writing: [`number`] hands a non-finite value to serde_json, which calls the formatter's
//! `write_null` for it at once; the value waits in a thread-local cell, and [`Python`] writes
//! it instead of `null`. Every other `null` finds the cell empty.
//!
//! Reading: [`literals`] replaces each bare `Infinity`, `-Infinity` or `NaN` outside a string
//! with the object `{"\u0000": "<literal>"}`, which [`number`] reads back and a text field
//! rejects.

use std::borrow::Cow;
use std::cell::Cell;
use std::fmt;
use std::io;

use serde::de::{Error, MapAccess, Visitor};
use serde::{Deserializer, Serializer};
use serde_json::ser::Formatter;

thread_local! {
    /// The non-finite number serde_json is writing as `null` right now.
    static PENDING: Cell<Option<f64>> = const { Cell::new(None) };
}

/// The key of the object that stands for a literal while reading.
const MARK: &str = "\u{0}";

/// The literals Python's `json` writes, longest first so `-Infinity` is not read as `-`.
const LITERALS: [&str; 3] = ["-Infinity", "Infinity", "NaN"];

/// serde_json's compact format, with non-finite numbers as Python writes them.
pub(crate) struct Python;

impl Formatter for Python {
    fn write_null<W: ?Sized + io::Write>(&mut self, writer: &mut W) -> io::Result<()> {
        match PENDING.take() {
            None => writer.write_all(b"null"),
            Some(x) if x.is_nan() => writer.write_all(b"NaN"),
            Some(x) if x > 0.0 => writer.write_all(b"Infinity"),
            Some(_) => writer.write_all(b"-Infinity"),
        }
    }
}

/// `text` with each literal outside a string as the object [`number`] reads.
pub(crate) fn literals(text: &str) -> Cow<'_, str> {
    let mut out = String::new();
    let mut copied = 0;
    let mut in_string = false;
    let mut escaped = false;
    let mut at = 0;

    while at < text.len() {
        let byte = text.as_bytes()[at];

        if in_string {
            match (escaped, byte) {
                (true, _) => escaped = false,
                (false, b'\\') => escaped = true,
                (false, b'"') => in_string = false,
                _ => {}
            }
            at += 1;
            continue;
        }

        if byte == b'"' {
            in_string = true;
            at += 1;
            continue;
        }

        // Bytes, as `at` can fall inside a character of text that is not JSON. A literal is
        // ASCII, so a match starts and ends on character boundaries.
        let rest = &text.as_bytes()[at..];

        match LITERALS.iter().find(|l| rest.starts_with(l.as_bytes())) {
            Some(literal) => {
                out.push_str(&text[copied..at]);
                out.push_str(&format!(r#"{{"\u0000":"{literal}"}}"#));
                at += literal.len();
                copied = at;
            }
            None => at += 1,
        }
    }

    match copied {
        0 => Cow::Borrowed(text),
        _ => {
            out.push_str(&text[copied..]);
            Cow::Owned(out)
        }
    }
}

/// Write and read an `f64` field.
pub mod number {
    use super::*;

    /// Write `x`, as [`Python`] needs for a non-finite one.
    ///
    /// # Errors
    ///
    /// If the serializer fails.
    pub fn serialize<S: Serializer>(x: &f64, serializer: S) -> Result<S::Ok, S::Error> {
        if x.is_finite() {
            return serializer.serialize_f64(*x);
        }

        PENDING.set(Some(*x));
        // Empties the cell on the way out, a panic in the serializer included.
        let _clear = Clear;

        serializer.serialize_f64(*x)
    }

    /// Empties [`PENDING`] when dropped.
    struct Clear;

    impl Drop for Clear {
        fn drop(&mut self) {
            PENDING.set(None);
        }
    }

    /// Read a number, or a literal [`literals`] marked.
    ///
    /// # Errors
    ///
    /// If the value is neither.
    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<f64, D::Error> {
        deserializer.deserialize_any(Number)
    }
}

/// Write and read an `Option<f64>` field that is left out when `None`.
pub mod optional {
    use super::*;

    /// Write the number in `x`.
    ///
    /// # Errors
    ///
    /// If the serializer fails.
    pub fn serialize<S: Serializer>(x: &Option<f64>, serializer: S) -> Result<S::Ok, S::Error> {
        match x {
            Some(x) => number::serialize(x, serializer),
            None => serializer.serialize_none(),
        }
    }

    /// Read a present number, never `null`.
    ///
    /// # Errors
    ///
    /// If the value is not a number.
    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<f64>, D::Error> {
        number::deserialize(deserializer).map(Some)
    }
}

struct Number;

impl<'de> Visitor<'de> for Number {
    type Value = f64;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "a number")
    }

    fn visit_f64<E: Error>(self, x: f64) -> Result<f64, E> {
        Ok(x)
    }

    fn visit_u64<E: Error>(self, x: u64) -> Result<f64, E> {
        Ok(x as f64)
    }

    fn visit_i64<E: Error>(self, x: i64) -> Result<f64, E> {
        Ok(x as f64)
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<f64, A::Error> {
        let literal = match map.next_entry::<String, String>()? {
            Some((key, literal)) if key == MARK => literal,
            _ => return Err(A::Error::custom("expected a number")),
        };

        if map.next_key::<String>()?.is_some() {
            return Err(A::Error::custom("expected a number"));
        }

        match literal.as_str() {
            "Infinity" => Ok(f64::INFINITY),
            "-Infinity" => Ok(f64::NEG_INFINITY),
            "NaN" => Ok(f64::NAN),
            _ => Err(A::Error::custom("expected a number")),
        }
    }
}
