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
        ("grade", "grade one submission locally"),
    ]:
        assert any(line.split() == [command, *help.split()] for line in out.splitlines()), out


def test_command_help_shows_its_description(capsys):
    code = main(["grade", "--help"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Usage: mogs grade <DIR> <SUBMISSION>" in out
    assert "and list every failing test, hidden ones included." in out


def test_version(capsys):
    code = main(["--version"])

    assert code == 0
    assert capsys.readouterr().out == f"mogs {version('mogs')}\n"


@pytest.mark.parametrize(
    "argv",
    [[], ["nope"], ["init"], ["edit", "a", "b"], ["grade", "a"], ["build", "--nope", "a"]],
)
def test_usage_error_exits_2(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()

    assert code == 2
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "Usage: mogs" in captured.err
