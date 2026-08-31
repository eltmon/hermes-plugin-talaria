"""WI-7: t3_wait — shell poll, injected clock, MockTransport sequences."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from talaria.t3_env import T3EnvClient
from talaria.tools import (
    DEFAULT_WAIT_INTERVAL,
    DEFAULT_WAIT_TIMEOUT,
    MIN_WAIT_INTERVAL,
    bind_ctx,
    handle_t3_wait,
    set_client_factory,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BASE = "https://t3.example.test"
TOKEN = "tok-test"
LAPTOP = {"base_url": BASE}
THREAD_ID = "thr-fix-flaky"


class FakeCtx:
    def __init__(self, settings=None, secrets=None) -> None:
        self._settings = dict(settings or {})
        self.secrets = dict(secrets or {})

    def get_config(self, key, default=None):
        return self._settings.get(key, default)


def _ctx_authed(**more_settings) -> FakeCtx:
    settings = {"environments": {"laptop": LAPTOP}}
    settings.update(more_settings)
    return FakeCtx(
        settings=settings,
        secrets={"T3CODE_TOKEN_LAPTOP": TOKEN},
    )


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _json(status: int, payload) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _snapshot(
    *,
    thread_id: str = THREAD_ID,
    state: str | None = "running",
    approvals: bool = False,
    user_input: bool = False,
    missing: bool = False,
    plan_step: str | None = "edit tools.py",
) -> dict:
    shell = deepcopy(_load("shell_snapshot.json"))
    if missing:
        shell["threads"] = [
            row for row in shell["threads"] if row.get("id") != thread_id
        ]
        return shell
    thread = next(row for row in shell["threads"] if row.get("id") == thread_id)
    if state is None:
        thread["latestTurn"] = None
    else:
        thread["latestTurn"]["state"] = state
        if state != "running":
            thread["latestTurn"]["completedAt"] = "2026-08-31T15:04:00.000Z"
    thread["hasPendingApprovals"] = approvals
    thread["hasPendingUserInput"] = user_input
    if plan_step is None:
        thread["planProgress"] = None
    else:
        thread["planProgress"] = {
            "step": plan_step,
            "completedSteps": 2,
            "totalSteps": 4,
        }
    return shell


@pytest.fixture(autouse=True)
def _reset_seams(monkeypatch):
    bind_ctx(None)
    set_client_factory(None)

    def boom(_seconds=None):
        raise AssertionError("time.sleep called; inject a fake clock")

    monkeypatch.setattr(time, "sleep", boom)
    monkeypatch.setattr("talaria.tools.time.sleep", boom)
    yield
    bind_ctx(None)
    set_client_factory(None)


@pytest.fixture
def mock_http():
    clients: list[httpx.Client] = []

    def install(handler) -> httpx.Client:
        http = httpx.Client(transport=httpx.MockTransport(handler), timeout=30.0)
        clients.append(http)

        def factory(ref, headers_fn):
            return T3EnvClient(ref.base_url, headers_fn, client=http)

        set_client_factory(factory)
        return http

    yield install
    for http in clients:
        http.close()
    set_client_factory(None)


def _script(mock_http, payloads):
    seen: list[httpx.Request] = []
    it = iter(payloads)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        try:
            payload = next(it)
        except StopIteration as exc:
            raise AssertionError("unexpected extra poll") from exc
        if isinstance(payload, httpx.Response):
            return payload
        return _json(200, payload)

    mock_http(handler)
    return seen


def _wait(args, clock: FakeClock, **kwargs):
    return handle_t3_wait(
        args,
        extra=True,
        ctx=kwargs.pop("ctx", _ctx_authed()),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **kwargs,
    )


def test_import_opens_no_socket(socket_guard):
    import talaria.tools as mod  # noqa: F401

    assert callable(mod.handle_t3_wait)


def test_wait_constants():
    assert DEFAULT_WAIT_INTERVAL == 5
    assert MIN_WAIT_INTERVAL == 2
    assert DEFAULT_WAIT_TIMEOUT == 300


def test_missing_ctx_stays_stub():
    payload = json.loads(handle_t3_wait({"thread_id": THREAD_ID}, extra=True))
    assert payload["error"] == "not implemented"


def test_requires_thread_id():
    clock = FakeClock()
    payload = json.loads(_wait({}, clock))
    assert payload["error"] == "thread_id is required"
    assert clock.sleeps == []


def test_settle_completed_after_running(mock_http):
    clock = FakeClock()
    seen = _script(
        mock_http,
        [
            _snapshot(state="running", approvals=False),
            _snapshot(state="running", approvals=False),
            _snapshot(state="completed", approvals=False, plan_step=None),
        ],
    )
    raw = _wait({"thread_id": THREAD_ID, "timeout": 30, "interval": 5}, clock)
    payload = json.loads(raw)
    assert payload == {
        "environment": "laptop",
        "threadId": THREAD_ID,
        "status": "settled",
        "latestTurn": {"turnId": "turn-7", "state": "completed"},
        "hasPendingApprovals": False,
        "hasPendingUserInput": False,
        "excerpt": "completed turn-7",
    }
    assert clock.sleeps == [5, 5]
    assert [req.url.path for req in seen] == [
        "/api/orchestration/shell",
        "/api/orchestration/shell",
        "/api/orchestration/shell",
    ]
    assert all(req.method == "GET" for req in seen)
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in raw
    assert "extraTurnField" not in raw
    assert "unexpectedField" not in raw


@pytest.mark.parametrize("state", ["interrupted", "error"])
def test_settle_interrupted_and_error(state, mock_http):
    clock = FakeClock()
    _script(mock_http, [_snapshot(state=state, approvals=False, plan_step=None)])
    payload = json.loads(
        _wait({"thread_id": THREAD_ID, "timeout": 10, "interval": 2}, clock)
    )
    assert payload["status"] == "settled"
    assert payload["latestTurn"]["state"] == state
    assert clock.sleeps == []


def test_approval_interrupt_returns_immediately(mock_http):
    clock = FakeClock()
    seen = _script(
        mock_http,
        [_snapshot(state="running", approvals=True, user_input=False)],
    )
    raw = _wait({"thread_id": THREAD_ID, "timeout": 30, "interval": 5}, clock)
    payload = json.loads(raw)
    assert payload["status"] == "approval"
    assert payload["latestTurn"] == {"turnId": "turn-7", "state": "running"}
    assert payload["hasPendingApprovals"] is True
    assert payload["hasPendingUserInput"] is False
    assert payload["excerpt"] == "edit tools.py"
    assert clock.sleeps == []
    assert len(seen) == 1
    assert seen[0].url.path == "/api/orchestration/shell"


def test_user_input_interrupt(mock_http):
    clock = FakeClock()
    _script(
        mock_http,
        [
            _snapshot(state="running", approvals=False, user_input=False),
            _snapshot(state="running", approvals=False, user_input=True),
        ],
    )
    payload = json.loads(
        _wait({"thread_id": THREAD_ID, "timeout": 30, "interval": 5}, clock)
    )
    assert payload["status"] == "user-input"
    assert payload["hasPendingApprovals"] is False
    assert payload["hasPendingUserInput"] is True
    assert payload["latestTurn"]["state"] == "running"
    assert clock.sleeps == [5]


def test_pending_approval_wins_over_user_input_and_settled(mock_http):
    clock = FakeClock()
    _script(
        mock_http,
        [
            _snapshot(
                state="completed",
                approvals=True,
                user_input=True,
                plan_step=None,
            )
        ],
    )
    payload = json.loads(
        _wait({"thread_id": THREAD_ID, "timeout": 10, "interval": 2}, clock)
    )
    assert payload["status"] == "approval"
    assert payload["hasPendingApprovals"] is True
    assert payload["hasPendingUserInput"] is True
    assert clock.sleeps == []


def test_timeout_while_running(mock_http):
    clock = FakeClock()
    running = _snapshot(state="running", approvals=False, user_input=False)
    seen = _script(mock_http, [running, running, running])
    raw = _wait({"thread_id": THREAD_ID, "timeout": 10, "interval": 5}, clock)
    payload = json.loads(raw)
    assert payload["status"] == "timeout"
    assert payload["latestTurn"] == {"turnId": "turn-7", "state": "running"}
    assert payload["hasPendingApprovals"] is False
    assert payload["hasPendingUserInput"] is False
    assert payload["excerpt"] == "edit tools.py"
    assert clock.sleeps == [5, 5]
    assert len(seen) == 3
    assert all(req.url.path == "/api/orchestration/shell" for req in seen)
    assert TOKEN not in raw


def test_interval_floor_is_two_seconds(mock_http):
    clock = FakeClock()
    running = _snapshot(state="running", approvals=False)
    _script(mock_http, [running, running, running, running])
    payload = json.loads(
        _wait({"thread_id": THREAD_ID, "timeout": 6, "interval": 1}, clock)
    )
    assert payload["status"] == "timeout"
    assert clock.sleeps == [2, 2, 2]


def test_default_interval_five_and_remainder_sleep(mock_http):
    clock = FakeClock()
    running = _snapshot(state="running", approvals=False)
    _script(mock_http, [running, running, running, running])
    payload = json.loads(_wait({"thread_id": THREAD_ID, "timeout": 12}, clock))
    assert payload["status"] == "timeout"
    assert clock.sleeps == [5, 5, 2]


def test_null_latest_turn_keeps_polling_until_timeout(mock_http):
    clock = FakeClock()
    empty = _snapshot(state=None, approvals=False, user_input=False, plan_step=None)
    _script(mock_http, [empty, empty, empty])
    payload = json.loads(
        _wait({"thread_id": THREAD_ID, "timeout": 4, "interval": 2}, clock)
    )
    assert payload["status"] == "timeout"
    assert payload["latestTurn"] is None
    assert payload["excerpt"] == "Fix the flaky read-tools test"
    assert clock.sleeps == [2, 2]


def test_missing_thread_keeps_polling_until_timeout(mock_http):
    clock = FakeClock()
    missing = _snapshot(missing=True)
    _script(mock_http, [missing, missing])
    payload = json.loads(
        _wait({"thread_id": THREAD_ID, "timeout": 2, "interval": 2}, clock)
    )
    assert payload["status"] == "timeout"
    assert payload["latestTurn"] is None
    assert payload["hasPendingApprovals"] is False
    assert payload["hasPendingUserInput"] is False
    assert payload["excerpt"] is None
    assert clock.sleeps == [2]


def test_does_not_poll_thread_detail(mock_http):
    clock = FakeClock()
    seen = _script(
        mock_http,
        [_snapshot(state="completed", approvals=False, plan_step=None)],
    )
    json.loads(_wait({"thread_id": THREAD_ID, "timeout": 5, "interval": 2}, clock))
    assert seen[0].url.path == "/api/orchestration/shell"
    assert "/threads/" not in str(seen[0].url)


def test_without_secret_is_not_authenticated(mock_http):
    clock = FakeClock()
    seen = _script(mock_http, [_snapshot()])
    ctx = FakeCtx(settings={"environments": {"laptop": LAPTOP}}, secrets={})
    payload = json.loads(_wait({"thread_id": THREAD_ID}, clock, ctx=ctx))
    assert "not authenticated" in payload["error"]
    assert "hermes t3code login" in payload["hint"]
    assert seen == []
    assert clock.sleeps == []


def test_401_is_json_not_raise(mock_http):
    clock = FakeClock()
    _script(
        mock_http,
        [httpx.Response(401, json={"token": TOKEN, "code": "auth_invalid"})],
    )
    raw = _wait({"thread_id": THREAD_ID, "timeout": 10, "interval": 2}, clock)
    payload = json.loads(raw)
    assert "not authenticated" in payload["error"]
    assert TOKEN not in raw
    assert clock.sleeps == []


def test_handlers_never_raise_on_unexpected():
    clock = FakeClock()

    def factory(_ref, _headers_fn):
        raise RuntimeError("boom-factory")

    set_client_factory(factory)
    raw = _wait({"thread_id": THREAD_ID, "timeout": 10}, clock)
    payload = json.loads(raw)
    assert "error" in payload
    assert "hint" in payload
    assert TOKEN not in raw
    assert clock.sleeps == []


def test_bind_ctx_used_when_kwargs_omit_ctx(mock_http):
    clock = FakeClock()
    _script(mock_http, [_snapshot(state="completed", approvals=False, plan_step=None)])
    bind_ctx(_ctx_authed())
    raw = handle_t3_wait(
        {"thread_id": THREAD_ID, "timeout": 5, "interval": 2},
        extra=True,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    payload = json.loads(raw)
    assert payload["status"] == "settled"
    assert payload["environment"] == "laptop"
    assert clock.sleeps == []
