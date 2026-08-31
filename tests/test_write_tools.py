"""WI-6: write tools — dispatch payloads vs ClientOrchestrationCommand shapes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from talaria import commands
from talaria.t3_env import T3EnvClient
from talaria.tools import (
    bind_ctx,
    handle_t3_interrupt,
    handle_t3_new_thread,
    handle_t3_prompt,
    handle_t3_respond,
    handle_t3_wait,
    set_client_factory,
)

BASE = "https://t3.example.test"
TOKEN = "tok-test"
LAPTOP = {"base_url": BASE}
THREAD_ID = "thr-fix-flaky"
PROJECT_ID = "proj-hermes"
FROZEN = datetime(2026, 8, 31, 16, 20, 0, tzinfo=timezone.utc)
CREATED_AT = "2026-08-31T16:20:00.000Z"
CMD_ID = "11111111-1111-4111-8111-111111111111"
MSG_ID = "22222222-2222-4222-8222-222222222222"
NEW_THREAD_ID = "33333333-3333-4333-8333-333333333333"
DISPATCH_OK = {"sequence": 7}


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


class FrozenDateTime:
    @staticmethod
    def now(tz=None):
        return FROZEN


def freeze_clock(monkeypatch, *ids: str) -> None:
    monkeypatch.setattr(commands, "datetime", FrozenDateTime)
    it = iter(UUID(value) for value in ids)
    monkeypatch.setattr(commands, "uuid4", lambda: next(it))


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


def _json(status: int, payload) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _capture(mock_http):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, DISPATCH_OK)

    mock_http(handler)
    return seen


def _dispatched(seen: list[httpx.Request]) -> dict:
    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/api/orchestration/dispatch"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    return json.loads(req.content)


def test_import_opens_no_socket(socket_guard):
    import talaria.commands as mod  # noqa: F401
    import talaria.tools as tools  # noqa: F401

    assert callable(tools.handle_t3_prompt)


def test_plugin_runtime_mode_default_is_never_full_access():
    assert commands.DEFAULT_RUNTIME_MODE == "approval-required"
    assert commands.DEFAULT_RUNTIME_MODE != "full-access"


def test_t3_wait_stays_stub_even_with_ctx():
    payload = json.loads(handle_t3_wait({"thread_id": THREAD_ID}, extra=True, ctx=_ctx_authed()))
    assert payload["error"] == "not implemented"


def test_prompt_turn_start_matches_prd_shape(monkeypatch, mock_http):
    freeze_clock(monkeypatch, CMD_ID, MSG_ID)
    seen = _capture(mock_http)
    raw = handle_t3_prompt(
        {
            "thread_id": THREAD_ID,
            "text": "Fix the flaky test",
            "model_selection": {"instance_id": "codex", "model": "gpt-5.4"},
            "runtime_mode": "approval-required",
            "interaction_mode": "default",
        },
        extra=True,
        ctx=_ctx_authed(),
    )
    assert _dispatched(seen) == {
        "type": "thread.turn.start",
        "commandId": CMD_ID,
        "threadId": THREAD_ID,
        "message": {
            "messageId": MSG_ID,
            "role": "user",
            "text": "Fix the flaky test",
            "attachments": [],
        },
        "modelSelection": {"instanceId": "codex", "model": "gpt-5.4"},
        "runtimeMode": "approval-required",
        "interactionMode": "default",
        "createdAt": CREATED_AT,
    }
    body = json.loads(raw)
    assert body["threadId"] == THREAD_ID
    assert body["commandId"] == CMD_ID
    assert body["messageId"] == MSG_ID
    assert body["sequence"] == 7
    assert TOKEN not in raw


def test_prompt_omitted_runtime_mode_is_approval_required_attachments_empty(
    monkeypatch, mock_http
):
    freeze_clock(monkeypatch, CMD_ID, MSG_ID)
    seen = _capture(mock_http)
    handle_t3_prompt(
        {"thread_id": THREAD_ID, "text": "hello"},
        ctx=_ctx_authed(),
    )
    payload = _dispatched(seen)
    assert payload["runtimeMode"] == "approval-required"
    assert payload["runtimeMode"] != "full-access"
    assert payload["interactionMode"] == "default"
    assert payload["message"]["attachments"] == []
    assert "modelSelection" not in payload
    assert payload == {
        "type": "thread.turn.start",
        "commandId": CMD_ID,
        "threadId": THREAD_ID,
        "message": {
            "messageId": MSG_ID,
            "role": "user",
            "text": "hello",
            "attachments": [],
        },
        "runtimeMode": "approval-required",
        "interactionMode": "default",
        "createdAt": CREATED_AT,
    }


def test_prompt_explicit_full_access_is_sent_only_when_exact(monkeypatch, mock_http):
    freeze_clock(monkeypatch, CMD_ID, MSG_ID)
    seen = _capture(mock_http)
    handle_t3_prompt(
        {
            "thread_id": THREAD_ID,
            "text": "danger",
            "runtime_mode": "full-access",
            "interaction_mode": "plan",
        },
        ctx=_ctx_authed(),
    )
    payload = _dispatched(seen)
    assert payload["runtimeMode"] == "full-access"
    assert payload["interactionMode"] == "plan"


@pytest.mark.parametrize(
    "runtime_mode",
    ["full_access", "FULL-ACCESS", "danger-full-access", "auto-accept"],
)
def test_prompt_unknown_runtime_mode_is_rejected_not_dispatched(
    runtime_mode, mock_http
):
    seen = _capture(mock_http)
    raw = handle_t3_prompt(
        {
            "thread_id": THREAD_ID,
            "text": "hello",
            "runtime_mode": runtime_mode,
        },
        extra=True,
        ctx=_ctx_authed(),
    )
    payload = json.loads(raw)
    assert "unknown runtime_mode" in payload["error"]
    assert "full-access" in payload["hint"]
    assert seen == []
    assert TOKEN not in raw


def test_prompt_stripped_full_access_is_exact_literal(monkeypatch, mock_http):
    freeze_clock(monkeypatch, CMD_ID, MSG_ID)
    seen = _capture(mock_http)
    handle_t3_prompt(
        {
            "thread_id": THREAD_ID,
            "text": "hello",
            "runtime_mode": "  full-access  ",
        },
        ctx=_ctx_authed(),
    )
    assert _dispatched(seen)["runtimeMode"] == "full-access"


def test_new_thread_matches_prd_shape(monkeypatch, mock_http):
    freeze_clock(monkeypatch, CMD_ID, NEW_THREAD_ID)
    seen = _capture(mock_http)
    raw = handle_t3_new_thread(
        {
            "project_id": PROJECT_ID,
            "title": "Fix the flaky read-tools test",
            "model_selection": {"instance_id": "codex", "model": "gpt-5.4"},
        },
        extra=True,
        ctx=_ctx_authed(),
    )
    assert _dispatched(seen) == {
        "type": "thread.create",
        "commandId": CMD_ID,
        "threadId": NEW_THREAD_ID,
        "projectId": PROJECT_ID,
        "title": "Fix the flaky read-tools test",
        "modelSelection": {"instanceId": "codex", "model": "gpt-5.4"},
        "runtimeMode": "approval-required",
        "interactionMode": "default",
        "branch": None,
        "worktreePath": None,
        "createdAt": CREATED_AT,
    }
    body = json.loads(raw)
    assert body["threadId"] == NEW_THREAD_ID
    assert body["sequence"] == 7
    assert TOKEN not in raw


def test_interrupt_matches_prd_shape(monkeypatch, mock_http):
    freeze_clock(monkeypatch, CMD_ID)
    seen = _capture(mock_http)
    handle_t3_interrupt({"thread_id": THREAD_ID}, extra=True, ctx=_ctx_authed())
    payload = _dispatched(seen)
    assert payload == {
        "type": "thread.turn.interrupt",
        "commandId": CMD_ID,
        "threadId": THREAD_ID,
        "createdAt": CREATED_AT,
    }
    assert "turnId" not in payload


def test_respond_approval_matches_prd_shape(monkeypatch, mock_http):
    freeze_clock(monkeypatch, CMD_ID)
    seen = _capture(mock_http)
    handle_t3_respond(
        {
            "thread_id": THREAD_ID,
            "kind": "approval",
            "request_id": "req-approval-1",
            "decision": "accept",
        },
        extra=True,
        ctx=_ctx_authed(),
    )
    assert _dispatched(seen) == {
        "type": "thread.approval.respond",
        "commandId": CMD_ID,
        "threadId": THREAD_ID,
        "requestId": "req-approval-1",
        "decision": "accept",
        "createdAt": CREATED_AT,
    }


def test_respond_user_input_matches_prd_shape(monkeypatch, mock_http):
    freeze_clock(monkeypatch, CMD_ID)
    seen = _capture(mock_http)
    answers = {"question-1": "yes", "path": "/tmp/out"}
    handle_t3_respond(
        {
            "thread_id": THREAD_ID,
            "kind": "user-input",
            "request_id": "req-input-1",
            "answers": answers,
        },
        extra=True,
        ctx=_ctx_authed(),
    )
    assert _dispatched(seen) == {
        "type": "thread.user-input.respond",
        "commandId": CMD_ID,
        "threadId": THREAD_ID,
        "requestId": "req-input-1",
        "answers": answers,
        "createdAt": CREATED_AT,
    }


def test_respond_unknown_kind_does_not_dispatch(mock_http):
    seen = _capture(mock_http)
    payload = json.loads(
        handle_t3_respond(
            {
                "thread_id": THREAD_ID,
                "kind": "other",
                "request_id": "req-1",
            },
            ctx=_ctx_authed(),
        )
    )
    assert "unknown kind" in payload["error"]
    assert seen == []


def test_respond_unknown_decision_does_not_dispatch(mock_http):
    seen = _capture(mock_http)
    payload = json.loads(
        handle_t3_respond(
            {
                "thread_id": THREAD_ID,
                "kind": "approval",
                "request_id": "req-1",
                "decision": "allow",
            },
            ctx=_ctx_authed(),
        )
    )
    assert "unknown decision" in payload["error"]
    assert seen == []


def test_missing_auth_is_not_authenticated(mock_http):
    seen = _capture(mock_http)
    ctx = FakeCtx(settings={"environments": {"laptop": LAPTOP}}, secrets={})
    payload = json.loads(
        handle_t3_prompt({"thread_id": THREAD_ID, "text": "hi"}, ctx=ctx)
    )
    assert "not authenticated" in payload["error"]
    assert "hermes t3code login" in payload["hint"]
    assert seen == []


def test_401_is_json_not_raise(mock_http):
    mock_http(lambda _req: httpx.Response(401, json={"token": TOKEN, "code": "auth_invalid"}))
    raw = handle_t3_interrupt({"thread_id": THREAD_ID}, extra=True, ctx=_ctx_authed())
    payload = json.loads(raw)
    assert "not authenticated" in payload["error"]
    assert TOKEN not in raw


def test_handlers_never_raise_on_unexpected():
    def factory(_ref, _headers_fn):
        raise RuntimeError("boom-factory")

    set_client_factory(factory)
    raw = handle_t3_prompt(
        {"thread_id": THREAD_ID, "text": "hi"},
        extra=True,
        ctx=_ctx_authed(),
    )
    payload = json.loads(raw)
    assert "error" in payload
    assert "hint" in payload
    assert TOKEN not in raw


def test_prompt_requires_thread_id_and_text(mock_http):
    seen = _capture(mock_http)
    missing_thread = json.loads(handle_t3_prompt({"text": "hi"}, ctx=_ctx_authed()))
    missing_text = json.loads(
        handle_t3_prompt({"thread_id": THREAD_ID}, ctx=_ctx_authed())
    )
    assert missing_thread["error"] == "thread_id is required"
    assert missing_text["error"] == "text is required"
    assert seen == []


def test_new_thread_requires_model_selection(mock_http):
    seen = _capture(mock_http)
    payload = json.loads(
        handle_t3_new_thread(
            {"project_id": PROJECT_ID, "title": "x"},
            ctx=_ctx_authed(),
        )
    )
    assert payload["error"] == "model_selection is required"
    assert seen == []


def test_model_selection_instance_id_maps_to_instanceId():
    freeze = {"instance_id": "codex", "model": "gpt-5.4"}
    assert commands._model_selection(freeze, required=True) == {
        "instanceId": "codex",
        "model": "gpt-5.4",
    }
    assert commands._model_selection(
        {"instanceId": "claude", "model": "opus-4.1"},
        required=True,
    ) == {"instanceId": "claude", "model": "opus-4.1"}


def test_builders_without_http_match_wire_keys(monkeypatch):
    freeze_clock(monkeypatch, CMD_ID, MSG_ID)
    start = commands.thread_turn_start({"thread_id": THREAD_ID, "text": "hi"})
    assert set(start) == {
        "type",
        "commandId",
        "threadId",
        "message",
        "runtimeMode",
        "interactionMode",
        "createdAt",
    }
    assert set(start["message"]) == {"messageId", "role", "text", "attachments"}
    freeze_clock(monkeypatch, CMD_ID, NEW_THREAD_ID)
    created = commands.thread_create(
        {
            "project_id": PROJECT_ID,
            "title": "t",
            "model_selection": {"instance_id": "codex", "model": "gpt-5.4"},
        }
    )
    assert created["branch"] is None
    assert created["worktreePath"] is None
    assert created["modelSelection"]["instanceId"] == "codex"


def test_bind_ctx_used_when_kwargs_omit_ctx(monkeypatch, mock_http):
    freeze_clock(monkeypatch, CMD_ID)
    seen = _capture(mock_http)
    bind_ctx(_ctx_authed())
    handle_t3_interrupt({"thread_id": THREAD_ID})
    assert _dispatched(seen)["type"] == "thread.turn.interrupt"


def test_unknown_interaction_mode_rejected(mock_http):
    seen = _capture(mock_http)
    payload = json.loads(
        handle_t3_prompt(
            {
                "thread_id": THREAD_ID,
                "text": "hi",
                "interaction_mode": "agent",
            },
            ctx=_ctx_authed(),
        )
    )
    assert "unknown interaction_mode" in payload["error"]
    assert seen == []
