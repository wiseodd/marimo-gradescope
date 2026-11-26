import json
from pathlib import Path

import pytest

from mogs.config import AssignmentConfig, Question
from mogs.grader.bundle import GradingBundle, TestCell
from mogs.grader.results import build_results, failure_results, find_submission
from mogs.runner import MAX_BYTES, FailureReason, Outcome

PUBLIC = ("test_q1_a", "test_q1_b")
HIDDEN = ("test_q1_secret_x", "test_q1_secret_y")


def bundle() -> GradingBundle:
    questions = {
        "q1": Question("q1", "Addition", 25),
        "q2": Question("q2", "Essay", 5, manual=True),
    }
    cells = (
        TestCell("q1", False, "", PUBLIC, ()),
        TestCell("q1", True, "", HIDDEN, ()),
    )
    return GradingBundle(AssignmentConfig("A", "hw.py", questions), "", cells)


def outcomes(failing: str = "test_q1_secret_y", message: str = "SECRET hidden detail"):
    result = {name: Outcome(name, True) for name in PUBLIC + HIDDEN}
    result[failing] = Outcome(failing, False, message, FailureReason.ASSERTION)
    return result


def question_row(results, qid: str):
    [row] = [row for row in results["tests"] if row.get("number") == qid]
    return row


def public_row(results):
    [row] = [row for row in results["tests"] if row["name"] == "Public tests"]
    return row


def shown_to_students(results) -> list:
    rows = [row for row in results["tests"] if row["visibility"] != "hidden"]
    return [results.get("output", ""), rows]


# build_results


def test_three_of_four_tests_give_three_quarters_of_the_points():
    results = build_results(bundle(), outcomes(), [])

    row = question_row(results, "q1")
    assert row["name"] == "Addition"
    assert row["score"] == 18.75
    assert row["max_score"] == 25
    assert row["output_format"] == "md"
    assert results["stdout_visibility"] == "hidden"


def test_score_is_rounded():
    cells = (TestCell("q1", False, "", ("test_q1_a", "test_q1_b", "test_q1_c"), ()),)
    grading = GradingBundle(
        AssignmentConfig("A", "hw.py", {"q1": Question("q1", "Q", 1)}), "", cells
    )
    result = {name: Outcome(name, name == "test_q1_a") for name in cells[0].tests}

    row = question_row(build_results(grading, result, []), "q1")

    assert row["score"] == 0.3333


def test_manual_questions_get_no_row():
    results = build_results(bundle(), outcomes(), [])

    assert [row.get("number") for row in results["tests"]] == [None, "q1"]


def test_question_rows_are_hidden_from_students():
    results = build_results(bundle(), outcomes(), [])

    assert question_row(results, "q1")["visibility"] == "hidden"


def test_hidden_names_and_messages_reach_only_hidden_rows():
    results = build_results(bundle(), outcomes(), [])

    text = json.dumps(shown_to_students(results))
    assert "SECRET" not in text
    assert "secret" not in text
    assert "Hidden" not in text

    output = question_row(results, "q1")["output"]
    assert "✗ `test_q1_secret_y`" in output
    assert "SECRET hidden detail" in output


def test_public_row_is_visible_and_worth_nothing():
    results = build_results(bundle(), outcomes("test_q1_b", "AssertionError: nope"), [])

    row = public_row(results)
    assert row["visibility"] == "visible"
    assert row["score"] == 0
    assert row["max_score"] == 0
    assert "number" not in row
    assert "Addition" in row["output"]
    assert "✓ `test_q1_a`" in row["output"]
    assert "✗ `test_q1_b`" in row["output"]
    assert "AssertionError: nope" in row["output"]


def test_public_row_shows_as_failed_when_a_public_test_fails():
    results = build_results(bundle(), outcomes("test_q1_b"), [])

    assert public_row(results)["status"] == "failed"


def test_public_row_carries_no_status_when_only_hidden_tests_fail():
    results = build_results(bundle(), outcomes("test_q1_secret_y"), [])

    assert "status" not in public_row(results)


def test_question_row_lists_public_and_hidden_tests():
    output = question_row(build_results(bundle(), outcomes(), []), "q1")["output"]

    for name in PUBLIC + HIDDEN:
        assert f"`{name}`" in output


def test_all_passing_tests_are_summed_up():
    passing = {name: Outcome(name, True) for name in PUBLIC + HIDDEN}
    results = build_results(bundle(), passing, [])

    assert "All tests passed" in public_row(results)["output"]
    assert "All tests passed" in question_row(results, "q1")["output"]
    assert "test_q1_a" not in public_row(results)["output"]


def test_summary_shows_the_total_and_each_question_score():
    results = build_results(bundle(), outcomes(), [])

    assert results["output_format"] == "md"
    assert "Total: 18.75 / 25" in results["output"]
    assert "Addition: 18.75 / 25" in results["output"]
    assert "Essay" not in results["output"]


def test_question_with_only_hidden_tests_is_left_out_of_the_public_row():
    questions = {
        "q1": Question("q1", "Addition", 25),
        "q2": Question("q2", "Secret", 5),
    }
    cells = (
        TestCell("q1", False, "", PUBLIC, ()),
        TestCell("q2", True, "", ("test_q2_x",), ()),
    )
    grading = GradingBundle(AssignmentConfig("A", "hw.py", questions), "", cells)
    result = {name: Outcome(name, True) for name in (*PUBLIC, "test_q2_x")}

    results = build_results(grading, result, [])

    assert "Secret" not in public_row(results)["output"]
    assert question_row(results, "q2")["score"] == 5


def test_no_public_tests_means_no_public_row():
    cells = (TestCell("q1", True, "", HIDDEN, ()),)
    grading = GradingBundle(
        AssignmentConfig("A", "hw.py", {"q1": Question("q1", "Q", 1)}), "", cells
    )
    result = {name: Outcome(name, True) for name in HIDDEN}

    results = build_results(grading, result, [])

    assert [row.get("number") for row in results["tests"]] == ["q1"]


def test_message_cannot_close_its_code_fence():
    message = "x\n````\n# Injected heading\n````"
    output = public_row(build_results(bundle(), outcomes("test_q1_b", message), []))["output"]

    fence = "`````"
    assert f"{fence}\n{message}\n{fence}" in output


def test_huge_messages_are_capped():
    message = "y" * 1_000_000
    output = public_row(build_results(bundle(), outcomes("test_q1_b", message), []))["output"]

    assert len(output.encode()) < 2 * MAX_BYTES


def test_notices_come_first_in_a_zero_point_row():
    results = build_results(bundle(), outcomes(), ["No PDF found.", "z" * 1_000_000])

    first = results["tests"][0]
    assert first["name"] == "Submission"
    assert first["score"] == 0
    assert first["max_score"] == 0
    assert "No PDF found." in first["output"]
    assert len(first["output"].encode()) < 3 * MAX_BYTES
    assert results["tests"][1]["name"] == "Public tests"


def test_notice_row_shows_as_failed_and_question_rows_carry_no_status():
    [notice, _, question] = build_results(bundle(), outcomes(), ["No PDF found."])["tests"]

    assert notice["status"] == "failed"
    assert "status" not in question


def test_all_manual_assignment_gets_a_top_level_score():
    questions = {"q1": Question("q1", "Essay", 5, manual=True)}
    manual = GradingBundle(AssignmentConfig("A", "hw.py", questions), "", ())

    results = build_results(manual, {}, [])

    assert results["tests"] == []
    assert results["score"] == 0.0
    assert "output" not in results


def test_graded_assignment_has_no_top_level_score():
    assert "score" not in build_results(bundle(), outcomes(), [])


@pytest.mark.parametrize("grading", [bundle(), None])
def test_failure_rows_show_as_failed(grading):
    rows = failure_results(grading, RuntimeError("boom"))["tests"]

    assert rows
    assert all(row["status"] == "failed" for row in rows)
    assert all(row["visibility"] == "visible" for row in rows)


# find_submission


def touch(root: Path, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")


def test_exact_name_in_a_nested_folder_wins(tmp_path):
    touch(tmp_path, "upload/hw.py", "upload/helper.py", "__MACOSX/upload/._hw.py", "upload/a.pdf")

    found = find_submission(tmp_path, "hw.py")

    assert found.py == tmp_path / "upload" / "hw.py"
    assert found.answers == tmp_path / "upload" / "a.pdf"
    assert found.error is None


def test_single_py_with_another_name_is_accepted(tmp_path):
    touch(tmp_path, "folder/my_homework.py", "__MACOSX/folder/._my_homework.py", ".hidden.py")
    touch(tmp_path, "__pycache__/x.py")

    found = find_submission(tmp_path, "hw.py")

    assert found.py == tmp_path / "folder" / "my_homework.py"
    assert found.answers is None
    assert found.error is None


def test_two_py_files_are_both_named(tmp_path):
    touch(tmp_path, "one.py", "sub/two.py")

    found = find_submission(tmp_path, "hw.py")

    assert found.py is None
    assert found.error is not None
    assert "one.py" in found.error
    assert "two.py" in found.error
    assert "hw.py" in found.error


def test_public_test_files_are_not_notebooks(tmp_path):
    touch(tmp_path, "hw2/student_file.py", "hw2/tests/q1.py", "hw2/tests/q2.py")

    found = find_submission(tmp_path, "hw.py")

    assert found.py == tmp_path / "hw2" / "student_file.py"


def test_notebook_named_like_a_public_test_file_is_found(tmp_path):
    touch(tmp_path, "hw/q1.py", "hw/tests/q1.py")

    found = find_submission(tmp_path, "q1.py")

    assert found.py == tmp_path / "hw" / "q1.py"


def test_assignment_named_tests_is_found(tmp_path):
    touch(tmp_path, "tests/hw.py", "tests/tests/q1.py")

    found = find_submission(tmp_path, "hw.py")

    assert found.py == tmp_path / "tests" / "hw.py"


def test_lone_tests_folder_still_counts(tmp_path):
    touch(tmp_path, "tests/my_homework.py")

    assert find_submission(tmp_path, "hw.py").py == tmp_path / "tests" / "my_homework.py"


def test_two_py_files_are_not_called_notebooks(tmp_path):
    touch(tmp_path, "one.py", "two.py")

    error = find_submission(tmp_path, "hw.py").error or ""

    assert "notebooks" not in error
    assert "No `hw.py`" in error


def test_markdown_answers_are_named_after_the_assignment(tmp_path):
    touch(tmp_path, "hw2.py", "homework2_answers.md")

    assert find_submission(tmp_path, "hw2.py", "homework2").answers == (
        tmp_path / "homework2_answers.md"
    )


def test_no_py_file(tmp_path):
    touch(tmp_path, "notes.txt")

    found = find_submission(tmp_path, "hw.py")

    assert found.py is None
    assert "hw.py" in (found.error or "")


def test_markdown_answers_are_a_fallback(tmp_path):
    touch(tmp_path, "hw.py", "folder/HW_Answers.md")

    assert find_submission(tmp_path, "hw.py").answers == tmp_path / "folder" / "HW_Answers.md"

    touch(tmp_path, "answers.pdf")

    assert find_submission(tmp_path, "hw.py").answers == tmp_path / "answers.pdf"


@pytest.mark.parametrize("name", ["README.md", "answers.md", "hw.md", "other_answers.md"])
def test_other_markdown_is_not_answers(tmp_path, name):
    touch(tmp_path, "hw.py", name)

    assert find_submission(tmp_path, "hw.py").answers is None


def test_symlinks_are_ignored(tmp_path):
    outside = tmp_path / "outside"
    touch(outside, "bundle.json", "real.py", "a.pdf", "dir/inner.py")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "hw.py").symlink_to(outside / "bundle.json")
    (sub / "a.pdf").symlink_to(outside / "a.pdf")
    (sub / "linked").symlink_to(outside / "dir", target_is_directory=True)
    touch(sub, "mine.py")

    found = find_submission(sub, "hw.py")

    assert found.py == sub / "mine.py"
    assert found.answers is None


def test_names_in_messages_cannot_break_markdown(tmp_path):
    touch(tmp_path, "a`` **b**.py", "c.py")

    error = find_submission(tmp_path, "hw.py").error or ""

    assert "``` a`` **b**.py ```" in error


def test_exact_name_twice_is_ambiguous(tmp_path):
    touch(tmp_path, "a/hw.py", "b/hw.py")

    found = find_submission(tmp_path, "hw.py")

    assert found.py is None
    assert "a/hw.py" in (found.error or "")
