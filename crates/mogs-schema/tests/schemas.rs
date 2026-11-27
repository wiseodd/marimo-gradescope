//! The wire types round trip, reject other versions, and match the checked-in schemas.

use std::collections::BTreeMap;
use std::fmt::Debug;
use std::path::{Path, PathBuf};

use mogs_schema::{
    Answer, Bundle, BundleConfig, BundleQuestion, Event, EventLine, FailureReason, HiddenHelper,
    Inspect, InspectConfig, InspectQuestion, Job, Outcome, Results, Row, SCHEMA, TestCell,
    TestFile, decode, schemas,
};
use serde::Serialize;
use serde::de::DeserializeOwned;

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
            unavailable: BTreeMap::from([("x".into(), "it raised".into())]),
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

    round_trip(Inspect {
        schema: SCHEMA,
        source_error: None,
        config_error: None,
        config: Some(InspectConfig { name: "hw".into() }),
        problems: vec!["line 3: bad".into()],
        questions: vec![InspectQuestion {
            id: "q2".into(),
            title: "Question 2".into(),
            points: 3.0,
            manual: true,
            public: vec![],
            hidden: vec![],
            prompts: vec!["Explain why.".into()],
        }],
        answers: vec![Answer {
            qid: "q2".into(),
            cell: "answer_q2".into(),
            text: Some("Because.".into()),
            source: "@app.cell\ndef answer_q2():\n    mo.md(\"Because.\")\n".into(),
        }],
        test_files: vec![TestFile {
            qid: "q1".into(),
            path: "tests/q1.py".into(),
            tests: vec!["test_q1_a".into()],
            error: None,
        }],
        public_job: Some(bundle()),
    });
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
