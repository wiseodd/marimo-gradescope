//! The JSON messages between the Rust side of mogs and its Python processes.
//!
//! These types are the source of truth: the JSON Schemas in the repo's `schemas/` folder are
//! generated from them (see [`schemas`]). Every message except [`Results`] carries `"schema"`,
//! the format version [`SCHEMA`]. Fields serialize in the order Python writes its keys.

mod bundle;
mod event;
mod job;
mod outcome;
mod results;

use std::collections::BTreeSet;
use std::fmt;

use schemars::transform::{RecursiveTransform, Transform};
use schemars::{JsonSchema, Schema, schema_for};
use serde::de::{DeserializeOwned, Error as _};
use serde::{Deserialize, Deserializer};
use serde_json::Value;

pub use bundle::{Bundle, BundleConfig, BundleQuestion, HiddenHelper, TestCell};
pub use event::{Event, EventLine};
pub use job::Job;
pub use outcome::{FailureReason, Outcome};
pub use results::{Results, Row};

/// Version of the message formats. Bump it on any change a reader of the old format would
/// misread.
pub const SCHEMA: u32 = 1;

/// Why a message could not be read.
#[derive(Debug)]
pub enum WireError {
    /// The text is not JSON, or not the expected message.
    Json(serde_json::Error),
    /// The message has no `schema` key.
    MissingVersion,
    /// The message is of another format version.
    Version(Value),
}

impl fmt::Display for WireError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            WireError::Json(error) => write!(f, "{error}"),
            WireError::MissingVersion => write!(f, "missing schema version, expected {SCHEMA}"),
            WireError::Version(found) => write!(f, "schema version {found}, expected {SCHEMA}"),
        }
    }
}

impl std::error::Error for WireError {}

impl From<serde_json::Error> for WireError {
    fn from(error: serde_json::Error) -> Self {
        WireError::Json(error)
    }
}

/// Parse a message, checking its `schema` version before its other fields.
///
/// # Errors
///
/// If the text is not JSON, has no `schema` key or another version, or is not a `T`.
pub fn decode<T: DeserializeOwned>(text: &str) -> Result<T, WireError> {
    let value: Value = serde_json::from_str(text)?;

    match value.get("schema") {
        None => return Err(WireError::MissingVersion),
        Some(found) if found.as_u64() != Some(SCHEMA.into()) => {
            return Err(WireError::Version(found.clone()));
        }
        Some(_) => {}
    }

    Ok(serde_json::from_value(value)?)
}

/// Read a `schema` field, failing on any version but `SCHEMA` with the text `decode` uses.
/// This also checks the messages nested in another, such as a job's bundle.
fn version<'de, D: Deserializer<'de>>(deserializer: D) -> Result<u32, D::Error> {
    let found = Value::deserialize(deserializer)?;

    match found.as_u64() == Some(SCHEMA.into()) {
        true => Ok(SCHEMA),
        false => Err(D::Error::custom(WireError::Version(found))),
    }
}

/// Read a key that may be left out but is never `null`.
fn present<'de, D: Deserializer<'de>, T: Deserialize<'de>>(
    deserializer: D,
) -> Result<Option<T>, D::Error> {
    T::deserialize(deserializer).map(Some)
}

/// The JSON Schema of each message, by the file stem it is checked in under.
///
/// Every key of a versioned message is required, `null` ones included, as Python writes them
/// all and `decode` requires them. `results.json` leaves out the keys Gradescope does not need.
/// No schema allows a key it does not name, as `decode` rejects them too.
pub fn schemas() -> Vec<(&'static str, Schema)> {
    vec![
        ("job", versioned::<Job>()),
        ("event", versioned::<EventLine>()),
        ("bundle", versioned::<Bundle>()),
        ("results", closed(schema_for!(Results))),
    ]
}

/// The schema of a versioned message. schemars lists `Option` fields as optional even when
/// serde requires them (`deserialize_with = "Option::deserialize"`), so this marks every key
/// required.
fn versioned<T: JsonSchema>() -> Schema {
    let mut schema = schema_for!(T);
    RecursiveTransform(require_all).transform(&mut schema);

    closed(schema)
}

/// Make every property of an object schema required.
fn require_all(schema: &mut Schema) {
    let Some(Value::Object(properties)) = schema.get("properties") else {
        return;
    };
    let keys: Vec<Value> = properties.keys().cloned().map(Value::String).collect();

    schema.insert("required".into(), Value::Array(keys));
}

/// `schema` with `unevaluatedProperties: false` on each object schema that stands alone.
///
/// A `oneOf`, `anyOf` or `allOf` branch stays open, and so does a definition that a branch
/// merges in by `$ref` next to its own properties (a flattened struct): the schema around
/// them names the other keys, and its `unevaluatedProperties` sees the keys they name.
fn closed(schema: Schema) -> Schema {
    let mut value = schema.to_value();
    let mut merged = BTreeSet::new();
    merged_refs(&value, &mut merged);

    if let Some(Value::Object(definitions)) = value.get_mut("$defs") {
        for (name, definition) in definitions {
            close(definition, !merged.contains(&format!("#/$defs/{name}")));
        }
    }

    close(&mut value, true);

    Schema::try_from(value).expect("a schema stays an object")
}

/// The `$ref` targets of schemas that also have their own `properties`.
fn merged_refs(value: &Value, found: &mut BTreeSet<String>) {
    match value {
        Value::Object(object) => {
            if let (Some(Value::String(target)), true) =
                (object.get("$ref"), object.contains_key("properties"))
            {
                found.insert(target.clone());
            }

            object.values().for_each(|child| merged_refs(child, found));
        }
        Value::Array(items) => items.iter().for_each(|child| merged_refs(child, found)),
        _ => {}
    }
}

/// Close `value` when it stands `alone` and describes an object, then its subschemas.
/// `closed` handles `$defs`.
fn close(value: &mut Value, alone: bool) {
    let Value::Object(object) = value else {
        return;
    };

    let branches_have_properties = ["oneOf", "anyOf"].iter().any(|key| {
        object
            .get(*key)
            .and_then(Value::as_array)
            .is_some_and(|branches| branches.iter().any(|b| b.get("properties").is_some()))
    });

    if alone && (object.contains_key("properties") || branches_have_properties) {
        object.insert("unevaluatedProperties".into(), Value::Bool(false));
    }

    for (key, child) in object.iter_mut() {
        match key.as_str() {
            "properties" => {
                if let Value::Object(properties) = child {
                    properties.values_mut().for_each(|p| close(p, true));
                }
            }
            "items" | "additionalProperties" => close(child, true),
            "oneOf" | "anyOf" | "allOf" => {
                if let Value::Array(branches) = child {
                    branches.iter_mut().for_each(|b| close(b, false));
                }
            }
            _ => {}
        }
    }
}
