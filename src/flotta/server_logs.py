"""What our servers write to their logs: never a query string's values (FLOTTA-118).

uvicorn logs every request line with its query string, and a WebSocket's too.
Through the door that line held `?access_token=` and Hermes's `?ticket=`, and
Fly kept it. So the door, the control plane and the gateway all run uvicorn with
`log_config()`, which keeps each query's names and replaces its values with `…`.

On the handlers rather than the loggers, because a logger's filter only sees
what is logged on that logger itself, and uvicorn writes the WebSocket line on
`uvicorn.error` and the request line on `uvicorn.access`.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

#: The door's logger (`flotta.door.app`, `flotta.door.turns`).
DOOR_LOG = "flotta.door"

#: A query parameter's value, after its name: up to the next `&`, space, quote or `#`.
_VALUE = re.compile(r'([?&][^=&\s"#]*=)[^&\s"#]*')


def hide_query_values(text: str) -> str:
    """`text` with every query value replaced, and the names left to read."""
    return _VALUE.sub(r"\1…", text) if "?" in text else text


class HideQueryValues(logging.Filter):
    """Rewrites a record's message and its arguments before any formatter sees them.

    The arguments keep their shape: uvicorn's access formatter unpacks five of them.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = hide_query_values(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_hidden(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _hidden(v) for k, v in record.args.items()}
        return True


def _hidden(value: Any) -> Any:
    return hide_query_values(value) if isinstance(value, str) else value


def log_config() -> dict[str, Any]:
    """uvicorn's own logging config, with `HideQueryValues` on every handler."""
    from uvicorn.config import LOGGING_CONFIG

    from flotta.github_app import TIMING_LOG

    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["hide_query_values"] = {"()": HideQueryValues}
    for handler in config["handlers"].values():
        handler.setdefault("filters", []).append("hide_query_values")
    # How long each ask to GitHub took (FLOTTA-180). Only these loggers: turning
    # on all of `flotta` at INFO would change what the gateway writes.
    # And how long `/machine` waited on Fly and on the fleet's image (FLOTTA-182).
    # And the door's own account of a conversation (FLOTTA-218): a turn ended
    # disconnected, an approval settled, a turn that went unrecorded. Without
    # it every one of those INFO lines went nowhere, since uvicorn's config sets
    # up only its own loggers, and an unrecorded turn end left no trace at all.
    for name in (TIMING_LOG, "flotta.control.machine", DOOR_LOG):
        config["loggers"][name] = {"handlers": ["default"], "level": "INFO", "propagate": False}
    return config
