"""The version of the JSON that crosses between mogs processes.

A grading bundle, a job and an event each carry `"schema": SCHEMA`. The Rust crate
`mogs-schema` defines their formats; `schemas/` in the repo holds their JSON Schemas.
"""

import json
from collections.abc import Mapping

SCHEMA = 1


def check_schema(data: object) -> None:
    """Check that a decoded message is of format version `SCHEMA`.

    Raises:
        ValueError: If `data` has no `schema` key, or another version. The message names both
            versions.
    """

    match data:
        case {"schema": int() as version} if not isinstance(version, bool) and version == SCHEMA:
            return
        case {"schema": version}:
            raise ValueError(f"schema version {json.dumps(version)}, expected {SCHEMA}")

    raise ValueError(f"missing schema version, expected {SCHEMA}")


def reject_extra(extra: Mapping[object, object], what: str) -> None:
    """Fail on the keys a reader's pattern left over, as the Rust reader does.

    Args:
        extra: The keys of a message besides the ones the reader knows.
        what: The kind of message, for the error.

    Raises:
        ValueError: If `extra` holds a key, naming the first one.
    """

    if extra:
        raise ValueError(f"unknown field `{next(iter(extra))}` in {what}")
