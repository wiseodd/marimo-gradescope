//! The JSON messages between the `mogs` executable and its Python processes.
//!
//! These types are the source of truth: the JSON Schemas in the repo's `schemas/` folder are
//! generated from them (see [`schemas`]). Every message except [`Results`] carries `"schema"`,
//! the format version [`SCHEMA`]. Fields serialize in the order Python writes its keys.

mod bundle;
mod event;
mod inspect;
mod job;
mod outcome;
mod results;

use std::fmt;

use schemars::transform::{RecursiveTransform, Transform};
use schemars::{JsonSchema, Schema, schema_for};
use serde::de::DeserializeOwned;
use serde_json::Value;

pub use bundle::{Bundle, BundleConfig, BundleQuestion, HiddenHelper, TestCell};
pub use event::{Event, EventLine};
pub use inspect::{
    Answer, Inspect, InspectConfig, InspectQuestion, TestFile, TestFileError, Utf8Reason,
};
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

/// The JSON Schema of each message, by the file stem it is checked in under.
///
/// Every key of a versioned message is required, `null` ones included, as Python writes them
/// all and `decode` requires them. `results.json` leaves out the keys Gradescope does not need.
pub fn schemas() -> Vec<(&'static str, Schema)> {
    vec![
        ("job", versioned::<Job>()),
        ("event", versioned::<EventLine>()),
        ("bundle", versioned::<Bundle>()),
        ("results", schema_for!(Results)),
        ("inspect", versioned::<Inspect>()),
    ]
}

/// The schema of a versioned message. schemars lists `Option` fields as optional even when
/// serde requires them (`deserialize_with = "Option::deserialize"`), so this marks every key
/// required.
fn versioned<T: JsonSchema>() -> Schema {
    let mut schema = schema_for!(T);
    RecursiveTransform(require_all).transform(&mut schema);

    schema
}

/// Make every property of an object schema required.
fn require_all(schema: &mut Schema) {
    let Some(Value::Object(properties)) = schema.get("properties") else {
        return;
    };
    let keys: Vec<Value> = properties.keys().cloned().map(Value::String).collect();

    schema.insert("required".into(), Value::Array(keys));
}
