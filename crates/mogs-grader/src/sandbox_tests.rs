//! `sandbox::grade` with the real grading child, `python -m mogs.grader.child`.
//!
//! The child runs on the Python of the project's environment, `$MOGS_TEST_PYTHON`, which
//! `.cargo/config.toml` points at the `.venv` that `uv sync` makes.

use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use indexmap::IndexMap;
use mogs_schema::{
    Bundle, BundleConfig, BundleQuestion, FailureReason, HiddenHelper, Outcome, TestCell,
};
use rustix::process::{Pid, Signal};
use tempfile::TempDir;

use crate::fake::{FakeHost, spawning, writing_script};
use crate::files::data_files;
use crate::process::OUTPUT_LIMIT;
use crate::sandbox::{GradeOptions, Graded, grade};
use crate::{GradeError, Level};

#[cfg(target_os = "linux")]
use FailureReason::OutOfMemory;
use FailureReason::{Assertion, Budget, Killed, LoadFailed, NotRun, Timeout};

const GOOD: [&str; 3] = ["test_q1_before", "test_q1_after", "test_q2_other"];
const ALL: [&str; 4] = [
    "test_q1_before",
    "test_q1_bad",
    "test_q1_after",
    "test_q2_other",
];

const Q1: &str = "\
def test_q1_before():
    assert add(1, 2) == 3

def test_q1_bad():
    assert misbehave() is True

def test_q1_after():
    assert add(2, 2) == 4
";

const Q2: &str = "\
def test_q2_other():
    assert add(0, 0) == 0
";

const NEEDS_RUN: &str = "\
def test_q2_other():
    assert nothing_here() is None
";

const ADD_AND_MISBEHAVE: &[&str] = &["add", "misbehave"];

/// Held while grading. The tests' time limits assume the children do not compete for the CPU
/// with other tests' children.
static SERIAL: Mutex<()> = Mutex::new(());

/// The project's Python, which has mogs.
fn python() -> PathBuf {
    let python = std::env::var_os("MOGS_TEST_PYTHON")
        .expect("the sandbox tests need the project's Python: `.cargo/config.toml` names it");

    PathBuf::from(python)
}

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures")
        .join(format!("{name}.py"))
}

/// A test cell of `qid` whose tests are the `def test_...` lines of `source`.
fn cell(qid: &str, source: &str, hidden: bool, uses: &[&str]) -> TestCell {
    let tests = source
        .lines()
        .filter_map(|line| line.strip_prefix("def "))
        .filter(|line| line.starts_with("test_"))
        .map(|line| line.split('(').next().unwrap_or_default().to_string())
        .collect();

    TestCell {
        qid: qid.into(),
        label: format!("{qid}-tests"),
        hidden,
        source: source.into(),
        tests,
        uses: uses.iter().map(|name| (*name).into()).collect(),
        needs: Vec::new(),
    }
}

/// Questions q1 and q2 worth 1 point each, with the given limits.
fn bundle_with(cells: Vec<TestCell>, timeout: f64, budget: f64) -> Bundle {
    let question = |id: &str| BundleQuestion {
        id: id.into(),
        title: id.into(),
        points: 1.0,
        manual: false,
    };

    Bundle {
        schema: mogs_schema::SCHEMA,
        config: BundleConfig {
            name: "A".into(),
            notebook: "a.py".into(),
            questions: vec![question("q1"), question("q2")],
            files: Vec::new(),
            timeout,
            budget,
            memory_mb: 1024,
            python: "3.13".into(),
        },
        setup_code: String::new(),
        cells,
        helpers: Vec::new(),
    }
}

fn bundle(cells: Vec<TestCell>) -> Bundle {
    bundle_with(cells, 1.0, 60.0)
}

/// The two usual cells: q1 with a misbehaving test, and q2.
fn usual() -> Vec<TestCell> {
    vec![
        cell("q1", Q1, false, ADD_AND_MISBEHAVE),
        cell("q2", Q2, false, ADD_AND_MISBEHAVE),
    ]
}

/// Q1 with `body` as the test `test_q1_bad`.
fn hostile(body: &str) -> Vec<TestCell> {
    let indented: String = body.lines().map(|line| format!("    {line}\n")).collect();
    let source = Q1.replace("    assert misbehave() is True\n", &indented);

    vec![
        cell("q1", &source, false, ADD_AND_MISBEHAVE),
        cell("q2", Q2, false, ADD_AND_MISBEHAVE),
    ]
}

/// A submission with `add`, then `body`.
fn notebook(tmp: &Path, body: &str) -> PathBuf {
    let path = tmp.join("submission.py");
    let header = "import marimo\n\napp = marimo.App()\n\n\n@app.function\ndef add(a, b):\n";
    fs::write(&path, format!("{header}    return a + b\n\n{body}")).unwrap();

    path
}

/// No data files.
static NO_DATA: LazyLock<IndexMap<String, PathBuf>> = LazyLock::new(IndexMap::new);

/// Grading options without a user or data files.
fn options(python: &Path) -> GradeOptions<'_> {
    GradeOptions {
        user: None,
        data: &NO_DATA,
        python: python.as_os_str(),
    }
}

/// Grade into `tmp/work`, with `host`.
fn graded_with(
    tmp: &Path,
    submission: &Path,
    bundle: &Bundle,
    options: &GradeOptions,
    host: &FakeHost,
) -> Result<Graded, GradeError> {
    let workdir = tmp.join("work");
    fs::create_dir_all(&workdir).unwrap();
    let _spawning = spawning();

    grade(bundle, submission, &workdir, options, host)
}

fn run(tmp: &Path, submission: &Path, bundle: &Bundle) -> IndexMap<String, Outcome> {
    let python = python();
    let graded = graded_with(
        tmp,
        submission,
        bundle,
        &options(&python),
        &FakeHost::default(),
    );

    graded.unwrap().outcomes
}

fn assert_only_bad_fails(results: &IndexMap<String, Outcome>) -> &Outcome {
    assert_eq!(results.keys().collect::<Vec<_>>(), ALL);

    for name in GOOD {
        assert!(results[name].passed, "{:?}", results[name]);
    }

    let bad = &results["test_q1_bad"];
    assert!(!bad.passed);
    bad
}

/// A temporary folder, and this test's turn to grade.
struct Tmp {
    dir: TempDir,
    _serial: MutexGuard<'static, ()>,
}

impl Tmp {
    fn path(&self) -> &Path {
        self.dir.path()
    }
}

fn tmp() -> Tmp {
    Tmp {
        _serial: SERIAL.lock().unwrap_or_else(|e| e.into_inner()),
        dir: tempfile::tempdir().unwrap(),
    }
}

// Misbehaving submissions cost only their own test

fn misbehaving_test_fails_alone(name: &str, reason: FailureReason, detail: &str) {
    let tmp = tmp();
    let start = Instant::now();

    let results = run(tmp.path(), &fixture(name), &bundle(usual()));
    let bad = assert_only_bad_fails(&results);

    assert_eq!(bad.reason, Some(reason), "{bad:?}");
    assert!(bad.message.contains(detail), "{bad:?}");
    assert!(start.elapsed() < Duration::from_secs(15));
}

#[test]
fn infinite_loop_fails_alone() {
    misbehaving_test_fails_alone("infinite_loop", Timeout, "longer than 1 s");
}

#[test]
fn c_loop_fails_alone() {
    misbehaving_test_fails_alone("c_loop", Timeout, "longer than 1 s");
}

#[test]
fn segfault_fails_alone() {
    misbehaving_test_fails_alone("segfault", Killed, "SIGSEGV");
}

#[test]
fn exit_fails_alone() {
    misbehaving_test_fails_alone("exits", Killed, "exited with code 3");
}

// macOS rejects RLIMIT_AS, so there the bomb would take the machine's memory.
#[cfg(target_os = "linux")]
#[test]
fn memory_bomb_fails_alone() {
    misbehaving_test_fails_alone("memory_bomb", OutOfMemory, "MemoryError");
}

#[test]
fn stdout_flood_finishes_and_keeps_only_the_tail() {
    let tmp = tmp();
    let python = python();
    let host = FakeHost::default();
    let start = Instant::now();

    let graded = graded_with(
        tmp.path(),
        &fixture("stdout_flood"),
        &bundle(usual()),
        &options(&python),
        &host,
    );
    let results = graded.unwrap().outcomes;

    assert!(start.elapsed() < Duration::from_secs(30));
    assert_eq!(assert_only_bad_fails(&results).reason, Some(Assertion));

    let logs = host.logs.lock().unwrap();
    let [(Level::Info, message)] = &logs[..] else {
        panic!("one log message: {logs:?}");
    };
    let (_, output) = message.split_once(":\n").unwrap();
    assert!(output.ends_with(&format!("{}\n", "x".repeat(1000))));
    assert_eq!(output.chars().count(), OUTPUT_LIMIT);
}

/// Kills the process whose pid the file holds, when dropped, even after a failed assertion.
struct KillOnDrop(PathBuf);

impl Drop for KillOnDrop {
    fn drop(&mut self) {
        let Ok(pid) = fs::read_to_string(&self.0) else {
            return;
        };

        if let Some(pid) = pid.trim().parse().ok().and_then(Pid::from_raw) {
            let _ = rustix::process::kill_process(pid, Signal::KILL);
        }
    }
}

#[test]
fn daemonizer_does_not_hang() {
    let tmp = tmp();
    let _grandchild = KillOnDrop(tmp.path().join("work/public/grandchild.pid"));
    let start = Instant::now();

    let results = run(tmp.path(), &fixture("daemonizer"), &bundle(usual()));

    assert!(start.elapsed() < Duration::from_secs(11));
    assert_eq!(assert_only_bad_fails(&results).reason, Some(Assertion));
}

#[test]
fn printed_events_do_not_flip_results() {
    let tmp = tmp();

    let results = run(tmp.path(), &fixture("forge_print"), &bundle(usual()));

    assert_eq!(assert_only_bad_fails(&results).reason, Some(Assertion));
}

#[test]
fn parent_keeps_first_result_and_ignores_unstarted_tests() {
    let source = r#"import json, os, sys

def send(name, passed):
    result = {"schema": 1, "event": "result", "name": name, "passed": passed}
    result |= {"message": "forged", "reason": None if passed else "error", "seconds": 0.0}
    os.write(int(sys.argv[1]), (json.dumps(result) + "\n").encode())

def test_q1_before():
    pass

def test_q1_bad():
    send("test_q1_before", False)
    send("test_q1_after", False)
    send("test_q2_other", False)

def test_q1_after():
    pass
"#;
    let cells = vec![
        cell("q1", source, false, &[]),
        cell("q2", Q2, false, ADD_AND_MISBEHAVE),
    ];
    let tmp = tmp();

    let results = run(tmp.path(), &fixture("infinite_loop"), &bundle(cells));

    assert!(
        results.values().all(|outcome| outcome.passed),
        "{results:?}"
    );
}

// Deadlines, crashes outside tests, and limits on restarts

#[test]
fn budget_exhaustion_marks_the_rest_budget() {
    // 16 tests of 0.6 s, but a 6 s budget: room for a slow child start, and still the budget
    // runs out during the run.
    let source: String = std::iter::once("import time\n\n".to_string())
        .chain((0..16).map(|i| format!("def test_q1_s{i}():\n    time.sleep(0.6)\n\n")))
        .collect();
    let grading = bundle_with(vec![cell("q1", &source, false, &[])], 2.0, 6.0);
    let tmp = tmp();
    let start = Instant::now();

    let results = run(tmp.path(), &fixture("infinite_loop"), &grading);

    assert!(start.elapsed() < Duration::from_secs_f64(6.0 + 2.0));
    let names: Vec<String> = (0..16).map(|i| format!("test_q1_s{i}")).collect();
    assert_eq!(results.keys().cloned().collect::<Vec<_>>(), names);
    assert!(results["test_q1_s0"].passed);
    assert_eq!(results["test_q1_s15"].reason, Some(Budget));

    let outcomes: Vec<&Outcome> = results.values().collect();
    let first = outcomes
        .iter()
        .position(|o| o.reason == Some(Budget))
        .unwrap();
    assert!(outcomes[..first].iter().all(|o| o.reason.is_none()));
    assert!(outcomes[first..].iter().all(|o| o.reason == Some(Budget)));

    // The budget ran out during a test: that one was stopped, the rest never started.
    assert!(
        outcomes[first].message.starts_with("stopped:"),
        "{:?}",
        outcomes[first]
    );
    assert!(
        outcomes[first + 1..]
            .iter()
            .all(|o| o.message.starts_with("not run:"))
    );
}

fn crash_while_loading_a_cell_fails_its_tests(name: &str, reason: FailureReason, detail: &str) {
    let source = format!("_value = misbehave()\n\n{Q1}");
    let cells = vec![
        cell("q1", &source, false, ADD_AND_MISBEHAVE),
        cell("q2", Q2, false, ADD_AND_MISBEHAVE),
    ];
    let tmp = tmp();

    let results = run(tmp.path(), &fixture(name), &bundle(cells));

    assert!(
        results["test_q2_other"].passed,
        "{:?}",
        results["test_q2_other"]
    );

    for test in ["test_q1_before", "test_q1_bad", "test_q1_after"] {
        let outcome = &results[test];
        assert_eq!(outcome.reason, Some(reason), "{outcome:?}");
        assert!(
            outcome.message.contains("loading the test cell"),
            "{outcome:?}"
        );
        assert!(outcome.message.contains(detail), "{outcome:?}");
    }
}

#[test]
fn segfault_while_loading_a_cell_fails_its_tests() {
    crash_while_loading_a_cell_fails_its_tests("segfault", Killed, "SIGSEGV");
}

#[test]
fn c_loop_while_loading_a_cell_fails_its_tests() {
    crash_while_loading_a_cell_fails_its_tests("c_loop", Timeout, "longer than 1 s");
}

/// The usual bundle, where q2 needs a name only the whole notebook run defines.
fn needs_run(hidden: bool) -> Bundle {
    bundle(vec![
        cell("q1", Q1, false, &["add", "nothing_here"]),
        cell("q2", NEEDS_RUN, hidden, &["nothing_here"]),
    ])
}

fn failed_notebook_run_costs_only_the_names_that_need_it(body: &str, detail: &str) {
    let tmp = tmp();
    let indented: String = body.lines().map(|line| format!("    {line}\n")).collect();
    let path = notebook(
        tmp.path(),
        &format!("@app.cell\ndef _():\n{indented}    return\n"),
    );
    let start = Instant::now();

    let results = run(tmp.path(), &path, &needs_run(false));

    assert!(start.elapsed() < Duration::from_secs(20));
    assert_eq!(results.keys().collect::<Vec<_>>(), ALL);
    assert!(
        results["test_q1_before"].passed,
        "{:?}",
        results["test_q1_before"]
    );
    assert!(
        results["test_q1_after"].passed,
        "{:?}",
        results["test_q1_after"]
    );
    assert!(!results["test_q1_bad"].passed);

    let other = &results["test_q2_other"];
    assert!(!other.passed);
    assert!(
        other.message.contains("`nothing_here` is unavailable"),
        "{other:?}"
    );
    assert!(
        other
            .message
            .contains("needs the whole notebook to run, which failed"),
        "{other:?}"
    );
    assert!(other.message.contains(detail), "{other:?}");
}

#[test]
fn segfault_in_the_notebook_run_costs_only_the_names_that_need_it() {
    failed_notebook_run_costs_only_the_names_that_need_it(
        "import ctypes\n\nctypes.string_at(0)",
        "SIGSEGV",
    );
}

#[test]
fn exit_in_the_notebook_run_costs_only_the_names_that_need_it() {
    failed_notebook_run_costs_only_the_names_that_need_it(
        "import os\n\nos._exit(3)",
        "exited with code 3",
    );
}

#[test]
fn loop_in_the_notebook_run_costs_only_the_names_that_need_it() {
    failed_notebook_run_costs_only_the_names_that_need_it(
        "while True:\n    pass",
        "longer than 1 s",
    );
}

#[test]
fn crash_while_importing_fails_everything_after_one_retry() {
    let tmp = tmp();
    let path = tmp.path().join("submission.py");
    let source = "import marimo\n\napp = marimo.App()\n\nwith app.setup:\n    import ctypes\n\n    \
                  ctypes.string_at(0)\n";
    fs::write(&path, source).unwrap();
    let start = Instant::now();

    let results = run(tmp.path(), &path, &bundle(usual()));

    assert!(start.elapsed() < Duration::from_secs(10));
    assert_eq!(results.keys().collect::<Vec<_>>(), ALL);

    for outcome in results.values() {
        assert_eq!(outcome.reason, Some(LoadFailed), "{outcome:?}");
        assert!(
            outcome.message.contains("loading your notebook"),
            "{outcome:?}"
        );
        assert!(outcome.message.contains("SIGSEGV"), "{outcome:?}");
    }
}

#[test]
fn slow_check_cell_does_not_stall_the_notebook_run() {
    let tmp = tmp();
    let path = tmp.path().join("submission.py");
    let source = r#"import marimo

app = marimo.App()

with app.setup:
    import time

    import mogs


@app.cell
def _():
    factor = 2
    return (factor,)


@app.cell
def double(factor):
    def double(x):
        return factor * x

    return (double,)


@app.cell
def _():
    def slow():
        time.sleep(30)

    mogs.grade("q1", tests=[slow])
    return
"#;
    fs::write(&path, source).unwrap();
    let test = "def test_q1_double():\n    assert double(2) == 4\n";
    let grading = bundle(vec![cell("q1", test, false, &["double"])]);
    let start = Instant::now();

    let results = run(tmp.path(), &path, &grading);

    assert!(start.elapsed() < Duration::from_secs(15));
    assert!(
        results["test_q1_double"].passed,
        "{:?}",
        results["test_q1_double"]
    );
}

#[test]
fn failed_notebook_run_is_not_retried_in_the_hidden_phase() {
    // An interpreter that keeps each job it reads in `jobs/`, then runs as the real one.
    let tmp = tmp();
    let jobs = tmp.path().join("jobs");
    fs::create_dir(&jobs).unwrap();
    let recording = tmp.path().join("python");
    let script = format!(
        "#!/bin/sh\n\
         job=\"{jobs}/$(($(ls {jobs} | wc -l))).json\"\n\
         cat > \"$job\"\n\
         exec {python} \"$@\" < \"$job\"\n",
        jobs = jobs.display(),
        python = python().display(),
    );
    {
        let _writing = writing_script();
        fs::write(&recording, script).unwrap();
        fs::set_permissions(&recording, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let path = notebook(
        tmp.path(),
        "@app.cell\ndef _():\n    import os\n\n    os._exit(3)\n",
    );
    let graded = graded_with(
        tmp.path(),
        &path,
        &needs_run(true),
        &options(&recording),
        &FakeHost::default(),
    );
    let results = graded.unwrap().outcomes;

    assert!(results["test_q1_before"].passed);
    assert!(
        results["test_q2_other"]
            .message
            .contains("exited with code 3")
    );
    let count = fs::read_dir(&jobs).unwrap().count();
    let run_failed: Vec<serde_json::Value> = (0..count)
        .map(|i| {
            let job = fs::read_to_string(jobs.join(format!("{i}.json"))).unwrap();
            serde_json::from_str::<serde_json::Value>(&job).unwrap()["run_failed"].clone()
        })
        .collect();
    assert_eq!(
        run_failed,
        [
            serde_json::Value::Null,
            "exited with code 3".into(),
            "exited with code 3".into()
        ]
    );
}

#[test]
fn respawns_are_capped() {
    let source: String = (0..6)
        .map(|i| format!("def test_q1_c{i}():\n    misbehave()\n\n"))
        .collect();
    let tmp = tmp();

    let results = run(
        tmp.path(),
        &fixture("segfault"),
        &bundle(vec![cell("q1", &source, false, ADD_AND_MISBEHAVE)]),
    );

    let reasons: Vec<_> = results.values().map(|outcome| outcome.reason).collect();
    assert_eq!(
        reasons,
        [Some(Killed); 4]
            .into_iter()
            .chain([Some(NotRun); 2])
            .collect::<Vec<_>>()
    );
}

// Two phases

#[test]
fn grade_runs_both_phases_in_fresh_workdirs() {
    let tmp = tmp();
    let source = tmp.path().join("source");
    fs::create_dir_all(source.join("data")).unwrap();
    fs::write(source.join("data/x.txt"), "42").unwrap();
    let path = notebook(tmp.path(), "");
    let public = r#"from pathlib import Path

def test_q1_public():
    Path("public_was_here").touch()
    assert Path("data/x.txt").read_text() == "42"
    assert Path("submission.py").exists()
"#;
    let hidden = r#"# HIDDEN
from pathlib import Path

def test_q2_hidden():
    assert not Path("public_was_here").exists()
    assert Path("data/x.txt").read_text() == "42"
"#;
    let grading = bundle(vec![
        cell("q1", public, false, &[]),
        cell("q2", hidden, true, &[]),
    ]);
    let python = python();
    let data = data_files(&source, &["data/x.txt".to_string()]).unwrap();
    let options = GradeOptions {
        data: &data,
        ..options(&python)
    };

    let results = graded_with(tmp.path(), &path, &grading, &options, &FakeHost::default())
        .unwrap()
        .outcomes;

    assert_eq!(
        results.keys().collect::<Vec<_>>(),
        ["test_q1_public", "test_q2_hidden"]
    );
    assert!(
        results.values().all(|outcome| outcome.passed),
        "{results:?}"
    );
    assert!(tmp.path().join("work/public/public_was_here").exists());
    assert!(!tmp.path().join("work/hidden/public_was_here").exists());
}

#[test]
fn grade_reports_why_the_notebook_did_not_load() {
    let tmp = tmp();
    let path = tmp.path().join("submission.py");
    fs::write(&path, "def broken(:\n").unwrap();
    let python = python();

    let graded = graded_with(
        tmp.path(),
        &path,
        &bundle(usual()),
        &options(&python),
        &FakeHost::default(),
    )
    .unwrap();

    let load_error = graded.load_error.expect("a load error");
    assert!(load_error.contains("failed to import"), "{load_error}");
    assert!(
        graded
            .outcomes
            .values()
            .all(|o| o.reason == Some(LoadFailed))
    );
}

#[test]
fn grade_returns_outcomes_and_no_load_error() {
    let tmp = tmp();
    let python = python();

    let graded = graded_with(
        tmp.path(),
        &notebook(tmp.path(), ""),
        &bundle(usual()),
        &options(&python),
        &FakeHost::default(),
    )
    .unwrap();

    assert_eq!(graded.load_error, None);
    assert_eq!(graded.outcomes.keys().collect::<Vec<_>>(), ALL);
    let before = &graded.outcomes["test_q1_before"];
    assert_eq!(
        (before.passed, before.message.as_str(), before.reason),
        (true, "", None)
    );
    assert!(graded.outcomes["test_q1_bad"].reason.is_some());
}

#[test]
fn failing_hidden_helper_is_not_a_notebook_load_failure() {
    let helper = HiddenHelper {
        source: "EXPECTED = missing()\n".into(),
        defines: vec!["EXPECTED".into()],
        uses: vec!["missing".into()],
        needs: Vec::new(),
    };
    let mut hidden = cell(
        "q1",
        "# HIDDEN\ndef test_q1_hidden():\n    assert EXPECTED\n",
        true,
        ADD_AND_MISBEHAVE,
    );
    hidden.needs = vec!["EXPECTED".into()];
    let mut grading = bundle(vec![hidden]);
    grading.helpers = vec![helper];
    let tmp = tmp();
    let python = python();

    let graded = graded_with(
        tmp.path(),
        &notebook(tmp.path(), ""),
        &grading,
        &options(&python),
        &FakeHost::default(),
    )
    .unwrap();

    assert_eq!(graded.outcomes["test_q1_hidden"].reason, Some(LoadFailed));
    assert_eq!(graded.load_error, None);
}

#[test]
fn grade_leaves_junk_out_of_data_folders() {
    let tmp = tmp();
    let source = tmp.path().join("source");
    for name in [
        "data/x.txt",
        "data/__pycache__/m.pyc",
        "data/__marimo__/c",
        "data/.DS_Store",
    ] {
        fs::create_dir_all(source.join(name).parent().unwrap()).unwrap();
        fs::write(source.join(name), "42").unwrap();
    }
    let test = r#"from pathlib import Path

def test_q1_tree():
    found = sorted(str(p) for p in Path("data").rglob("*") if p.is_file())
    assert found == ["data/x.txt"], found
"#;
    let grading = bundle(vec![cell("q1", test, false, &[])]);
    let python = python();
    let data = data_files(&source, &["data".to_string()]).unwrap();
    let options = GradeOptions {
        data: &data,
        ..options(&python)
    };

    let results = graded_with(
        tmp.path(),
        &notebook(tmp.path(), ""),
        &grading,
        &options,
        &FakeHost::default(),
    )
    .unwrap()
    .outcomes;

    assert!(results["test_q1_tree"].passed, "{results:?}");
}

#[test]
fn grade_rejects_a_symlinked_submission() {
    let tmp = tmp();
    let link = tmp.path().join("link.py");
    symlink(notebook(tmp.path(), ""), &link).unwrap();
    let python = python();

    let graded = graded_with(
        tmp.path(),
        &link,
        &bundle(usual()),
        &options(&python),
        &FakeHost::default(),
    );

    match graded {
        Err(GradeError::Value(message)) => {
            let message = message.to_string_lossy();
            assert!(message.contains("regular file"), "{message}");
        }
        other => panic!("a ValueError: {other:?}"),
    }
}

#[test]
fn infinite_budget_means_no_overall_deadline() {
    // `mogs.assignment(budget=1e999)` reads as infinity.
    let grading = bundle_with(usual(), 1.0, f64::INFINITY);
    let tmp = tmp();
    let body = "def misbehave():\n    return True\n";

    let results = run(tmp.path(), &notebook(tmp.path(), body), &grading);

    assert!(
        results.values().all(|outcome| outcome.passed),
        "{results:?}"
    );
}

#[test]
fn infinite_timeout_reaches_the_child() {
    // As in v0.7, the child cannot arm an infinite timer, so each cell fails to load.
    let grading = bundle_with(usual(), f64::INFINITY, 60.0);
    let tmp = tmp();

    let results = run(tmp.path(), &notebook(tmp.path(), ""), &grading);

    assert_eq!(results.keys().collect::<Vec<_>>(), ALL);

    for outcome in results.values() {
        assert_eq!(outcome.reason, Some(LoadFailed), "{outcome:?}");
        let expected = "the test cell failed to load (OverflowError";
        assert!(outcome.message.contains(expected), "{outcome:?}");
    }
}

#[test]
fn grade_shares_the_budget() {
    let slow = |qid: &str, seconds: f64| {
        format!("import time\n\ndef test_{qid}_slow():\n    time.sleep({seconds})\n")
    };
    // The public phase, a slow start included, fits the 6 s budget; the hidden test does not.
    let cells = vec![
        cell("q1", &slow("q1", 0.2), false, &[]),
        cell("q2", &format!("# HIDDEN\n{}", slow("q2", 30.0)), true, &[]),
    ];
    let tmp = tmp();

    let results = run(
        tmp.path(),
        &notebook(tmp.path(), ""),
        &bundle_with(cells, 10.0, 6.0),
    );

    assert!(results["test_q1_slow"].passed);
    assert_eq!(results["test_q2_slow"].reason, Some(Budget));
}

// Process setup

#[test]
fn root_and_own_user_are_refused() {
    for (user, expected) in [("root", "root"), ("me", "user running the grader")] {
        if rustix::process::getuid().is_root() && user == "me" {
            continue;
        }

        let tmp = tmp();
        let python = python();
        let options = GradeOptions {
            user: Some(user),
            ..options(&python)
        };

        let graded = graded_with(
            tmp.path(),
            &notebook(tmp.path(), ""),
            &bundle(usual()),
            &options,
            &FakeHost::default(),
        );

        match graded {
            Err(GradeError::Value(message)) => {
                let message = message.to_string_lossy();
                assert!(message.contains(expected), "{message}");
            }
            other => panic!("a ValueError: {other:?}"),
        }
        assert_eq!(fs::read_dir(tmp.path().join("work")).unwrap().count(), 0);
    }
}

// Hostile event lines

#[test]
fn events_before_a_bad_line_still_count() {
    let source = r#"import os, sys
from mogs.grader.protocol import Result, encode
from mogs.runner import Outcome

def test_q1_before():
    assert add(1, 2) == 3

def test_q1_bad():
    forged = encode(Result(Outcome("test_q1_bad", False, "forged")))
    os.write(int(sys.argv[1]), (forged + "\nnot json\n").encode())
    import time
    time.sleep(5)

def test_q1_after():
    assert add(2, 2) == 4
"#;
    let cells = vec![
        cell("q1", source, false, ADD_AND_MISBEHAVE),
        cell("q2", Q2, false, ADD_AND_MISBEHAVE),
    ];
    let tmp = tmp();

    let results = run(tmp.path(), &fixture("infinite_loop"), &bundle(cells));

    assert_eq!(assert_only_bad_fails(&results).message, "forged");
}

#[test]
fn bad_event_line_fails_only_the_writer() {
    for data in [
        r#"b"not json\n""#,
        r#"b"[" * 200_000 + b"]" * 200_000 + b"\n""#,
        r#"b"\xff\xfe not utf-8\n""#,
        r#"b"x" * (2 * 1024 * 1024) + b"\n""#,
        r#"b"x" * (2 * 1024 * 1024)"#,
    ] {
        let body = format!(
            "import os, sys\nos.write(int(sys.argv[1]), {data})\nimport time\ntime.sleep(5)\n"
        );
        let tmp = tmp();

        let results = run(
            tmp.path(),
            &fixture("infinite_loop"),
            &bundle(hostile(&body)),
        );

        let bad = assert_only_bad_fails(&results);
        assert!(bad.message.contains("malformed event"), "{data}: {bad:?}");
    }
}

// Hostile children

#[test]
fn output_that_is_not_utf8_is_kept() {
    let body = "import sys\nsys.stdout.buffer.write(b'\\xff\\xfe tail')\nsys.stdout.flush()\n\
                assert False\n";
    let tmp = tmp();
    let python = python();
    let host = FakeHost::default();

    let results = graded_with(
        tmp.path(),
        &fixture("infinite_loop"),
        &bundle(hostile(body)),
        &options(&python),
        &host,
    )
    .unwrap()
    .outcomes;

    assert_eq!(assert_only_bad_fails(&results).reason, Some(Assertion));
    let logs = host.logs.lock().unwrap();
    let [(Level::Info, message)] = &logs[..] else {
        panic!("one log message: {logs:?}");
    };
    assert!(message.contains("\u{fffd}\u{fffd} tail"), "{message}");
}

#[test]
fn child_that_closes_its_event_channel_fails_alone() {
    let body = "import os, sys\nos.close(int(sys.argv[1]))\n";
    let tmp = tmp();

    let results = run(
        tmp.path(),
        &fixture("infinite_loop"),
        &bundle(hostile(body)),
    );

    assert_eq!(assert_only_bad_fails(&results).reason, Some(Killed));
}

#[test]
fn child_that_ignores_sigterm_is_killed() {
    let body = "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True:\n    \
                pass\n";
    let tmp = tmp();
    let start = Instant::now();

    let results = run(
        tmp.path(),
        &fixture("infinite_loop"),
        &bundle(hostile(body)),
    );

    assert!(start.elapsed() < Duration::from_secs(15));
    assert_eq!(assert_only_bad_fails(&results).reason, Some(Timeout));
}

#[test]
fn processes_a_test_forks_are_killed_with_it() {
    // A bounded fork storm: without a grading user there is no process cap.
    let body = r#"import os, time
pids = []
for _ in range(100):
    pid = os.fork()
    if pid == 0:
        time.sleep(60)
        os._exit(0)
    pids.append(pid)
with open("forked.txt", "w") as f:
    f.write(" ".join(map(str, pids)))
while True:
    pass
"#;
    let tmp = tmp();

    let results = run(
        tmp.path(),
        &fixture("infinite_loop"),
        &bundle(hostile(body)),
    );

    assert_eq!(assert_only_bad_fails(&results).reason, Some(Timeout));
    let forked = fs::read_to_string(tmp.path().join("work/public/forked.txt")).unwrap();
    let pids: Vec<Pid> = forked
        .split_whitespace()
        .map(|pid| Pid::from_raw(pid.parse().unwrap()).unwrap())
        .collect();
    assert_eq!(pids.len(), 100);

    // Killed processes vanish once reaped. macOS reaps the orphans through launchd, which can
    // take a moment, so wait a little before calling a process a survivor.
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut alive = pids;

    while !alive.is_empty() && Instant::now() < deadline {
        alive.retain(|pid| rustix::process::test_kill_process(*pid).is_ok());
        std::thread::sleep(Duration::from_millis(100));
    }

    assert!(alive.is_empty(), "{alive:?}");
}

#[test]
fn child_gets_no_inherited_descriptors() {
    let tmp = tmp();
    // A high number, which the child's own files do not reach.
    let opened = fs::File::open(tmp.path()).unwrap();
    let file = rustix::io::fcntl_dupfd_cloexec(&opened, 900).unwrap();
    rustix::io::fcntl_setfd(&file, rustix::io::FdFlags::empty()).unwrap();
    let fd = std::os::fd::AsRawFd::as_raw_fd(&file);
    let body = format!(
        "import os\ntry:\n    os.fstat({fd})\nexcept OSError:\n    pass\nelse:\n    \
         raise AssertionError(\"descriptor {fd} reached the child\")\n"
    );

    let results = run(
        tmp.path(),
        &notebook(tmp.path(), ""),
        &bundle(hostile(&body)),
    );
    drop(file);

    assert!(
        results["test_q1_bad"].passed,
        "{:?}",
        results["test_q1_bad"]
    );
}

// Errors

#[test]
fn grade_errors_name_the_path_or_value() {
    let tmp = tmp();
    let root = tmp.path().join("work");
    fs::create_dir_all(root.join("public")).unwrap();
    let python = python();
    let missing = tmp.path().join("missing.py");
    let submission = notebook(tmp.path(), "");
    let host = FakeHost::default();
    let _spawning = spawning();
    let grading = bundle(usual());

    match grade(&grading, &missing, &root, &options(&python), &host) {
        Err(GradeError::Os { error, path }) => {
            assert_eq!(error.kind(), std::io::ErrorKind::NotFound);
            assert_eq!(path, Some(missing));
        }
        other => panic!("an OSError: {other:?}"),
    }

    match grade(&grading, &submission, &root, &options(&python), &host) {
        Err(GradeError::Os { error, path }) => {
            assert_eq!(error.kind(), std::io::ErrorKind::AlreadyExists);
            assert_eq!(path, Some(root.join("public")));
        }
        other => panic!("an OSError: {other:?}"),
    }

    let options = GradeOptions {
        user: Some("no-such-mogs-user"),
        ..options(&python)
    };
    match grade(&grading, &submission, &root, &options, &host) {
        Err(GradeError::Key(name)) => assert_eq!(name, "no-such-mogs-user"),
        other => panic!("a KeyError: {other:?}"),
    }
}
