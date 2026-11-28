from importlib.metadata import version

import pytest

from mogs._native import main


def test_help_names_the_command(capsys):
    code = main(["--help"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Usage: mogs " in out
    assert "Build Gradescope autograders from marimo notebooks." in out


def test_help_lists_the_commands(capsys):
    main(["--help"])
    out = capsys.readouterr().out

    for command, help in [
        ("init", "start a new assignment"),
        ("edit", "open the source notebook in marimo's editor"),
        ("build", "build the student notebook and the autograder zip"),
    ]:
        assert any(line.split() == [command, *help.split()] for line in out.splitlines()), out


def test_command_help_shows_its_description(capsys):
    code = main(["build", "--help"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Usage: mogs build <DIR>" in out
    assert "then grade the solved and the blank student copy." in out


def test_grade_is_an_unknown_subcommand(capsys):
    code = main(["grade", "hw", "submission.py"])
    captured = capsys.readouterr()

    assert code == 2
    assert captured.out == ""
    assert captured.err.startswith("error: unrecognized subcommand 'grade'")


def test_version(capsys):
    code = main(["--version"])

    assert code == 0
    assert capsys.readouterr().out == f"mogs {version('mogs')}\n"


@pytest.mark.parametrize(
    "argv",
    [[], ["nope"], ["init"], ["edit", "a", "b"], ["build"], ["build", "--nope", "a"]],
)
def test_usage_error_exits_2(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()

    assert code == 2
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "Usage: mogs" in captured.err


def test_help_hides_the_autograder_command(capsys):
    main(["--help"])
    out = capsys.readouterr().out

    assert "  build " in out
    assert "  autograde " not in out


def test_autograder_command_takes_gradescope_paths(capsys):
    code = main(["autograde", "--help"])
    out = capsys.readouterr().out

    assert code == 0
    for option in ("--source", "--submission", "--results", "--workdir"):
        assert option in out
