"""JSON error contract for tool handlers.

Handlers never raise; they return json_error(...) strings.
WI-2 adds NotAuthenticated, EnvironmentNotFound, T3ApiError.
"""

from __future__ import annotations

import json


def json_error(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint})
