"""Fold subscribeThread/subscribeShell items and push salient events.

Reducer is pure (sequence dedupe, snapshot replacement). The background
reader is started from t3_watch via ctx.spawn_task (or a test-injected
spawn fn), never from register().
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from collections.abc import Callable, Mapping
from typing import Any

from .errors import TalariaError

WATCH_STATE_KEY = "watch"
SHELL_TAG = "orchestration.subscribeShell"
THREAD_TAG = "orchestration.subscribeThread"
GRANT_CONFIG_KEY = "allow_gateway_injection"
GRANT_HINT = (
    "set plugins.entries.t3code.settings.allow_gateway_injection: true in "
    "config.yaml (boolean true, not a string). Then retry t3_watch. Without "
    "this grant Talaria will not inject live T3 events into the conversation. "
    "Gateway inject_message also needs the host grant "
    "plugins.entries.t3code.allow_gateway_injection: true."
)
_SETTLED_TURN_STATES = frozenset({"completed", "interrupted", "error"})

_live: dict[str, dict[str, Any]] = {}


def reset_live_watches() -> None:
    """Drop in-process readers. Tests and on_unload call this."""
    for live in list(_live.values()):
        _interrupt_live(live)
    _live.clear()


def empty_reducer_state() -> dict[str, Any]:
    return {"sequence": 0, "threads": {}}


def injection_granted(ctx) -> bool:
    """Fail closed. ``ctx.get_config("allow_gateway_injection")`` must be True."""
    if ctx is None:
        return False
    get_config = getattr(ctx, "get_config", None)
    if not callable(get_config):
        return False
    try:
        return get_config(GRANT_CONFIG_KEY, False) is True
    except Exception:
        return False


def grant_denied_json() -> str:
    return json.dumps(
        {
            "error": "gateway injection is not granted",
            "hint": GRANT_HINT,
            "watched": False,
        }
    )


def watch_id(kind: str, environment: str, thread_id: str | None = None) -> str:
    if kind == "thread":
        return f"{environment}:thread:{thread_id}"
    return f"{environment}:shell"


def reduce_item(
    state: Mapping[str, Any] | None, item: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fold one stream item. Sequence <= cursor is dropped; snapshots replace."""
    current = _copy_state(state)
    if not isinstance(item, dict):
        return current, []
    kind = item.get("kind")
    if kind == "synchronized":
        return current, []
    if kind == "snapshot":
        return _apply_snapshot(current, item.get("snapshot"))
    if kind == "event":
        return _apply_thread_event(current, item.get("event"))
    if kind in (
        "thread-upserted",
        "thread-removed",
        "project-upserted",
        "project-removed",
    ):
        return _apply_shell_event(current, item)
    return current, []


def _copy_state(state: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state, Mapping):
        return empty_reducer_state()
    threads = state.get("threads")
    copied: dict[str, dict[str, Any]] = {}
    if isinstance(threads, Mapping):
        for key, flags in threads.items():
            if isinstance(flags, Mapping):
                copied[str(key)] = dict(flags)
    sequence = state.get("sequence", 0)
    if not isinstance(sequence, int) or sequence < 0:
        sequence = 0
    return {"sequence": sequence, "threads": copied}


def _thread_flags(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    thread_id = raw.get("id")
    if not isinstance(thread_id, str) or not thread_id:
        return None
    latest = raw.get("latestTurn")
    turn_state = None
    turn_id = None
    if isinstance(latest, dict):
        turn_state = latest.get("state")
        turn_id = latest.get("turnId")
    session = raw.get("session")
    session_status = None
    active_turn_id = None
    if isinstance(session, dict):
        session_status = session.get("status")
        active_turn_id = session.get("activeTurnId")
    return {
        "id": thread_id,
        "latestTurnState": turn_state,
        "latestTurnId": turn_id,
        "hasPendingApprovals": bool(raw.get("hasPendingApprovals")),
        "hasPendingUserInput": bool(raw.get("hasPendingUserInput")),
        "sessionStatus": session_status,
        "activeTurnId": active_turn_id,
    }


def _threads_from_snapshot(snapshot: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    raw_threads = snapshot.get("threads")
    if isinstance(raw_threads, list):
        for raw in raw_threads:
            flags = _thread_flags(raw)
            if flags is not None:
                out[flags["id"]] = flags
        return out
    flags = _thread_flags(snapshot.get("thread"))
    if flags is not None:
        out[flags["id"]] = flags
    return out


def _snapshot_sequence(snapshot: Any, fallback: int) -> int:
    if not isinstance(snapshot, dict):
        return fallback
    seq = snapshot.get("snapshotSequence")
    if isinstance(seq, int) and seq >= 0:
        return seq
    return fallback


def _salient(
    kind: str,
    thread_id: str | None,
    sequence: int | None,
    **extra: Any,
) -> dict[str, Any]:
    event = {"kind": kind, "threadId": thread_id, "sequence": sequence}
    event.update(extra)
    return event


def _diff_thread(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    sequence: int | None,
) -> list[dict[str, Any]]:
    old = previous if isinstance(previous, Mapping) else {}
    thread_id = current.get("id")
    events: list[dict[str, Any]] = []
    if current.get("hasPendingApprovals") and not old.get("hasPendingApprovals"):
        events.append(_salient("approval-requested", thread_id, sequence))
    if current.get("hasPendingUserInput") and not old.get("hasPendingUserInput"):
        events.append(_salient("user-input-requested", thread_id, sequence))
    new_state = current.get("latestTurnState")
    old_state = old.get("latestTurnState")
    if new_state in _SETTLED_TURN_STATES and new_state != old_state:
        events.append(
            _salient("turn-settled", thread_id, sequence, turnState=new_state)
        )
    return events


def _diff_threads(
    previous: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Mapping[str, Any]],
    sequence: int | None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for thread_id, flags in current.items():
        events.extend(_diff_thread(previous.get(thread_id), flags, sequence))
    return events


def _apply_snapshot(
    state: dict[str, Any], snapshot: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    threads = _threads_from_snapshot(snapshot)
    sequence = _snapshot_sequence(snapshot, state["sequence"])
    events = _diff_threads(state["threads"], threads, sequence)
    return {"sequence": sequence, "threads": threads}, events


def _stale(sequence: Any, cursor: int) -> bool:
    return isinstance(sequence, int) and sequence <= cursor


def _apply_shell_event(
    state: dict[str, Any], item: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sequence = item.get("sequence")
    if _stale(sequence, state["sequence"]):
        return state, []
    next_seq = sequence if isinstance(sequence, int) and sequence >= 0 else state["sequence"]
    kind = item.get("kind")
    threads = dict(state["threads"])
    events: list[dict[str, Any]] = []
    if kind == "thread-upserted":
        flags = _thread_flags(item.get("thread"))
        if flags is not None:
            events = _diff_thread(threads.get(flags["id"]), flags, next_seq)
            threads[flags["id"]] = flags
    elif kind == "thread-removed":
        thread_id = item.get("threadId")
        if isinstance(thread_id, str):
            threads.pop(thread_id, None)
    return {"sequence": next_seq, "threads": threads}, events


def _activity_kind(event: Mapping[str, Any]) -> str | None:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None
    activity = payload.get("activity")
    if isinstance(activity, Mapping):
        kind = activity.get("kind")
        if isinstance(kind, str):
            return kind
    kind = payload.get("kind")
    return kind if isinstance(kind, str) else None


def _event_thread_id(event: Mapping[str, Any], state: Mapping[str, Any]) -> str | None:
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        thread_id = payload.get("threadId")
        if isinstance(thread_id, str) and thread_id:
            return thread_id
        activity = payload.get("activity")
        if isinstance(activity, Mapping):
            thread_id = activity.get("threadId")
            if isinstance(thread_id, str) and thread_id:
                return thread_id
    aggregate = event.get("aggregateId")
    if isinstance(aggregate, str) and aggregate:
        return aggregate
    threads = state.get("threads")
    if isinstance(threads, Mapping) and len(threads) == 1:
        return next(iter(threads))
    return None


def _settled_turn_state_for_session(status: Any) -> str | None:
    if status in ("idle", "ready"):
        return "completed"
    if status == "error":
        return "error"
    if status in ("interrupted", "stopped"):
        return "interrupted"
    return None


def _fold_thread_event(flags: dict[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(flags)
    etype = event.get("type")
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    if etype == "thread.settled":
        out["latestTurnState"] = "completed"
        return out
    if etype == "thread.session-set":
        session = payload.get("session")
        if not isinstance(session, Mapping):
            return out
        status = session.get("status")
        active = session.get("activeTurnId")
        out["sessionStatus"] = status
        out["activeTurnId"] = active
        settled = _settled_turn_state_for_session(status)
        if status == "running" and isinstance(active, str) and active:
            out["latestTurnState"] = "running"
            out["latestTurnId"] = active
        elif flags.get("latestTurnState") == "running" and settled is not None:
            out["latestTurnState"] = settled
        return out
    if etype == "thread.message-sent":
        role = payload.get("role")
        turn_id = payload.get("turnId")
        streaming = bool(payload.get("streaming"))
        if role != "assistant" or not isinstance(turn_id, str) or not turn_id:
            return out
        current_id = flags.get("latestTurnId")
        if current_id not in (None, turn_id):
            return out
        turn_still_running = (
            flags.get("sessionStatus") == "running"
            and flags.get("activeTurnId") == turn_id
        )
        settles = (not streaming) and (not turn_still_running)
        prev_state = flags.get("latestTurnState")
        if settles:
            if prev_state in ("interrupted", "error"):
                out["latestTurnState"] = prev_state
            else:
                out["latestTurnState"] = "completed"
        else:
            out["latestTurnState"] = "running"
        out["latestTurnId"] = turn_id
        return out
    if etype == "thread.turn-interrupt-requested":
        turn_id = payload.get("turnId")
        if flags.get("latestTurnId") == turn_id and turn_id is not None:
            out["latestTurnState"] = "interrupted"
        return out
    activity_kind = _activity_kind(event)
    if activity_kind == "approval.requested":
        out["hasPendingApprovals"] = True
    elif activity_kind == "user-input.requested":
        out["hasPendingUserInput"] = True
    return out


def _apply_thread_event(
    state: dict[str, Any], event: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(event, dict):
        return state, []
    sequence = event.get("sequence")
    if _stale(sequence, state["sequence"]):
        return state, []
    next_seq = sequence if isinstance(sequence, int) and sequence >= 0 else state["sequence"]
    threads = dict(state["threads"])
    thread_id = _event_thread_id(event, state)
    prev = threads.get(thread_id) if thread_id else None
    base = dict(prev) if isinstance(prev, dict) else {}
    if thread_id:
        base["id"] = thread_id
    updated = _fold_thread_event(base, event)
    events: list[dict[str, Any]] = []
    if thread_id:
        threads[thread_id] = updated
        events = _diff_thread(prev, updated, next_seq)
    return {"sequence": next_seq, "threads": threads}, events


def _state_get(ctx, key: str, default: Any = None) -> Any:
    state = getattr(ctx, "state", None)
    if state is None:
        return default
    getter = getattr(state, "get", None)
    if not callable(getter):
        return default
    return getter(key, default)


def _state_set(ctx, key: str, value: Any) -> None:
    state = getattr(ctx, "state", None)
    if state is None:
        return
    setter = getattr(state, "set", None)
    if callable(setter):
        setter(key, value)
        return
    if isinstance(state, dict):
        state[key] = value


def _watch_blob(ctx) -> dict[str, Any]:
    raw = _state_get(ctx, WATCH_STATE_KEY, {}) or {}
    if not isinstance(raw, dict):
        return {"cursors": {}, "targets": {}}
    cursors = raw.get("cursors")
    targets = raw.get("targets")
    return {
        "cursors": dict(cursors) if isinstance(cursors, dict) else {},
        "targets": dict(targets) if isinstance(targets, dict) else {},
    }


def _save_watch_blob(ctx, blob: dict[str, Any]) -> None:
    _state_set(ctx, WATCH_STATE_KEY, blob)


def _cursor_for(ctx, wid: str) -> int:
    seq = _watch_blob(ctx)["cursors"].get(wid, 0)
    if isinstance(seq, int) and seq > 0:
        return seq
    return 0


def _put_target(ctx, wid: str, target: dict[str, Any], sequence: int | None = None) -> None:
    blob = _watch_blob(ctx)
    blob["targets"][wid] = target
    if sequence is not None:
        blob["cursors"][wid] = sequence
    _save_watch_blob(ctx, blob)


def _drop_target(ctx, wid: str) -> None:
    blob = _watch_blob(ctx)
    blob["targets"].pop(wid, None)
    blob["cursors"].pop(wid, None)
    _save_watch_blob(ctx, blob)


def parse_kind(args: Mapping[str, Any] | None) -> str:
    raw = None if args is None else args.get("kind")
    if not isinstance(raw, str):
        raise TalariaError(
            "kind is required",
            "pass kind=thread or kind=shell",
        )
    kind = raw.strip()
    if kind not in ("thread", "shell"):
        raise TalariaError(
            "kind must be thread or shell",
            "t3_watch/t3_unwatch take kind=thread or kind=shell",
        )
    return kind


def parse_thread_id(args: Mapping[str, Any] | None, kind: str) -> str | None:
    if kind != "thread":
        return None
    raw = None if args is None else args.get("thread_id")
    if not isinstance(raw, str) or not raw.strip():
        raise TalariaError(
            "thread_id is required when kind=thread",
            "pass thread_id from t3_list or t3_new_thread",
        )
    return raw.strip()


def format_inject_message(
    environment: str, event: Mapping[str, Any]
) -> str:
    kind = event.get("kind")
    thread_id = event.get("threadId") or "unknown"
    if kind == "approval-requested":
        action = "approval requested. Use t3_respond with kind=approval."
    elif kind == "user-input-requested":
        action = "user-input requested. Use t3_respond with kind=user-input."
    elif kind == "turn-settled":
        turn_state = event.get("turnState") or "settled"
        action = f"turn settled ({turn_state})."
    else:
        action = str(kind)
    return f"[t3code {environment}] thread {thread_id}: {action}"


async def run_watch(
    client: Any,
    tag: str,
    payload: Mapping[str, Any],
    *,
    reducer_state: Mapping[str, Any] | None,
    on_salient: Callable[[dict[str, Any]], None],
    persist_sequence: Callable[[int], None],
    stop: threading.Event,
) -> dict[str, Any]:
    """Consume a subscribe stream until stop or the iterator ends."""
    state = _copy_state(reducer_state)
    subscribe = client.subscribe
    agen = subscribe(tag, payload)
    if inspect.isawaitable(agen):
        agen = await agen
    try:
        async for item in agen:
            if stop.is_set():
                break
            state, events = reduce_item(state, item)
            persist_sequence(state["sequence"])
            for event in events:
                on_salient(event)
    finally:
        aclose = getattr(agen, "aclose", None)
        if callable(aclose):
            try:
                closing = aclose()
                if inspect.isawaitable(closing):
                    await closing
            except Exception:
                pass
    return state


async def _close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        closing = close()
        if inspect.isawaitable(closing):
            await closing
    except Exception:
        return


def _call_spawn(spawn: Callable, coro, name: str):
    try:
        return spawn(coro, name=name)
    except TypeError:
        return spawn(coro)


def _thread_fallback(coro) -> threading.Thread:
    def worker() -> None:
        try:
            asyncio.run(coro)
        except Exception:
            return

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def _inject_event(ctx, environment: str, event: Mapping[str, Any], kwargs: Mapping[str, Any] | None) -> None:
    inject = getattr(ctx, "inject_message", None)
    if not callable(inject):
        return
    text = format_inject_message(environment, event)
    session_key = None
    if kwargs is not None:
        session_key = kwargs.get("session_key")
    try:
        if session_key:
            inject(text, role="user", session_key=session_key)
        else:
            inject(text, role="user")
    except TypeError:
        try:
            inject(text)
        except Exception:
            return
    except Exception:
        return


def start_watch(
    ctx,
    *,
    kind: str,
    environment: str,
    thread_id: str | None,
    spawn: Callable | None,
    client_factory: Callable[[], Any],
    kwargs: Mapping[str, Any] | None = None,
) -> str:
    wid = watch_id(kind, environment, thread_id)
    target = {"kind": kind, "environment": environment, "threadId": thread_id}
    existing = _live.get(wid)
    if existing is not None and not existing["stop"].is_set():
        return json.dumps(
            {
                "watched": True,
                "kind": kind,
                "environment": environment,
                "threadId": thread_id,
                "afterSequence": _cursor_for(ctx, wid),
            }
        )
    cursor = _cursor_for(ctx, wid)
    _put_target(ctx, wid, target, cursor if cursor else None)
    stop = threading.Event()
    live: dict[str, Any] = {"stop": stop, "task": None, "client": None}
    _live[wid] = live
    payload: dict[str, Any] = {"requestCompletionMarker": True}
    if cursor > 0:
        payload["afterSequence"] = cursor
    if kind == "thread":
        payload["threadId"] = thread_id
        tag = THREAD_TAG
    else:
        tag = SHELL_TAG

    async def body() -> None:
        live["loop"] = asyncio.get_running_loop()
        client = client_factory()
        live["client"] = client
        try:
            def persist(sequence: int) -> None:
                blob = _watch_blob(ctx)
                blob["cursors"][wid] = sequence
                if wid not in blob["targets"]:
                    blob["targets"][wid] = target
                _save_watch_blob(ctx, blob)

            def on_salient(event: dict[str, Any]) -> None:
                _inject_event(ctx, environment, event, kwargs)

            await run_watch(
                client,
                tag,
                payload,
                reducer_state={"sequence": cursor, "threads": {}},
                on_salient=on_salient,
                persist_sequence=persist,
                stop=stop,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        finally:
            await _close_client(client)
            current = _live.get(wid)
            if current is live:
                _live.pop(wid, None)

    name = f"t3-watch-{wid}"
    task = None
    if spawn is not None:
        try:
            task = _call_spawn(spawn, body(), name)
        except RuntimeError:
            task = _thread_fallback(body())
    else:
        task = _thread_fallback(body())
    live["task"] = task
    return json.dumps(
        {
            "watched": True,
            "kind": kind,
            "environment": environment,
            "threadId": thread_id,
            "afterSequence": cursor,
        }
    )


def _interrupt_live(live: Mapping[str, Any]) -> None:
    stop = live.get("stop")
    if stop is not None:
        stop.set()
    task = live.get("task")
    cancel = getattr(task, "cancel", None)
    if callable(cancel):
        try:
            cancel()
        except Exception:
            pass
    client = live.get("client")
    if client is None:
        return
    loop = live.get("loop")
    if loop is not None:
        try:
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(_close_client(client), loop)
                return
        except Exception:
            pass
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            result.close()
    except Exception:
        return


def stop_watch(
    ctx,
    *,
    kind: str,
    environment: str,
    thread_id: str | None,
) -> str:
    wid = watch_id(kind, environment, thread_id)
    live = _live.pop(wid, None)
    if live is not None:
        _interrupt_live(live)
    _drop_target(ctx, wid)
    return json.dumps(
        {
            "watched": False,
            "kind": kind,
            "environment": environment,
            "threadId": thread_id,
        }
    )
