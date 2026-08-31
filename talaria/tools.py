"""Tool handlers. Contract: ``(args: dict, **kwargs) -> str``, JSON, never raise."""

from __future__ import annotations

from .errors import json_error

_HINT = "This tool is a scaffold stub; a later work item implements it."


def _stub(name: str) -> str:
    return json_error("not implemented", f"{name}: {_HINT}")


def handle_t3_environments(args: dict, **kwargs) -> str:
    return _stub("t3_environments")


def handle_t3_list(args: dict, **kwargs) -> str:
    return _stub("t3_list")


def handle_t3_thread(args: dict, **kwargs) -> str:
    return _stub("t3_thread")


def handle_t3_new_thread(args: dict, **kwargs) -> str:
    return _stub("t3_new_thread")


def handle_t3_prompt(args: dict, **kwargs) -> str:
    return _stub("t3_prompt")


def handle_t3_interrupt(args: dict, **kwargs) -> str:
    return _stub("t3_interrupt")


def handle_t3_respond(args: dict, **kwargs) -> str:
    return _stub("t3_respond")


def handle_t3_wait(args: dict, **kwargs) -> str:
    return _stub("t3_wait")
