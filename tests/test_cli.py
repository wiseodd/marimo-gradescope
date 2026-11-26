from importlib.metadata import entry_points

import pytest

from mogs.cli import main


def test_the_command_is_mogs():
    scripts = {
        script.name: script.value
        for script in entry_points(group="console_scripts")
        if script.value.startswith("mogs.")
    }

    assert scripts == {"mogs": "mogs.cli:main"}


def test_help_names_the_command(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])

    assert capsys.readouterr().out.startswith("usage: mogs ")
