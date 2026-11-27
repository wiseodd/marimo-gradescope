//! The wire types round trip, reject other versions, and match the checked-in schemas.

use std::fmt::Debug;
use std::path::{Path, PathBuf};

use indexmap::IndexMap;
use mogs_schema::{
    Bundle, BundleConfig, BundleQuestion, Event, EventLine, FailureReason, HiddenHelper, Job,
    Outcome, Results, Row, SCHEMA, TestCell, decode, schemas,
};
use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::{Map, Value};

fn bundle() -> Bundle {
    Bundle {
        schema: SCHEMA,
        config: BundleConfig {
            name: "hw".into(),
            notebook: "hw.py".into(),
            questions: vec![BundleQuestion {
                id: "q1".into(),
                title: "Question 1".into(),
                points: 2.5,
                manual: false,
            }],
            files: vec!["data.csv".into()],
            timeout: 60.0,
            budget: 540.0,
            memory_mb: 4096,
            python: "3.13".into(),
        },
        setup_code: "import math\n".into(),
        cells: vec![TestCell {
            qid: "q1".into(),
            label: "q1-tests".into(),
            hidden: true,
            source: "def test_q1_a():\n    assert add(1, 1) == 2\n".into(),
            tests: vec!["test_q1_a".into()],
            uses: vec!["add".into()],
            needs: vec!["secret".into()],
        }],
        helpers: vec![HiddenHelper {
            source: "secret = 1\n".into(),
            defines: vec!["secret".into()],
            uses: vec![],
            needs: vec![],
        }],
    }
}

fn outcome() -> Outcome {
    Outcome {
        name: "test_q1_a".into(),
        passed: false,
        message: "AssertionError".into(),
        reason: Some(FailureReason::OutOfMemory),
        seconds: 0.5,
    }
}

fn round_trip<T: Serialize + DeserializeOwned + PartialEq + Debug>(value: T) {
    let text = serde_json::to_string(&value).unwrap();

    assert_eq!(decode::<T>(&text).unwrap(), value, "{text}");
}

#[test]
fn round_trips_each_type() {
    round_trip(bundle());
    round_trip(Job {
        schema: SCHEMA,
        bundle: bundle(),
        submission: PathBuf::from("/tmp/submission.py"),
        skip: vec!["test_q1_b".into()],
        run_failed: Some("exit code 3".into()),
        alarm: true,
    });

    let events = [
        Event::Loaded {
            error: None,
            unavailable: IndexMap::from([
                ("y".into(), "it raised".into()),
                ("x".into(), "no cell defines it".into()),
            ]),
        },
        Event::Loading {
            tests: vec!["test_q1_a".into()],
        },
        Event::Start {
            test: "test_q1_a".into(),
        },
        Event::Result(outcome()),
    ];

    for event in events {
        round_trip(EventLine {
            schema: SCHEMA,
            event,
        });
    }
}

/// The error of decoding `value` as a `T` after `edit` changes the object at `path`.
fn decode_edited<T: DeserializeOwned + Debug>(
    value: &impl Serialize,
    path: &[&str],
    edit: impl FnOnce(&mut Map<String, Value>),
) -> String {
    let mut json = serde_json::to_value(value).unwrap();
    let mut object = &mut json;

    for step in path {
        object = match step.parse::<usize>() {
            Ok(index) => &mut object[index],
            Err(_) => &mut object[*step],
        };
    }

    edit(object.as_object_mut().unwrap());

    match decode::<T>(&json.to_string()) {
        Ok(_) => panic!("decoded with the edit at {path:?}"),
        Err(error) => error.to_string(),
    }
}

/// Decoding `value` without the key at `path` fails, naming the key.
fn rejects_without<T: DeserializeOwned + Debug>(value: &impl Serialize, path: &[&str], key: &str) {
    let error = decode_edited::<T>(value, path, |object| {
        assert!(object.remove(key).is_some(), "no key {key} at {path:?}");
    });

    assert!(error.contains(&format!("missing field `{key}`")), "{error}");
}

/// Decoding `value` with an extra key at `path` fails, naming the key.
fn rejects_extra<T: DeserializeOwned + Debug>(value: &impl Serialize, path: &[&str]) {
    let error = decode_edited::<T>(value, path, |object| {
        object.insert("bogus".into(), Value::Null);
    });

    assert!(error.contains("unknown field `bogus`"), "{path:?}: {error}");
}

fn job() -> Job {
    Job {
        schema: SCHEMA,
        bundle: bundle(),
        submission: PathBuf::from("s.py"),
        skip: vec![],
        run_failed: None,
        alarm: true,
    }
}

fn events() -> Vec<EventLine> {
    let events = [
        Event::Loaded {
            error: None,
            unavailable: IndexMap::new(),
        },
        Event::Loading { tests: vec![] },
        Event::Start { test: "t".into() },
        Event::Result(outcome()),
    ];

    events
        .into_iter()
        .map(|event| EventLine {
            schema: SCHEMA,
            event,
        })
        .collect()
}

#[test]
fn rejects_unknown_keys() {
    for path in [
        &[][..],
        &["bundle"],
        &["bundle", "config"],
        &["bundle", "config", "questions", "0"],
        &["bundle", "cells", "0"],
        &["bundle", "helpers", "0"],
    ] {
        rejects_extra::<Job>(&job(), path);
    }

    for event in events() {
        rejects_extra::<EventLine>(&event, &[]);
    }

    let results = r#"{"tests": [], "stdout_visibility": "hidden", "bogus": 1}"#;
    let error = serde_json::from_str::<Results>(results)
        .unwrap_err()
        .to_string();

    assert!(error.contains("unknown field `bogus`"), "{error}");
}

#[test]
fn rejects_nested_bundle_of_other_version() {
    let other = |object: &mut Map<String, Value>| {
        object.insert("schema".into(), 2.into());
    };

    let error = decode_edited::<Job>(&job(), &["bundle"], other);
    assert!(error.contains("schema version 2, expected 1"), "{error}");
}

#[test]
fn keeps_the_order_of_unavailable_names() {
    let line = r#"{"schema":1,"event":"loaded","error":null,"unavailable":{"b":"x","a":"y"}}"#;
    let event = decode::<EventLine>(line).unwrap();

    assert_eq!(serde_json::to_string(&event).unwrap(), line);
}

#[test]
fn results_reject_null_for_left_out_keys() {
    for key in ["score", "output", "output_format"] {
        let text = format!(r#"{{"tests": [], "stdout_visibility": "hidden", "{key}": null}}"#);

        assert!(serde_json::from_str::<Results>(&text).is_err(), "{key}");
    }

    for key in ["number", "status"] {
        let row = format!(
            r#"{{"name": "n", "score": 0, "max_score": 0, "output": "", "output_format": "md", "visibility": "visible", "{key}": null}}"#
        );
        let text = format!(r#"{{"tests": [{row}], "stdout_visibility": "hidden"}}"#);

        assert!(serde_json::from_str::<Results>(&text).is_err(), "{key}");
    }
}

#[test]
fn rejects_missing_nullable_keys() {
    let job = job();
    let [loaded, ..] = &events()[..] else {
        unreachable!()
    };
    let result = EventLine {
        schema: SCHEMA,
        event: Event::Result(Outcome {
            reason: None,
            ..outcome()
        }),
    };

    rejects_without::<Job>(&job, &[], "run_failed");
    rejects_without::<EventLine>(loaded, &[], "error");
    rejects_without::<EventLine>(&result, &[], "reason");
    rejects_without::<Bundle>(&bundle(), &["cells", "0"], "label");
}

#[test]
fn outcome_and_results_serialize_as_python_does() {
    let line = EventLine {
        schema: SCHEMA,
        event: Event::Result(outcome()),
    };
    let text = serde_json::to_string(&line).unwrap();

    assert_eq!(
        text,
        r#"{"schema":1,"event":"result","name":"test_q1_a","passed":false,"message":"AssertionError","reason":"out_of_memory","seconds":0.5}"#
    );

    let results = Results {
        tests: vec![Row {
            name: "Question 1".into(),
            score: 1.0,
            max_score: 2.0,
            output: "ok".into(),
            output_format: "md".into(),
            visibility: "hidden".into(),
            number: Some("q1".into()),
            status: None,
        }],
        stdout_visibility: "hidden".into(),
        score: None,
        output: None,
        output_format: None,
    };
    let text = serde_json::to_string(&results).unwrap();

    assert_eq!(
        text,
        r#"{"tests":[{"name":"Question 1","score":1.0,"max_score":2.0,"output":"ok","output_format":"md","visibility":"hidden","number":"q1"}],"stdout_visibility":"hidden"}"#
    );
    assert_eq!(serde_json::from_str::<Results>(&text).unwrap(), results);
}

#[test]
fn rejects_other_schema_version() {
    let error = decode::<Job>(r#"{"schema": 2, "bundle": {}}"#).unwrap_err();

    assert_eq!(error.to_string(), "schema version 2, expected 1");

    let error = decode::<EventLine>(r#"{"event": "start", "test": "t"}"#).unwrap_err();

    assert_eq!(error.to_string(), "missing schema version, expected 1");
}

fn schema_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../schemas")
}

#[test]
fn checked_in_schemas_are_current() {
    let bless = std::env::var_os("MOGS_BLESS").is_some_and(|value| value == "1");
    let mut stale = vec![];

    for (name, schema) in schemas() {
        let path = schema_dir().join(format!("{name}.schema.json"));
        let fresh = serde_json::to_string_pretty(&schema).unwrap() + "\n";

        if bless {
            std::fs::write(&path, &fresh).unwrap();
        } else if std::fs::read_to_string(&path).ok().as_deref() != Some(fresh.as_str()) {
            stale.push(path.display().to_string());
        }
    }

    assert!(
        stale.is_empty(),
        "stale schemas, rerun with MOGS_BLESS=1: {stale:?}"
    );
}

#[test]
fn every_checked_in_schema_is_generated() {
    let generated: Vec<String> = schemas()
        .into_iter()
        .map(|(name, _)| format!("{name}.schema.json"))
        .collect();
    let mut extra: Vec<String> = std::fs::read_dir(schema_dir())
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|name| !generated.contains(name))
        .collect();
    extra.sort();

    assert!(extra.is_empty(), "no type generates {extra:?}");
}
