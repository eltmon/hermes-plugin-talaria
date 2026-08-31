"""JSON error contract for tool handlers.

Handlers never raise; they return json_error(...) strings. Callers that catch
TalariaError can use ``exc.to_json()``.
"""

from __future__ import annotations

import json


def json_error(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint})


def _excerpt(body: object, limit: int = 200) -> str:
    text = " ".join(str(body).split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


class TalariaError(Exception):
    """Plugin error that serializes to the ``{error, hint}`` JSON contract."""

    def __init__(self, error: str, hint: str) -> None:
        super().__init__(error)
        self.error = error
        self.hint = hint

    def to_json(self) -> str:
        return json_error(self.error, self.hint)


class NotAuthenticated(TalariaError):
    def __init__(self, environment: str | None = None) -> None:
        self.environment = environment
        if environment:
            error = f"not authenticated for {environment}"
        else:
            error = "not authenticated"
        hint = (
            "run `hermes t3code login <pairing-url>` to pair this environment"
        )
        super().__init__(error, hint)


class EnvironmentNotFound(TalariaError):
    def __init__(
        self,
        requested: str | None = None,
        available: list[str] | None = None,
    ) -> None:
        self.requested = requested
        self.available = list(available or [])
        listed = ", ".join(self.available) if self.available else "(none)"
        if requested:
            error = f"environment {requested!r} not found"
            hint = (
                f"available environments: {listed}. "
                "pass environment=<name or environmentId>, or set default_environment"
            )
        elif not self.available:
            error = "no environments configured"
            hint = (
                "add a mode-A entry under environments or run "
                "`hermes t3code login <pairing-url>`"
            )
        else:
            error = "multiple environments available"
            hint = (
                f"available environments: {listed}. "
                "pass environment=<name or environmentId>, or set default_environment"
            )
        super().__init__(error, hint)


class T3ApiError(TalariaError):
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self.body = body
        excerpt = _excerpt(body)
        error = f"T3 API error (HTTP {status})"
        if excerpt:
            hint = f"environment response: {excerpt}"
        else:
            hint = (
                "the T3 Code environment returned an error; "
                "check the instance and retry"
            )
        super().__init__(error, hint)
        self.excerpt = excerpt
