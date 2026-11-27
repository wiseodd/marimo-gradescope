import json

import pytest

from mogs import __version__
from mogs._analysis import tool_install_args
from mogs.requirements import RequirementsError


def direct_url(**fields: object) -> str:
    return json.dumps({"url": "file:///src/mogs"} | fields)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, [f"mogs=={__version__}"]),
        (direct_url(dir_info={"editable": True}), ["--editable", "/src/mogs"]),
        (direct_url(dir_info={}), ["/src/mogs"]),
        (
            json.dumps({"url": "file:///w/mogs.whl", "archive_info": {}}),
            ["mogs @ file:///w/mogs.whl"],
        ),
        (
            json.dumps(
                {
                    "url": "https://github.com/o/mogs",
                    "vcs_info": {"vcs": "git", "commit_id": "abc123"},
                }
            ),
            ["mogs @ git+https://github.com/o/mogs@abc123"],
        ),
    ],
)
def test_tool_install_args(text, expected):
    assert tool_install_args(text) == expected


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {"url": "file:///src/my%20project", "dir_info": {"editable": True}},
            ["--editable", "/src/my project"],
        ),
        ({"url": "file:///src/my%20project", "dir_info": {}}, ["/src/my project"]),
        (
            {"url": "file:///src/repo", "dir_info": {}, "subdirectory": "pkg"},
            ["/src/repo/pkg"],
        ),
        (
            {"url": "https://x.test/repo.tar.gz", "archive_info": {}, "subdirectory": "pkg"},
            ["mogs @ https://x.test/repo.tar.gz#subdirectory=pkg"],
        ),
        (
            {
                "url": "https://github.com/o/repo",
                "vcs_info": {"vcs": "git", "commit_id": "abc123"},
                "subdirectory": "pkg",
            },
            ["mogs @ git+https://github.com/o/repo@abc123#subdirectory=pkg"],
        ),
    ],
)
def test_tool_install_args_decodes_paths_and_keeps_subdirectory(fields, expected):
    assert tool_install_args(json.dumps(fields)) == expected


def test_tool_install_args_rejects_unknown_direct_url():
    with pytest.raises(RequirementsError) as raised:
        tool_install_args(json.dumps({"url": "file:///x"}))

    assert str(raised.value) == (
        "Cannot tell where mogs was installed from; its direct_url.json is {'url': 'file:///x'}."
    )
