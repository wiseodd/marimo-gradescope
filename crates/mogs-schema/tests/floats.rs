//! Numbers that are not finite cross JSON as Python's `json` writes them, in every message.

use std::fmt::Debug;

use mogs_schema::{Bundle, Job, Outcome, Results, SCHEMA, decode, encode};
use serde::de::value::{Error, StrDeserializer};
use serde::de::{
    DeserializeOwned, DeserializeSeed, IntoDeserializer, MapAccess, SeqAccess, Visitor,
};
use serde::{Deserializer, Serialize, forward_to_deserialize_any};

/// A deserializer that makes a value of any message type with every number infinite, every
/// optional value present and every list one item long. Each `f64` a type gains is covered
/// without editing this test.
#[derive(Clone, Copy)]
struct Fill {
    /// Whether this is the value of a `schema` key, which must be `SCHEMA`.
    version: bool,
}

const FILL: Fill = Fill { version: false };

impl<'de> Deserializer<'de> for Fill {
    type Error = Error;

    fn deserialize_any<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Error> {
        match self.version {
            true => visitor.visit_u64(SCHEMA.into()),
            false => visitor.visit_f64(f64::INFINITY),
        }
    }

    fn deserialize_bool<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Error> {
        visitor.visit_bool(true)
    }

    fn deserialize_u32<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Error> {
        visitor.visit_u32(1)
    }

    fn deserialize_f64<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Error> {
        visitor.visit_f64(f64::INFINITY)
    }

    fn deserialize_str<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Error> {
        visitor.visit_str("x")
    }

    fn deserialize_string<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Error> {
        visitor.visit_str("x")
    }

    fn deserialize_option<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Error> {
        visitor.visit_some(self)
    }

    fn deserialize_seq<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Error> {
        visitor.visit_seq(Items(1))
    }

    fn deserialize_struct<V: Visitor<'de>>(
        self,
        _name: &'static str,
        fields: &'static [&'static str],
        visitor: V,
    ) -> Result<V::Value, Error> {
        visitor.visit_map(Fields { fields, at: 0 })
    }

    fn deserialize_enum<V: Visitor<'de>>(
        self,
        _name: &'static str,
        variants: &'static [&'static str],
        visitor: V,
    ) -> Result<V::Value, Error> {
        let first: StrDeserializer<'_, Error> = variants[0].into_deserializer();

        visitor.visit_enum(first)
    }

    forward_to_deserialize_any! {
        i8 i16 i32 i64 i128 u8 u16 u64 u128 f32 char bytes byte_buf unit unit_struct
        newtype_struct tuple tuple_struct map identifier ignored_any
    }
}

struct Items(usize);

impl<'de> SeqAccess<'de> for Items {
    type Error = Error;

    fn next_element_seed<T: DeserializeSeed<'de>>(
        &mut self,
        seed: T,
    ) -> Result<Option<T::Value>, Error> {
        match self.0 {
            0 => Ok(None),
            _ => {
                self.0 -= 1;
                seed.deserialize(FILL).map(Some)
            }
        }
    }
}

struct Fields {
    fields: &'static [&'static str],
    at: usize,
}

impl<'de> MapAccess<'de> for Fields {
    type Error = Error;

    fn next_key_seed<K: DeserializeSeed<'de>>(
        &mut self,
        seed: K,
    ) -> Result<Option<K::Value>, Error> {
        let Some(field) = self.fields.get(self.at) else {
            return Ok(None);
        };
        let key: StrDeserializer<'_, Error> = field.into_deserializer();

        seed.deserialize(key).map(Some)
    }

    fn next_value_seed<V: DeserializeSeed<'de>>(&mut self, seed: V) -> Result<V::Value, Error> {
        let field = self.fields[self.at];
        self.at += 1;

        seed.deserialize(Fill {
            version: field == "schema",
        })
    }
}

/// A value of `T` with every number infinite.
fn filled<T: DeserializeOwned>() -> T {
    T::deserialize(FILL).unwrap()
}

/// `T`'s numbers are written as `Infinity`, never as `null`.
fn writes_infinity<T: DeserializeOwned + Serialize>() -> (T, String) {
    let value = filled::<T>();
    let text = encode(&value).unwrap();

    assert!(text.contains("Infinity"), "{text}");
    assert!(!text.contains("null"), "{text}");

    (value, text)
}

fn round_trips<T: DeserializeOwned + Serialize + PartialEq + Debug>() {
    let (value, text) = writes_infinity::<T>();

    assert_eq!(decode::<T>(&text).unwrap(), value, "{text}");
}

#[test]
fn every_number_of_every_message_keeps_infinity() {
    round_trips::<Bundle>();
    round_trips::<Job>();
    writes_infinity::<Results>();
    writes_infinity::<Outcome>();
}

#[test]
fn reads_and_writes_the_literals_python_writes() {
    let text = r#"{"schema": 1, "config": {"name": "Infinity", "notebook": "NaN \" -Infinity", "questions": [{"id": "q1", "title": "t", "points": -Infinity, "manual": false}, {"id": "q2", "title": "t", "points": NaN, "manual": false}], "files": [], "timeout": 60.0, "budget": Infinity, "memory_mb": 1, "python": "3.13"}, "setup_code": "", "cells": [], "helpers": []}"#;

    let bundle = decode::<Bundle>(text).unwrap();
    let config = &bundle.config;

    assert_eq!(config.name, "Infinity");
    assert_eq!(config.notebook, "NaN \" -Infinity");
    assert_eq!(config.questions[0].points, f64::NEG_INFINITY);
    assert!(config.questions[1].points.is_nan());
    assert_eq!(config.timeout, 60.0);
    assert_eq!(config.budget, f64::INFINITY);

    let written = encode(&bundle).unwrap();

    assert!(written.contains(r#""points":-Infinity"#), "{written}");
    assert!(written.contains(r#""points":NaN"#), "{written}");
    assert!(
        written.contains(r#""timeout":60.0,"budget":Infinity"#),
        "{written}"
    );
    assert!(written.contains(r#""name":"Infinity""#), "{written}");
}

#[test]
fn keeps_null_for_absent_values() {
    let job = Job {
        run_failed: None,
        ..filled()
    };
    let text = encode(&job).unwrap();

    assert!(text.contains(r#""run_failed":null"#), "{text}");
}

#[test]
fn rejects_a_literal_in_a_text_field() {
    let text = r#"{"schema": 1, "config": {"name": Infinity, "notebook": "n", "questions": [], "files": [], "timeout": 1, "budget": 1, "memory_mb": 1, "python": "3.13"}, "setup_code": "", "cells": [], "helpers": []}"#;

    assert!(decode::<Bundle>(text).is_err());
}

/// A writer that panics when serde_json writes `null`, after `number` has parked its value.
struct PanicOnNull;

impl std::io::Write for PanicOnNull {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        assert_ne!(buf, b"null", "a writer that fails mid-number");
        Ok(buf.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

#[test]
fn a_panic_while_writing_a_number_leaves_no_infinity_behind() {
    let bundle = filled::<Bundle>();
    let panicked = std::panic::catch_unwind(|| serde_json::to_writer(PanicOnNull, &bundle));
    assert!(panicked.is_err());

    // Every number finite, so nothing parks a value before `run_failed`.
    let mut job = Job {
        run_failed: None,
        ..filled()
    };
    let config = &mut job.bundle.config;
    config.timeout = 1.0;
    config.budget = 1.0;
    config.questions.iter_mut().for_each(|q| q.points = 1.0);
    let text = encode(&job).unwrap();

    assert!(text.contains(r#""run_failed":null"#), "{text}");
}

#[test]
fn rejects_text_that_is_not_json_without_panicking() {
    for text in ["é", "éInfinity", "{\"a\": é NaN}"] {
        assert!(decode::<Bundle>(text).is_err(), "{text}");
    }
}
