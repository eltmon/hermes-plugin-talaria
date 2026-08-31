"""Tool handlers. Contract: ``(args: dict, **kwargs) -> str``, JSON, never raise."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from .commands import (
    thread_create,
    thread_respond,
    thread_turn_interrupt,
    thread_turn_start,
)
from .config import (
    EnvironmentRef,
    get_secret,
    resolve_environment,
    resolve_environments,
    secret_name_for,
)
from .errors import EnvironmentNotFound, NotAuthenticated, TalariaError, json_error
from .t3_env import T3EnvClient

OUTPUT_CHAR_LIMIT = 32_768
DEFAULT_TURN_LIMIT = 5

_HINT = "This tool is a scaffold stub; a later work item implements it."

# Hermes dispatch is handler(args, **kwargs) and does not pass PluginContext.
# register() should call bind_ctx(ctx). Tests may pass ctx= instead.
_bound_ctx = None
_client_factory = None


def bind_ctx(ctx) -> None:
    global _bound_ctx
    _bound_ctx = ctx


def set_client_factory(factory) -> None:
    """Test seam: replace T3EnvClient construction (MockTransport). ``None`` restores."""
    global _client_factory
    _client_factory = factory


def make_env_client(
    ref: EnvironmentRef,
    headers_fn: Callable[[], Mapping[str, str]] | None,
) -> T3EnvClient:
    factory = _client_factory
    if factory is not None:
        return factory(ref, headers_fn)
    return T3EnvClient(ref.base_url, headers_fn)


def _stub(name: str) -> str:
    return json_error("not implemented", f"{name}: {_HINT}")


def _ctx(kwargs):
    ctx = kwargs.get("ctx")
    if ctx is not None:
        return ctx
    return _bound_ctx


def _ready_ctx(kwargs):
    # Scaffold FakeCtx has no get_config; keep the stub so WI-1 stub tests hold.
    ctx = _ctx(kwargs)
    if ctx is None or not callable(getattr(ctx, "get_config", None)):
        return None
    return ctx


def _unexpected(exc: BaseException) -> str:
    return json.dumps(
        {
            "error": f"{type(exc).__name__}: {exc}",
            "hint": (
                "unexpected error in the t3code plugin; "
                "check environment connectivity and retry"
            ),
        }
    )


def _token(ctx, ref: EnvironmentRef) -> str | None:
    store = getattr(ctx, "secrets", None)
    if store is not None and not isinstance(store, Mapping):
        store = None
    token = get_secret(secret_name_for(ref.name), store=store)
    if token is None:
        return None
    token = str(token).strip()
    return token or None


def _headers_fn(token: str | None) -> Callable[[], Mapping[str, str]]:
    if not token:
        return lambda: {}
    value = f"Bearer {token}"
    return lambda: {"Authorization": value}


def _latest_turn_status(raw: Any) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    return {"turnId": raw.get("turnId"), "state": raw.get("state")}


def _join_text(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    return left + "\n" + right


def _message_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return str(raw)


def _turns_from_messages(messages: Any) -> list[dict]:
    if not isinstance(messages, list):
        return []
    order: list[Any] = []
    grouped: dict[Any, dict] = {}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        tid = msg.get("turnId")
        if tid not in grouped:
            grouped[tid] = {"turnId": tid, "user": "", "agent_output": ""}
            order.append(tid)
        bucket = grouped[tid]
        role = msg.get("role")
        text = _message_text(msg.get("text"))
        if role == "user":
            bucket["user"] = _join_text(bucket["user"], text)
        elif role == "assistant":
            bucket["agent_output"] = _join_text(bucket["agent_output"], text)
    return [grouped[key] for key in order]


def _condense_project(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    return {
        "id": raw.get("id"),
        "title": raw.get("title"),
        "path": raw.get("workspaceRoot"),
    }


def _condense_thread_shell(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    return {
        "id": raw.get("id"),
        "projectId": raw.get("projectId"),
        "title": raw.get("title"),
        "worktreePath": raw.get("worktreePath"),
        "latestTurn": _latest_turn_status(raw.get("latestTurn")),
        "hasPendingApprovals": raw.get("hasPendingApprovals"),
        "hasPendingUserInput": raw.get("hasPendingUserInput"),
    }


def _page_from_snapshot(snapshot: dict) -> dict | None:
    page = snapshot.get("page")
    if not isinstance(page, dict):
        return None
    return {
        "beforeCursor": page.get("beforeCursor"),
        "hasMore": page.get("hasMore"),
    }


def _thread_body(snapshot: Any) -> tuple[dict, dict | None]:
    if not isinstance(snapshot, dict):
        return {}, None
    thread = snapshot.get("thread")
    if isinstance(thread, dict):
        return thread, _page_from_snapshot(snapshot)
    return snapshot, _page_from_snapshot(snapshot)


def _dumps(payload: dict) -> str:
    return json.dumps(payload)


def _fit_thread_payload(payload: dict, limit: int = OUTPUT_CHAR_LIMIT) -> dict:
    if len(_dumps(payload)) <= limit:
        return payload
    out = dict(payload)
    out["truncated"] = True
    turns = list(out.get("turns") or [])
    while len(turns) > 1:
        turns.pop(0)
        out["turns"] = turns
        if len(_dumps(out)) <= limit:
            return out
    if not turns:
        return out
    turn = dict(turns[0])
    text = turn.get("agent_output")
    if not isinstance(text, str) or not text:
        out["turns"] = turns
        return out
    lo, hi = 0, len(text)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        turn["agent_output"] = text[:mid]
        out["turns"] = [turn]
        if len(_dumps(out)) <= limit:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    turn["agent_output"] = text[:best]
    out["turns"] = [turn]
    return out


def _turn_limit(args: dict) -> int:
    raw = args.get("turn_limit", DEFAULT_TURN_LIMIT)
    if raw is None:
        return DEFAULT_TURN_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TURN_LIMIT
    if value < 1:
        return DEFAULT_TURN_LIMIT
    return value


def _probe_env(ctx, ref: EnvironmentRef) -> dict:
    token = _token(ctx, ref)
    live = False
    label = None
    env_id = ref.environment_id
    try:
        client = make_env_client(ref, _headers_fn(token))
        desc = client.descriptor()
        live = True
        if isinstance(desc, dict):
            label = desc.get("label")
            if desc.get("environmentId"):
                env_id = desc.get("environmentId")
    except Exception:
        live = False
    return {
        "name": ref.name,
        "environmentId": env_id,
        "label": label,
        "mode": ref.mode,
        "auth": "ok" if token else "expired",
        "live": live,
    }


def handle_t3_environments(args: dict, **kwargs) -> str:
    try:
        ctx = _ctx(kwargs)
        if ctx is None:
            return _stub("t3_environments")
        args = args or {}
        requested = args.get("environment")
        if requested:
            refs = [resolve_environment(ctx, requested)]
        else:
            envs = resolve_environments(ctx)
            if not envs:
                raise EnvironmentNotFound(None, [])
            refs = list(envs.values())
        return json.dumps({"environments": [_probe_env(ctx, ref) for ref in refs]})
    except TalariaError as exc:
        return exc.to_json()
    except Exception as exc:
        return _unexpected(exc)


def handle_t3_list(args: dict, **kwargs) -> str:
    try:
        ctx = _ctx(kwargs)
        if ctx is None:
            return _stub("t3_list")
        args = args or {}
        ref = resolve_environment(ctx, args.get("environment"))
        token = _token(ctx, ref)
        if not token:
            return NotAuthenticated(ref.name).to_json()
        snapshot = make_env_client(ref, _headers_fn(token)).shell()
        if not isinstance(snapshot, dict):
            snapshot = {}
        projects = []
        for raw in snapshot.get("projects") or []:
            row = _condense_project(raw)
            if row is not None:
                projects.append(row)
        threads = []
        for raw in snapshot.get("threads") or []:
            row = _condense_thread_shell(raw)
            if row is not None:
                threads.append(row)
        return json.dumps(
            {
                "environment": ref.name,
                "projects": projects,
                "threads": threads,
            }
        )
    except TalariaError as exc:
        return exc.to_json()
    except Exception as exc:
        return _unexpected(exc)


def handle_t3_thread(args: dict, **kwargs) -> str:
    try:
        ctx = _ctx(kwargs)
        if ctx is None:
            return _stub("t3_thread")
        args = args or {}
        thread_id = args.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.strip():
            return json_error(
                "thread_id is required",
                "pass thread_id from t3_list or t3_new_thread",
            )
        thread_id = thread_id.strip()
        ref = resolve_environment(ctx, args.get("environment"))
        token = _token(ctx, ref)
        if not token:
            return NotAuthenticated(ref.name).to_json()
        before = args.get("before_cursor")
        if before is not None:
            before = str(before)
        snapshot = make_env_client(ref, _headers_fn(token)).thread(
            thread_id,
            turn_limit=_turn_limit(args),
            before_cursor=before,
        )
        thread, page = _thread_body(snapshot)
        payload = {
            "environment": ref.name,
            "id": thread.get("id", thread_id),
            "projectId": thread.get("projectId"),
            "title": thread.get("title"),
            "worktreePath": thread.get("worktreePath"),
            "latestTurn": _latest_turn_status(thread.get("latestTurn")),
            "turns": _turns_from_messages(thread.get("messages")),
        }
        if page is not None:
            payload["page"] = page
        payload = _fit_thread_payload(payload)
        return json.dumps(payload)
    except TalariaError as exc:
        return exc.to_json()
    except Exception as exc:
        return _unexpected(exc)


def _dispatch_command(ctx, args: dict, command: dict) -> str:
    ref = resolve_environment(ctx, args.get("environment"))
    token = _token(ctx, ref)
    if not token:
        return NotAuthenticated(ref.name).to_json()
    result = make_env_client(ref, _headers_fn(token)).dispatch(command)
    payload = {
        "environment": ref.name,
        "type": command["type"],
        "commandId": command["commandId"],
        "threadId": command["threadId"],
    }
    message = command.get("message")
    if isinstance(message, dict) and "messageId" in message:
        payload["messageId"] = message["messageId"]
    if isinstance(result, dict):
        payload.update(result)
    else:
        payload["result"] = result
    return json.dumps(payload)


def handle_t3_new_thread(args: dict, **kwargs) -> str:
    try:
        ctx = _ready_ctx(kwargs)
        if ctx is None:
            return _stub("t3_new_thread")
        args = args or {}
        return _dispatch_command(ctx, args, thread_create(args))
    except TalariaError as exc:
        return exc.to_json()
    except Exception as exc:
        return _unexpected(exc)


def handle_t3_prompt(args: dict, **kwargs) -> str:
    try:
        ctx = _ready_ctx(kwargs)
        if ctx is None:
            return _stub("t3_prompt")
        args = args or {}
        return _dispatch_command(ctx, args, thread_turn_start(args))
    except TalariaError as exc:
        return exc.to_json()
    except Exception as exc:
        return _unexpected(exc)


def handle_t3_interrupt(args: dict, **kwargs) -> str:
    try:
        ctx = _ready_ctx(kwargs)
        if ctx is None:
            return _stub("t3_interrupt")
        args = args or {}
        return _dispatch_command(ctx, args, thread_turn_interrupt(args))
    except TalariaError as exc:
        return exc.to_json()
    except Exception as exc:
        return _unexpected(exc)


def handle_t3_respond(args: dict, **kwargs) -> str:
    try:
        ctx = _ready_ctx(kwargs)
        if ctx is None:
            return _stub("t3_respond")
        args = args or {}
        return _dispatch_command(ctx, args, thread_respond(args))
    except TalariaError as exc:
        return exc.to_json()
    except Exception as exc:
        return _unexpected(exc)


def handle_t3_wait(args: dict, **kwargs) -> str:
    return _stub("t3_wait")
