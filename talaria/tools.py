"""Tool handlers. Contract: ``(args: dict, **kwargs) -> str``, JSON, never raise."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from .auth_t3connect import (
    ACCESS_SECRET,
    ConnectAuthError,
    RelayNotAuthenticated,
    environment_status,
    make_dpop_env_client,
    sync_discovered_environments,
    t3connect_enabled,
)
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
from .errors import (
    EnvironmentNotFound,
    NotAuthenticated,
    T3ApiError,
    TalariaError,
    json_error,
)
from .t3_env import T3EnvClient
from .t3_ws import RpcExitFailure, T3WsClient

OUTPUT_CHAR_LIMIT = 32_768
DEFAULT_TURN_LIMIT = 5
DEFAULT_WAIT_INTERVAL = 5
MIN_WAIT_INTERVAL = 2
DEFAULT_WAIT_TIMEOUT = 300
WAIT_EXCERPT_LIMIT = 200
_SETTLED_STATES = frozenset({"completed", "interrupted", "error"})
READ_BYTE_LIMIT = 256 * 1024
SEARCH_LIMIT_DEFAULT = 50
SEARCH_LIMIT_MAX = 500
SEARCH_QUERY_MAX = 256
FILE_PATH_MAX = 512
LS_TAG = "projects.listEntries"
READ_TAG = "projects.readFile"
WRITE_TAG = "projects.writeFile"
SEARCH_TAG = "projects.searchContents"

_HINT = "This tool is a scaffold stub; a later work item implements it."

# Hermes dispatch is handler(args, **kwargs) and does not pass PluginContext.
# register() should call bind_ctx(ctx). Tests may pass ctx= instead.
_bound_ctx = None
_client_factory = None
_ws_client_factory = None


def bind_ctx(ctx) -> None:
    global _bound_ctx
    _bound_ctx = ctx


def set_client_factory(factory) -> None:
    """Test seam: replace T3EnvClient construction (MockTransport). ``None`` restores."""
    global _client_factory
    _client_factory = factory


def set_ws_client_factory(factory) -> None:
    """Test seam: replace T3WsClient construction. ``None`` restores."""
    global _ws_client_factory
    _ws_client_factory = factory


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


def _store(ctx):
    store = getattr(ctx, "secrets", None)
    if store is not None and not isinstance(store, Mapping):
        return None
    return store


def _token(ctx, ref: EnvironmentRef) -> str | None:
    token = get_secret(secret_name_for(ref.name), store=_store(ctx))
    if token is None:
        return None
    token = str(token).strip()
    return token or None


def _clerk_token(ctx) -> str | None:
    token = get_secret(ACCESS_SECRET, store=_store(ctx))
    if token is None:
        return None
    token = str(token).strip()
    return token or None


def _has_auth(ctx, ref: EnvironmentRef) -> bool:
    if _token(ctx, ref):
        return True
    if ref.mode == "t3connect":
        return bool(_clerk_token(ctx))
    return False


def _ensure_discovered(ctx) -> None:
    if not callable(getattr(ctx, "get_config", None)):
        return
    if not t3connect_enabled(ctx) or not _clerk_token(ctx):
        return
    try:
        sync_discovered_environments(ctx, store=_store(ctx))
    except (NotAuthenticated, T3ApiError, ConnectAuthError):
        raise
    except Exception:
        return


def _resolved_env(ctx, name: str | None) -> EnvironmentRef:
    _ensure_discovered(ctx)
    return resolve_environment(ctx, name)


def _client_for(ctx, ref: EnvironmentRef) -> T3EnvClient:
    store = _store(ctx)
    if ref.mode == "t3connect" and _clerk_token(ctx):
        factory = _client_factory
        inner = factory(ref, None) if factory is not None else None
        if inner is not None and not isinstance(inner, T3EnvClient):
            return inner
        return make_dpop_env_client(
            ref, ctx, store, inner=inner if isinstance(inner, T3EnvClient) else None
        )
    token = _token(ctx, ref)
    if not token:
        if ref.mode == "t3connect":
            raise RelayNotAuthenticated(ref.name)
        raise NotAuthenticated(ref.name)
    return make_env_client(ref, _headers_fn(token))


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
    live = False
    label = None
    env_id = ref.environment_id
    if ref.mode == "t3connect" and _clerk_token(ctx):
        try:
            status = environment_status(
                ctx,
                str(ref.environment_id or ref.name),
                store=_store(ctx),
            )
            live = isinstance(status, Mapping) and status.get("status") == "online"
            if isinstance(status, Mapping):
                if status.get("environmentId"):
                    env_id = status.get("environmentId")
                desc = status.get("descriptor")
                if isinstance(desc, Mapping):
                    if "label" in desc:
                        label = desc.get("label")
                    if desc.get("environmentId"):
                        env_id = desc.get("environmentId")
        except Exception:
            live = False
    else:
        token = _token(ctx, ref)
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
        "auth": "ok" if _has_auth(ctx, ref) else "expired",
        "live": live,
    }


def handle_t3_environments(args: dict, **kwargs) -> str:
    try:
        ctx = _ctx(kwargs)
        if ctx is None:
            return _stub("t3_environments")
        args = args or {}
        _ensure_discovered(ctx)
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
        ref = _resolved_env(ctx, args.get("environment"))
        snapshot = _client_for(ctx, ref).shell()
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
        ref = _resolved_env(ctx, args.get("environment"))
        before = args.get("before_cursor")
        if before is not None:
            before = str(before)
        snapshot = _client_for(ctx, ref).thread(
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
    ref = _resolved_env(ctx, args.get("environment"))
    result = _client_for(ctx, ref).dispatch(command)
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


def _coerce_seconds(raw: Any, default: float) -> float:
    if raw is None or isinstance(raw, bool):
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _wait_interval(args: dict) -> float:
    value = _coerce_seconds(args.get("interval"), DEFAULT_WAIT_INTERVAL)
    return max(float(MIN_WAIT_INTERVAL), value)


def _wait_timeout(args: dict) -> float:
    value = _coerce_seconds(args.get("timeout"), DEFAULT_WAIT_TIMEOUT)
    return max(0.0, value)


def _thread_from_shell(snapshot: Any, thread_id: str) -> dict | None:
    if not isinstance(snapshot, dict):
        return None
    for raw in snapshot.get("threads") or []:
        if isinstance(raw, dict) and raw.get("id") == thread_id:
            return raw
    return None


def _activity_excerpt(thread: Any) -> str | None:
    if not isinstance(thread, dict):
        return None
    text = ""
    progress = thread.get("planProgress")
    if isinstance(progress, dict):
        step = progress.get("step")
        if isinstance(step, str) and step.strip():
            text = step.strip()
    if not text:
        session = thread.get("session")
        if isinstance(session, dict):
            err = session.get("lastError")
            if isinstance(err, str) and err.strip():
                text = err.strip()
    if not text:
        latest = thread.get("latestTurn")
        if isinstance(latest, dict):
            parts = [
                str(part)
                for part in (latest.get("state"), latest.get("turnId"))
                if part
            ]
            text = " ".join(parts)
    if not text:
        title = thread.get("title")
        if isinstance(title, str) and title.strip():
            text = title.strip()
    if not text:
        return None
    if len(text) > WAIT_EXCERPT_LIMIT:
        return text[: WAIT_EXCERPT_LIMIT - 1] + "…"
    return text


def _wait_terminal_status(thread: dict | None) -> str | None:
    if not isinstance(thread, dict):
        return None
    if thread.get("hasPendingApprovals"):
        return "approval"
    if thread.get("hasPendingUserInput"):
        return "user-input"
    latest = thread.get("latestTurn")
    if not isinstance(latest, dict):
        return None
    state = latest.get("state")
    if state == "running":
        return None
    if state in _SETTLED_STATES or (isinstance(state, str) and state):
        return "settled"
    return None


def _wait_payload(environment: str, thread_id: str, status: str, thread: dict | None) -> str:
    row = thread if isinstance(thread, dict) else {}
    return json.dumps(
        {
            "environment": environment,
            "threadId": thread_id,
            "status": status,
            "latestTurn": _latest_turn_status(row.get("latestTurn")),
            "hasPendingApprovals": bool(row.get("hasPendingApprovals")),
            "hasPendingUserInput": bool(row.get("hasPendingUserInput")),
            "excerpt": _activity_excerpt(row),
        }
    )


def handle_t3_wait(args: dict, **kwargs) -> str:
    try:
        ctx = _ready_ctx(kwargs)
        if ctx is None:
            return _stub("t3_wait")
        args = args or {}
        thread_id = args.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.strip():
            return json_error(
                "thread_id is required",
                "pass thread_id from t3_list or t3_new_thread",
            )
        thread_id = thread_id.strip()
        ref = _resolved_env(ctx, args.get("environment"))
        sleep = kwargs["sleep"] if "sleep" in kwargs else time.sleep
        monotonic = kwargs["monotonic"] if "monotonic" in kwargs else time.monotonic
        interval = _wait_interval(args)
        timeout = _wait_timeout(args)
        client = _client_for(ctx, ref)
        deadline = monotonic() + timeout
        last_thread: dict | None = None
        while True:
            # TODO(CP-1): poll GET /api/orchestration/threads/:id once a live
            # server confirms thread detail exposes settled status. Decided
            # fallback: GET /api/orchestration/shell and read that thread's
            # latestTurn + hasPendingApprovals / hasPendingUserInput.
            snapshot = client.shell()
            last_thread = _thread_from_shell(snapshot, thread_id)
            status = _wait_terminal_status(last_thread)
            if status is not None:
                return _wait_payload(ref.name, thread_id, status, last_thread)
            remaining = deadline - monotonic()
            if remaining <= 0:
                return _wait_payload(ref.name, thread_id, "timeout", last_thread)
            sleep(min(interval, remaining))
    except TalariaError as exc:
        return exc.to_json()
    except Exception as exc:
        return _unexpected(exc)


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import threading

    box: list[Any] = []
    err: list[BaseException] = []

    def worker() -> None:
        try:
            box.append(asyncio.run(coro))
        except BaseException as exc:
            err.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    if err:
        raise err[0]
    return box[0]


def _ws_client_for(ctx, ref: EnvironmentRef):
    factory = _ws_client_factory
    if factory is not None:
        return factory(ref)
    env = _client_for(ctx, ref)

    def ticket_fn() -> str:
        raw = env.ws_ticket()
        if isinstance(raw, dict):
            ticket = raw.get("ticket")
            if isinstance(ticket, str) and ticket.strip():
                return ticket.strip()
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        raise T3ApiError(200, "websocket-ticket response missing ticket")

    return T3WsClient(ref.base_url, ticket_fn)


async def _ws_call(client, tag: str, payload: Mapping[str, Any]) -> Any:
    result = client.request(tag, payload)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _ws_close(client) -> None:
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        closing = close()
        if inspect.isawaitable(closing):
            await closing
    except Exception:
        return


def _ws_request(ctx, ref: EnvironmentRef, tag: str, payload: Mapping[str, Any]) -> Any:
    async def body():
        client = _ws_client_for(ctx, ref)
        try:
            return await _ws_call(client, tag, payload)
        finally:
            await _ws_close(client)

    return _run_async(body())


def _walk_cause(cause: Any):
    if isinstance(cause, dict):
        yield cause
        for key in ("error", "cause", "defect", "value"):
            yield from _walk_cause(cause.get(key))
        return
    if isinstance(cause, list):
        for item in cause:
            yield from _walk_cause(item)


def _authz_from_cause(cause: Any) -> dict | None:
    for node in _walk_cause(cause):
        if node.get("_tag") == "EnvironmentAuthorizationError":
            return node
        error = node.get("error")
        if isinstance(error, dict) and error.get("_tag") == "EnvironmentAuthorizationError":
            return error
    return None


def _tagged_from_cause(cause: Any) -> dict | None:
    skip = frozenset({"Fail", "Die", "Interrupt", "Empty", "Sequential", "Parallel"})
    for node in _walk_cause(cause):
        tag = node.get("_tag")
        if isinstance(tag, str) and tag and tag not in skip:
            return node
    return None


def _rpc_failure_json(exc: RpcExitFailure) -> str:
    authz = _authz_from_cause(exc.cause)
    if authz is not None:
        scope = authz.get("requiredScope")
        if not isinstance(scope, str) or not scope.strip():
            scope = authz.get("required_scope")
        scope = scope.strip() if isinstance(scope, str) else None
        message = authz.get("message")
        if not isinstance(message, str) or not message.strip():
            if scope:
                message = (
                    "The authenticated token is missing required scope: "
                    f"{scope}."
                )
            else:
                message = "environment authorization error"
        if scope and scope not in message:
            message = f"{message} (missing scope: {scope})"
        payload = {
            "error": message,
            "hint": (
                f"this RPC needs the {scope} scope; re-pair with a token "
                "that includes it"
                if scope
                else "the environment token is missing a required scope"
            ),
        }
        if scope:
            payload["requiredScope"] = scope
        return json.dumps(payload)
    tagged = _tagged_from_cause(exc.cause)
    if tagged is not None:
        tag = tagged.get("_tag")
        message = tagged.get("message")
        if not isinstance(message, str) or not message.strip():
            defect = tagged.get("defect")
            message = defect if isinstance(defect, str) else str(tag)
        return json_error(str(message), f"T3 RPC failed ({tag})")
    for node in _walk_cause(exc.cause):
        if node.get("_tag") == "Die":
            defect = node.get("defect")
            text = defect if isinstance(defect, str) else str(node)
            return json_error(text, "T3 RPC failed")
    return json_error(str(exc.cause), "T3 RPC failed")


def _required_project_id(args: dict) -> str:
    raw = args.get("project_id")
    if not isinstance(raw, str) or not raw.strip():
        raise TalariaError(
            "project_id is required",
            "pass project_id from t3_list",
        )
    return raw.strip()


def _required_path(args: dict) -> str:
    raw = args.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise TalariaError(
            "path is required",
            "pass path as a project-relative path (wire relativePath)",
        )
    value = raw.strip()
    if len(value) > FILE_PATH_MAX:
        raise TalariaError(
            "path is too long",
            f"relativePath max is {FILE_PATH_MAX} characters",
        )
    return value


def _join_cwd(root: str, relative: Any) -> str:
    if relative is None:
        return root
    if not isinstance(relative, str):
        raise TalariaError(
            "path must be a string",
            "pass path as a project-relative subdirectory",
        )
    relative = relative.strip().replace("\\", "/")
    if not relative or relative in (".", "./"):
        return root
    if relative.startswith("/") or relative.startswith("~"):
        raise TalariaError(
            "path must be relative",
            "pass a path under the project's workspaceRoot, not an absolute path",
        )
    while relative.startswith("./"):
        relative = relative[2:]
    return root.rstrip("/") + "/" + relative.lstrip("/")


def _workspace_root(ctx, ref: EnvironmentRef, project_id: str, kwargs) -> str:
    injected = kwargs.get("cwd")
    if isinstance(injected, str) and injected.strip():
        return injected.strip()
    snapshot = _client_for(ctx, ref).shell()
    if not isinstance(snapshot, dict):
        snapshot = {}
    for raw in snapshot.get("projects") or []:
        if not isinstance(raw, dict) or raw.get("id") != project_id:
            continue
        root = raw.get("workspaceRoot")
        if isinstance(root, str) and root.strip():
            return root.strip()
        raise TalariaError(
            f"project {project_id!r} has no workspaceRoot",
            "the shell snapshot project is missing workspaceRoot",
        )
    raise TalariaError(
        f"project_id {project_id!r} not found",
        "pass project_id from t3_list",
    )


def _file_result(ref: EnvironmentRef, project_id: str, value: Any) -> str:
    if isinstance(value, dict):
        payload = dict(value)
    else:
        payload = {"result": value}
    payload["environment"] = ref.name
    payload["projectId"] = project_id
    return json.dumps(payload)


def _cap_read(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    out = dict(value)
    contents = out.get("contents")
    if not isinstance(contents, str):
        return out
    encoded = contents.encode("utf-8")
    if not isinstance(out.get("byteLength"), int) or out["byteLength"] < 0:
        out["byteLength"] = len(encoded)
    if len(encoded) <= READ_BYTE_LIMIT:
        if "truncated" not in out:
            out["truncated"] = False
        return out
    out["contents"] = encoded[:READ_BYTE_LIMIT].decode("utf-8", errors="ignore")
    out["truncated"] = True
    return out


def _flag(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in ("true", "1", "yes"):
            return True
        if value in ("false", "0", "no", ""):
            return False
        return default
    if isinstance(raw, (int, float)):
        return bool(raw)
    return default


def _search_limit(raw: Any) -> int:
    if raw is None or isinstance(raw, bool):
        return SEARCH_LIMIT_DEFAULT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return SEARCH_LIMIT_DEFAULT
    if value < 1:
        return SEARCH_LIMIT_DEFAULT
    if value > SEARCH_LIMIT_MAX:
        return SEARCH_LIMIT_MAX
    return value


def _search_query(args: dict) -> str:
    query = args.get("query")
    if not isinstance(query, str) or query == "":
        raise TalariaError(
            "query is required",
            "pass query as the content search string",
        )
    if len(query) > SEARCH_QUERY_MAX:
        raise TalariaError(
            "query is too long",
            f"search query max is {SEARCH_QUERY_MAX} characters",
        )
    return query


def _file_tool(
    args: dict | None,
    kwargs: dict,
    *,
    name: str,
    tag: str,
    payload_fn,
    transform=None,
    prepare=None,
) -> str:
    ctx = _ctx(kwargs)
    if ctx is None:
        return _stub(name)
    args = args or {}
    project_id = _required_project_id(args)
    if prepare is not None:
        prepare(args)
    ref = _resolved_env(ctx, args.get("environment"))
    cwd = _workspace_root(ctx, ref, project_id, kwargs)
    payload = payload_fn(cwd, args)
    value = _ws_request(ctx, ref, tag, payload)
    if transform is not None:
        value = transform(value)
    return _file_result(ref, project_id, value)


def handle_t3_ls(args: dict, **kwargs) -> str:
    try:
        return _file_tool(
            args,
            kwargs,
            name="t3_ls",
            tag=LS_TAG,
            payload_fn=lambda cwd, a: {"cwd": _join_cwd(cwd, a.get("path"))},
        )
    except RpcExitFailure as exc:
        return _rpc_failure_json(exc)
    except TalariaError as exc:
        return exc.to_json()
    except Exception as exc:
        return _unexpected(exc)


def handle_t3_read_file(args: dict, **kwargs) -> str:
    try:
        def payload_fn(cwd: str, a: dict) -> dict:
            return {"cwd": cwd, "relativePath": _required_path(a)}

        return _file_tool(
            args,
            kwargs,
            name="t3_read_file",
            tag=READ_TAG,
            payload_fn=payload_fn,
            transform=_cap_read,
            prepare=_required_path,
        )
    except RpcExitFailure as exc:
        return _rpc_failure_json(exc)
    except TalariaError as exc:
        return exc.to_json()
    except Exception as exc:
        return _unexpected(exc)


def handle_t3_write_file(args: dict, **kwargs) -> str:
    try:
        def payload_fn(cwd: str, a: dict) -> dict:
            contents = a.get("contents")
            if not isinstance(contents, str):
                raise TalariaError(
                    "contents is required",
                    "pass contents as the full file text to write",
                )
            return {
                "cwd": cwd,
                "relativePath": _required_path(a),
                "contents": contents,
            }

        def prepare(a: dict) -> None:
            _required_path(a)
            if not isinstance(a.get("contents"), str):
                raise TalariaError(
                    "contents is required",
                    "pass contents as the full file text to write",
                )

        return _file_tool(
            args,
            kwargs,
            name="t3_write_file",
            tag=WRITE_TAG,
            payload_fn=payload_fn,
            prepare=prepare,
        )
    except RpcExitFailure as exc:
        return _rpc_failure_json(exc)
    except TalariaError as exc:
        return exc.to_json()
    except Exception as exc:
        return _unexpected(exc)


def handle_t3_search(args: dict, **kwargs) -> str:
    try:
        def payload_fn(cwd: str, a: dict) -> dict:
            return {
                "cwd": cwd,
                "query": _search_query(a),
                "limit": _search_limit(a.get("limit")),
                "caseSensitive": _flag(a.get("case_sensitive"), False),
                "wholeWord": _flag(a.get("whole_word"), False),
                "useRegex": _flag(a.get("use_regex"), False),
            }

        return _file_tool(
            args,
            kwargs,
            name="t3_search",
            tag=SEARCH_TAG,
            payload_fn=payload_fn,
            prepare=_search_query,
        )
    except RpcExitFailure as exc:
        return _rpc_failure_json(exc)
    except TalariaError as exc:
        return exc.to_json()
    except Exception as exc:
        return _unexpected(exc)
