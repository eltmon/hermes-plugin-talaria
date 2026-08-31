"""ClientOrchestrationCommand builders. Pure dicts; no HTTP."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from .errors import TalariaError

# T3's ThreadTurnStartCommand decode-default is full-access. Always send a
# mode; the plugin default is approval-required, never full-access.
RUNTIME_MODES = (
    "approval-required",
    "auto-accept-edits",
    "auto",
    "full-access",
)
DEFAULT_RUNTIME_MODE = "approval-required"
INTERACTION_MODES = ("default", "plan")
DEFAULT_INTERACTION_MODE = "default"
APPROVAL_DECISIONS = (
    "accept",
    "acceptForSession",
    "acceptAlways",
    "decline",
    "cancel",
)
RESPOND_KINDS = ("approval", "user-input")


def new_id() -> str:
    return str(uuid4())


def now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def thread_create(args: Mapping[str, Any]) -> dict[str, Any]:
    model = _model_selection(args.get("model_selection"), required=True)
    return {
        "type": "thread.create",
        "commandId": new_id(),
        "threadId": new_id(),
        "projectId": _required_str(
            args.get("project_id"),
            "project_id",
            hint="pass project_id from t3_list",
        ),
        "title": _required_str(args.get("title"), "title"),
        "modelSelection": model,
        "runtimeMode": _runtime_mode(args.get("runtime_mode")),
        "interactionMode": _interaction_mode(args.get("interaction_mode")),
        "branch": None,
        "worktreePath": None,
        "createdAt": now_iso(),
    }


def thread_turn_start(args: Mapping[str, Any]) -> dict[str, Any]:
    text = args.get("text")
    if not isinstance(text, str):
        raise TalariaError("text is required", "pass the user prompt as text")
    command: dict[str, Any] = {
        "type": "thread.turn.start",
        "commandId": new_id(),
        "threadId": _required_str(
            args.get("thread_id"),
            "thread_id",
            hint="pass thread_id from t3_list or t3_new_thread",
        ),
        "message": {
            "messageId": new_id(),
            "role": "user",
            "text": text,
            "attachments": [],
        },
    }
    model = _model_selection(args.get("model_selection"), required=False)
    if model is not None:
        command["modelSelection"] = model
    command["runtimeMode"] = _runtime_mode(args.get("runtime_mode"))
    command["interactionMode"] = _interaction_mode(args.get("interaction_mode"))
    command["createdAt"] = now_iso()
    return command


def thread_turn_interrupt(args: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "thread.turn.interrupt",
        "commandId": new_id(),
        "threadId": _required_str(
            args.get("thread_id"),
            "thread_id",
            hint="pass thread_id from t3_list or t3_new_thread",
        ),
        "createdAt": now_iso(),
    }


def thread_respond(args: Mapping[str, Any]) -> dict[str, Any]:
    kind = _required_str(
        args.get("kind"),
        "kind",
        hint="kind=approval or kind=user-input",
    )
    if kind == "approval":
        return thread_approval_respond(args)
    if kind == "user-input":
        return thread_user_input_respond(args)
    raise TalariaError(
        f"unknown kind {kind!r}",
        "kind must be approval or user-input",
    )


def thread_approval_respond(args: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "thread.approval.respond",
        "commandId": new_id(),
        "threadId": _required_str(
            args.get("thread_id"),
            "thread_id",
            hint="pass thread_id from t3_list or t3_new_thread",
        ),
        "requestId": _required_str(
            args.get("request_id"),
            "request_id",
            hint="pass request_id from the pending approval",
        ),
        "decision": _approval_decision(args.get("decision")),
        "createdAt": now_iso(),
    }


def thread_user_input_respond(args: Mapping[str, Any]) -> dict[str, Any]:
    answers = args.get("answers")
    if not isinstance(answers, dict):
        raise TalariaError(
            "answers is required",
            "pass answers as an object of question id → value for kind=user-input",
        )
    return {
        "type": "thread.user-input.respond",
        "commandId": new_id(),
        "threadId": _required_str(
            args.get("thread_id"),
            "thread_id",
            hint="pass thread_id from t3_list or t3_new_thread",
        ),
        "requestId": _required_str(
            args.get("request_id"),
            "request_id",
            hint="pass request_id from the pending user-input request",
        ),
        "answers": answers,
        "createdAt": now_iso(),
    }


def _required_str(raw: Any, name: str, *, hint: str | None = None) -> str:
    if not isinstance(raw, str):
        raise TalariaError(f"{name} is required", hint or f"pass {name}")
    value = raw.strip()
    if not value:
        raise TalariaError(f"{name} is required", hint or f"pass {name}")
    return value


def _literal(
    raw: Any,
    *,
    name: str,
    allowed: tuple[str, ...],
    default: str | None,
) -> str:
    if raw is None:
        if default is not None:
            return default
        raise TalariaError(f"{name} is required", _literal_hint(name, allowed))
    if not isinstance(raw, str):
        raise TalariaError(
            f"unknown {name} {raw!r}",
            _literal_hint(name, allowed),
        )
    value = raw.strip()
    if not value:
        if default is not None:
            return default
        raise TalariaError(f"{name} is required", _literal_hint(name, allowed))
    if value not in allowed:
        raise TalariaError(
            f"unknown {name} {value!r}",
            _literal_hint(name, allowed),
        )
    return value


def _literal_hint(name: str, allowed: tuple[str, ...]) -> str:
    return f"{name} must be one of: {', '.join(allowed)}"


def _runtime_mode(raw: Any) -> str:
    return _literal(
        raw,
        name="runtime_mode",
        allowed=RUNTIME_MODES,
        default=DEFAULT_RUNTIME_MODE,
    )


def _interaction_mode(raw: Any) -> str:
    return _literal(
        raw,
        name="interaction_mode",
        allowed=INTERACTION_MODES,
        default=DEFAULT_INTERACTION_MODE,
    )


def _approval_decision(raw: Any) -> str:
    return _literal(
        raw,
        name="decision",
        allowed=APPROVAL_DECISIONS,
        default=None,
    )


def _model_selection(raw: Any, *, required: bool) -> dict[str, str] | None:
    if raw is None:
        if required:
            raise TalariaError(
                "model_selection is required",
                "pass model_selection {instance_id, model}",
            )
        return None
    if not isinstance(raw, dict):
        raise TalariaError(
            "model_selection must be an object",
            "pass model_selection {instance_id, model}",
        )
    instance_id = raw.get("instance_id", raw.get("instanceId"))
    model = raw.get("model")
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise TalariaError(
            "model_selection.instance_id is required",
            "pass model_selection {instance_id, model}",
        )
    if not isinstance(model, str) or not model.strip():
        raise TalariaError(
            "model_selection.model is required",
            "pass model_selection {instance_id, model}",
        )
    return {"instanceId": instance_id.strip(), "model": model.strip()}
