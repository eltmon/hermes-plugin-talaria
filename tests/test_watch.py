"""WI-12: subscribe reducer + t3_watch grant/inject gating."""

from __future__ import annotations

import json

from talaria.tools import (
    bind_ctx,
    handle_t3_unwatch,
    handle_t3_watch,
    set_client_factory,
    set_ws_client_factory,
)
from talaria.watch import (
    WATCH_STATE_KEY,
    empty_reducer_state,
    injection_granted,
    reduce_item,
    reset_live_watches,
)

BASE = "https://t3.example.test"
TOKEN = "tok-test"
LAPTOP = {"base_url": BASE}
THREAD_ID = "thr-fix-flaky"


class PluginStateLike:
    def __init__(self, data=None) -> None:
        self._data = dict(data or {})

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class DummyTask:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def done(self) -> bool:
        return self.cancelled


class SpawnBox:
    def __init__(self) -> None:
        self.coros: list = []
        self.tasks: list[DummyTask] = []

    def __call__(self, coro, name=None):
        self.coros.append(coro)
        task = DummyTask()
        self.tasks.append(task)
        return task

    def close(self) -> None:
        for coro in self.coros:
            try:
                coro.close()
            except Exception:
                pass


class FakeCtx:
    def __init__(
        self,
        settings=None,
        secrets=None,
        state=None,
    ) -> None:
        self._settings = dict(settings or {})
        self.secrets = dict(secrets or {})
        self.state = state if state is not None else PluginStateLike()
        self.injected: list[dict] = []

    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def inject_message(self, content, role="user", *, session_key=None):
        self.injected.append(
            {"content": content, "role": role, "session_key": session_key}
        )
        return True

    def spawn_task(self, coro, *, name=None):
        raise AssertionError("pass spawn= to tests; ctx.spawn_task is not used here")


class RealishCtx:
    """get_config reads entry.settings only, like Hermes PluginContext."""

    def __init__(self, entry, secrets=None) -> None:
        self._entry = dict(entry)
        self.secrets = dict(secrets or {})
        self.state = PluginStateLike()
        self.injected: list[dict] = []

    def get_config(self, key, default=None):
        settings = self._entry.get("settings")
        if not isinstance(settings, dict):
            return default
        return settings.get(key, default)

    def has_capability(self, capability: str) -> bool:
        return False

    def inject_message(self, content, role="user", *, session_key=None):
        self.injected.append(
            {"content": content, "role": role, "session_key": session_key}
        )
        return True

    def spawn_task(self, coro, *, name=None):
        raise AssertionError("pass spawn= to tests; ctx.spawn_task is not used here")


class FakeSubClient:
    def __init__(self, items) -> None:
        self.items = list(items)
        self.tag = None
        self.payload = None
        self.closed = False

    async def subscribe(self, tag, payload=None, **_kwargs):
        self.tag = tag
        self.payload = dict(payload or {})
        for item in self.items:
            yield item

    async def close(self) -> None:
        self.closed = True


def _ctx(**more_settings) -> FakeCtx:
    settings = {"environments": {"laptop": LAPTOP}}
    settings.update(more_settings)
    return FakeCtx(
        settings=settings,
        secrets={"T3CODE_TOKEN_LAPTOP": TOKEN},
    )


def _granted_ctx(**more_settings) -> FakeCtx:
    return _ctx(allow_gateway_injection=True, **more_settings)


def _realish(*, host_grant=False, settings_grant=False) -> RealishCtx:
    settings: dict = {"environments": {"laptop": LAPTOP}}
    if settings_grant:
        settings["allow_gateway_injection"] = True
    entry: dict = {"settings": settings}
    if host_grant:
        entry["allow_gateway_injection"] = True
    return RealishCtx(entry, secrets={"T3CODE_TOKEN_LAPTOP": TOKEN})


def _thread_shell(
    thread_id=THREAD_ID,
    *,
    state="running",
    approvals=False,
    user_input=False,
):
    return {
        "id": thread_id,
        "projectId": "proj-hermes",
        "title": "Fix the flaky read-tools test",
        "latestTurn": {"turnId": "turn-7", "state": state},
        "hasPendingApprovals": approvals,
        "hasPendingUserInput": user_input,
    }


def _shell_snapshot(sequence: int, threads: list[dict]) -> dict:
    return {
        "kind": "snapshot",
        "snapshot": {
            "snapshotSequence": sequence,
            "projects": [],
            "threads": threads,
            "updatedAt": "2026-08-31T00:00:00.000Z",
        },
    }


def _thread_snapshot(sequence: int, thread: dict) -> dict:
    return {
        "kind": "snapshot",
        "snapshot": {
            "snapshotSequence": sequence,
            "thread": thread,
        },
    }


def _fold(items, start=None):
    state = empty_reducer_state() if start is None else start
    events = []
    for item in items:
        state, salient = reduce_item(state, item)
        events.extend(salient)
    return state, events


def setup_function(_fn) -> None:
    bind_ctx(None)
    set_client_factory(None)
    set_ws_client_factory(None)
    reset_live_watches()


def teardown_function(_fn) -> None:
    bind_ctx(None)
    set_client_factory(None)
    set_ws_client_factory(None)
    reset_live_watches()


def test_reducer_dedupes_stale_shell_sequence():
    first = {
        "kind": "thread-upserted",
        "sequence": 4,
        "thread": _thread_shell(state="running"),
    }
    stale = {
        "kind": "thread-upserted",
        "sequence": 4,
        "thread": _thread_shell(state="completed", approvals=True),
    }
    older = {
        "kind": "thread-upserted",
        "sequence": 3,
        "thread": _thread_shell(state="error"),
    }
    state, events = _fold([first, stale, older])
    assert state["sequence"] == 4
    assert state["threads"][THREAD_ID]["latestTurnState"] == "running"
    assert state["threads"][THREAD_ID]["hasPendingApprovals"] is False
    assert events == []


def test_reducer_dedupes_stale_thread_event_sequence():
    live = {
        "kind": "event",
        "event": {
            "sequence": 8,
            "type": "thread.settled",
            "payload": {"threadId": THREAD_ID},
        },
    }
    stale = {
        "kind": "event",
        "event": {
            "sequence": 8,
            "type": "thread.activity-appended",
            "payload": {
                "threadId": THREAD_ID,
                "activity": {"kind": "approval.requested"},
            },
        },
    }
    state, events = _fold([live, stale])
    assert state["sequence"] == 8
    assert [row["kind"] for row in events] == ["turn-settled"]
    assert "approval-requested" not in [row["kind"] for row in events]


def test_reducer_snapshot_replaces_shell_event_state():
    upsert = {
        "kind": "thread-upserted",
        "sequence": 2,
        "thread": _thread_shell(state="running", approvals=True),
    }
    extra = {
        "kind": "thread-upserted",
        "sequence": 3,
        "thread": _thread_shell("thr-other", state="running"),
    }
    snapshot = _shell_snapshot(
        10,
        [_thread_shell(state="completed", approvals=False)],
    )
    state, events = _fold([upsert, extra, snapshot])
    assert state["sequence"] == 10
    assert set(state["threads"]) == {THREAD_ID}
    assert state["threads"][THREAD_ID]["latestTurnState"] == "completed"
    assert state["threads"][THREAD_ID]["hasPendingApprovals"] is False
    kinds = [row["kind"] for row in events]
    assert "turn-settled" in kinds
    assert kinds.count("approval-requested") == 1


def test_reducer_thread_snapshot_replaces_events():
    settled = {
        "kind": "event",
        "event": {
            "sequence": 4,
            "type": "thread.settled",
            "payload": {"threadId": THREAD_ID},
        },
    }
    snapshot = _thread_snapshot(
        20,
        _thread_shell(state="running", user_input=True),
    )
    state, events = _fold([settled, snapshot])
    assert state["sequence"] == 20
    assert state["threads"][THREAD_ID]["latestTurnState"] == "running"
    assert state["threads"][THREAD_ID]["hasPendingUserInput"] is True
    assert events[-1]["kind"] == "user-input-requested"


def test_reducer_session_set_idle_after_running_snapshot_is_turn_settled():
    snapshot = _thread_snapshot(
        1,
        {
            **_thread_shell(state="running"),
            "session": {
                "threadId": THREAD_ID,
                "status": "running",
                "activeTurnId": "turn-7",
                "updatedAt": "2026-08-31T00:00:00.000Z",
            },
        },
    )
    session_set = {
        "kind": "event",
        "event": {
            "sequence": 2,
            "type": "thread.session-set",
            "payload": {
                "threadId": THREAD_ID,
                "session": {
                    "threadId": THREAD_ID,
                    "status": "idle",
                    "activeTurnId": None,
                    "updatedAt": "2026-08-31T00:01:00.000Z",
                },
            },
        },
    }
    state, events = _fold([snapshot, session_set])
    assert state["sequence"] == 2
    assert state["threads"][THREAD_ID]["latestTurnState"] == "completed"
    assert [row["kind"] for row in events] == ["turn-settled"]
    assert events[0]["turnState"] == "completed"
    assert events[0]["threadId"] == THREAD_ID


def test_reducer_emits_pending_flags_on_upsert():
    baseline = {
        "kind": "thread-upserted",
        "sequence": 1,
        "thread": _thread_shell(),
    }
    pending = {
        "kind": "thread-upserted",
        "sequence": 2,
        "thread": _thread_shell(approvals=True, user_input=True),
    }
    _, events = _fold([baseline, pending])
    assert [row["kind"] for row in events] == [
        "approval-requested",
        "user-input-requested",
    ]
    assert all(row["threadId"] == THREAD_ID for row in events)


def test_reducer_synchronized_is_noop():
    state, events = _fold([{"kind": "synchronized"}])
    assert state == empty_reducer_state()
    assert events == []


def test_reducer_unknown_fields_are_tolerated():
    item = {
        "kind": "thread-upserted",
        "sequence": 1,
        "futureKey": {"nested": True},
        "thread": {**_thread_shell(), "brandNew": 1},
    }
    state, events = reduce_item(empty_reducer_state(), item)
    assert state["sequence"] == 1
    assert THREAD_ID in state["threads"]
    assert events == []


def test_injection_granted_fail_closed():
    assert injection_granted(None) is False
    assert injection_granted(_ctx()) is False
    assert injection_granted(_ctx(allow_gateway_injection="true")) is False
    assert injection_granted(_ctx(allow_gateway_injection=1)) is False
    assert injection_granted(_granted_ctx()) is True
    assert injection_granted(_realish(host_grant=True)) is False
    assert injection_granted(_realish(settings_grant=True)) is True
    assert injection_granted(_realish(host_grant=True, settings_grant=True)) is True
    class _ConfigOnly:
        def get_config(self, key, default=None):
            return True if key == "allow_gateway_injection" else default
    assert injection_granted(_ConfigOnly()) is True


def test_watch_without_grant_returns_instructions_and_does_not_inject():
    ctx = _ctx()
    spawn = SpawnBox()
    try:
        raw = handle_t3_watch(
            {"kind": "shell"},
            ctx=ctx,
            spawn=spawn,
            extra=True,
        )
        payload = json.loads(raw)
        assert payload["watched"] is False
        assert payload["error"] == "gateway injection is not granted"
        assert "settings.allow_gateway_injection" in payload["hint"]
        assert "true" in payload["hint"]
        assert ctx.injected == []
        assert spawn.coros == []
        assert ctx.state.get(WATCH_STATE_KEY) is None
    finally:
        spawn.close()


def test_watch_host_only_grant_stays_denied_and_names_settings_key():
    ctx = _realish(host_grant=True)
    spawn = SpawnBox()
    try:
        payload = json.loads(
            handle_t3_watch({"kind": "shell"}, ctx=ctx, spawn=spawn, extra=True)
        )
        assert payload["watched"] is False
        assert "settings.allow_gateway_injection" in payload["hint"]
        assert ctx.injected == []
        assert spawn.coros == []
        assert ctx.state.get(WATCH_STATE_KEY) is None
    finally:
        spawn.close()


def test_watch_settings_grant_starts_the_reader():
    ctx = _realish(settings_grant=True)
    spawn = SpawnBox()
    set_ws_client_factory(lambda _ref: FakeSubClient([]))
    try:
        payload = json.loads(
            handle_t3_watch({"kind": "shell"}, ctx=ctx, spawn=spawn, extra=True)
        )
        assert payload["watched"] is True
        assert payload["environment"] == "laptop"
        assert spawn.coros
    finally:
        spawn.close()


def test_watch_with_config_grant_spawns_and_injects_salient_events():
    ctx = _granted_ctx()
    items = [
        _shell_snapshot(1, [_thread_shell(state="running")]),
        {
            "kind": "thread-upserted",
            "sequence": 2,
            "thread": _thread_shell(state="completed", approvals=True),
        },
        {
            "kind": "thread-upserted",
            "sequence": 2,
            "thread": _thread_shell(state="error", user_input=True),
        },
    ]
    fake = FakeSubClient(items)
    set_ws_client_factory(lambda _ref: fake)

    def spawn(coro, name=None):
        import asyncio

        asyncio.run(coro)
        return DummyTask()

    raw = handle_t3_watch({"kind": "shell"}, ctx=ctx, spawn=spawn, extra=True)
    payload = json.loads(raw)
    assert payload["watched"] is True
    assert payload["kind"] == "shell"
    assert payload["environment"] == "laptop"
    assert fake.tag == "orchestration.subscribeShell"
    assert fake.payload["requestCompletionMarker"] is True
    assert "afterSequence" not in fake.payload
    kinds = []
    for row in ctx.injected:
        assert row["role"] == "user"
        kinds.append(row["content"])
    joined = "\n".join(kinds)
    assert "turn settled" in joined
    assert "approval requested" in joined
    assert "user-input requested" not in joined
    blob = ctx.state.get(WATCH_STATE_KEY)
    assert blob["cursors"]["laptop:shell"] == 2
    assert fake.closed is True


def test_watch_with_settings_grant_uses_after_sequence_cursor():
    ctx = _granted_ctx()
    ctx.state.set(
        WATCH_STATE_KEY,
        {"cursors": {"laptop:thread:" + THREAD_ID: 9}, "targets": {}},
    )
    fake = FakeSubClient(
        [
            {
                "kind": "event",
                "event": {
                    "sequence": 10,
                    "type": "thread.activity-appended",
                    "payload": {
                        "threadId": THREAD_ID,
                        "activity": {"kind": "user-input.requested"},
                    },
                },
            }
        ]
    )
    set_ws_client_factory(lambda _ref: fake)

    def spawn(coro, name=None):
        import asyncio

        asyncio.run(coro)
        return DummyTask()

    raw = handle_t3_watch(
        {"kind": "thread", "thread_id": THREAD_ID},
        ctx=ctx,
        spawn=spawn,
    )
    payload = json.loads(raw)
    assert payload["watched"] is True
    assert payload["afterSequence"] == 9
    assert fake.tag == "orchestration.subscribeThread"
    assert fake.payload["threadId"] == THREAD_ID
    assert fake.payload["afterSequence"] == 9
    assert any("user-input requested" in row["content"] for row in ctx.injected)
    assert ctx.state.get(WATCH_STATE_KEY)["cursors"]["laptop:thread:" + THREAD_ID] == 10


def test_unwatch_stops_reader_and_clears_state():
    ctx = _granted_ctx()
    spawn = SpawnBox()
    set_ws_client_factory(lambda _ref: FakeSubClient([]))
    try:
        started = json.loads(
            handle_t3_watch({"kind": "shell"}, ctx=ctx, spawn=spawn)
        )
        assert started["watched"] is True
        assert "laptop:shell" in ctx.state.get(WATCH_STATE_KEY)["targets"]
        assert spawn.tasks and spawn.tasks[0].cancelled is False
        stopped = json.loads(
            handle_t3_unwatch({"kind": "shell"}, ctx=ctx, extra=True)
        )
        assert stopped["watched"] is False
        assert spawn.tasks[0].cancelled is True
        blob = ctx.state.get(WATCH_STATE_KEY)
        assert blob["targets"] == {}
        assert blob["cursors"] == {}
    finally:
        spawn.close()


def test_handlers_never_raise_on_bad_args():
    ctx = _granted_ctx()
    spawn = SpawnBox()
    try:
        payload = json.loads(handle_t3_watch({}, ctx=ctx, spawn=spawn))
        assert "error" in payload
        payload = json.loads(handle_t3_unwatch({"kind": "nope"}, ctx=ctx))
        assert "error" in payload
        payload = json.loads(
            handle_t3_watch({"kind": "thread"}, ctx=ctx, spawn=spawn)
        )
        assert "thread_id" in payload["error"]
    finally:
        spawn.close()
    assert spawn.coros == []
