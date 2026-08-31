"""WI-5: t3_environments / t3_list / t3_thread — MockTransport, golden fixtures."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from talaria.config import DISCOVERED_STATE_KEY
from talaria.t3_env import T3EnvClient
from talaria.tools import (
    OUTPUT_CHAR_LIMIT,
    bind_ctx,
    handle_t3_environments,
    handle_t3_interrupt,
    handle_t3_list,
    handle_t3_new_thread,
    handle_t3_prompt,
    handle_t3_respond,
    handle_t3_thread,
    handle_t3_wait,
    set_client_factory,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BASE = "https://t3.example.test"
OFFLINE = "https://offline.example.test"
TOKEN = "tok-test"
LAPTOP = {"base_url": BASE}


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeCtx:
    def __init__(self, settings=None, state=None, secrets=None) -> None:
        self._settings = dict(settings or {})
        self.state = dict(state or {})
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


@pytest.fixture(autouse=True)
def _reset_seams():
    bind_ctx(None)
    set_client_factory(None)
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


def _json(status: int, payload) -> httpx.Response:
    return httpx.Response(status, json=payload)


def test_import_opens_no_socket(socket_guard):
    import talaria.tools as mod  # noqa: F401

    assert callable(mod.handle_t3_list)


def test_write_stubs_remain():
    for fn in (
        handle_t3_new_thread,
        handle_t3_prompt,
        handle_t3_interrupt,
        handle_t3_respond,
        handle_t3_wait,
    ):
        payload = json.loads(fn({}, extra=True))
        assert payload["error"] == "not implemented"
        assert "hint" in payload


def test_t3_environments_golden(mock_http):
    descriptor = _load("environment_descriptor.json")
    expected = _load("t3_environments.golden.json")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/.well-known/t3/environment"
        return _json(200, descriptor)

    mock_http(handler)
    raw = handle_t3_environments({}, ctx=_ctx_authed())
    payload = json.loads(raw)
    assert payload == expected
    assert TOKEN not in raw
    assert "unexpectedField" not in raw
    assert seen[0].method == "GET"


def test_t3_environments_descriptor_failure_is_not_live(mock_http):
    ctx = FakeCtx(
        settings={
            "environments": {
                "laptop": LAPTOP,
                "studio": {"base_url": OFFLINE},
            }
        },
        secrets={"T3CODE_TOKEN_LAPTOP": TOKEN},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "offline.example.test":
            return httpx.Response(502, text="tunnel down")
        return _json(200, _load("environment_descriptor.json"))

    mock_http(handler)
    payload = json.loads(handle_t3_environments({}, ctx=ctx))
    by_name = {row["name"]: row for row in payload["environments"]}
    assert by_name["laptop"]["live"] is True
    assert by_name["laptop"]["auth"] == "ok"
    assert by_name["studio"]["live"] is False
    assert by_name["studio"]["auth"] == "expired"
    assert by_name["studio"]["label"] is None


def test_t3_environments_never_returns_token(mock_http):
    mock_http(lambda _req: _json(200, _load("environment_descriptor.json")))
    raw = handle_t3_environments({}, extra=True, ctx=_ctx_authed())
    assert TOKEN not in raw
    assert "Bearer" not in raw
    assert "T3CODE_TOKEN" not in raw


def test_t3_list_golden_includes_worktree_latest_turn_pending(mock_http):
    shell = _load("shell_snapshot.json")
    expected = _load("t3_list.golden.json")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, shell)

    mock_http(handler)
    raw = handle_t3_list({}, ctx=_ctx_authed())
    payload = json.loads(raw)
    assert payload == expected
    thread = payload["threads"][0]
    assert thread["worktreePath"].endswith("thr-fix-flaky")
    assert thread["latestTurn"]["state"] == "running"
    assert thread["hasPendingApprovals"] is True
    assert thread["hasPendingUserInput"] is False
    assert payload["threads"][1]["hasPendingUserInput"] is True
    assert payload["projects"][0]["path"] == shell["projects"][0]["workspaceRoot"]
    assert "unexpectedField" not in payload
    assert "brandNewProjectKey" not in raw
    assert TOKEN not in raw
    assert seen[0].url.path == "/api/orchestration/shell"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_t3_thread_golden_extracts_agent_output_and_page(mock_http):
    detail = _load("thread_detail.json")
    expected = _load("t3_thread.golden.json")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, detail)

    mock_http(handler)
    raw = handle_t3_thread(
        {"thread_id": "thr-fix-flaky", "before_cursor": "older-page"},
        ctx=_ctx_authed(),
    )
    payload = json.loads(raw)
    assert payload == expected
    assert payload["turns"][-1]["agent_output"] == (
        "Patched tools.py handlers and added golden tests."
    )
    assert payload["page"]["beforeCursor"] == "turn-1"
    assert "truncated" not in payload
    assert TOKEN not in raw
    assert "wireOnly" not in raw
    req = seen[0]
    assert req.url.path == "/api/orchestration/threads/thr-fix-flaky"
    assert req.url.params["turnLimit"] == "5"
    assert req.url.params["beforeCursor"] == "older-page"


def test_t3_thread_default_turn_limit_is_five(mock_http):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, _load("thread_detail.json"))

    mock_http(handler)
    handle_t3_thread({"thread_id": "thr-fix-flaky"}, ctx=_ctx_authed())
    assert seen[0].url.params["turnLimit"] == "5"
    assert "beforeCursor" not in seen[0].url.params


def test_t3_thread_explicit_turn_limit(mock_http):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, _load("thread_detail.json"))

    mock_http(handler)
    handle_t3_thread(
        {"thread_id": "thr-fix-flaky", "turn_limit": 2},
        ctx=_ctx_authed(),
    )
    assert seen[0].url.params["turnLimit"] == "2"


def test_t3_thread_oversized_sets_truncated_and_keeps_latest(mock_http):
    snapshot = deepcopy(_load("thread_oversized.json"))
    chunk = "x" * 12_000
    for msg in snapshot["thread"]["messages"]:
        if msg["role"] == "assistant":
            msg["text"] = chunk + msg["text"]
    untruncated_len = len(
        json.dumps(
            {
                "environment": "laptop",
                "id": "thr-huge",
                "turns": [
                    {"turnId": "turn-early", "agent_output": chunk},
                    {"turnId": "turn-mid", "agent_output": chunk},
                    {"turnId": "turn-late", "agent_output": chunk},
                ],
            }
        )
    )
    assert untruncated_len > OUTPUT_CHAR_LIMIT

    mock_http(lambda _req: _json(200, snapshot))
    raw = handle_t3_thread({"thread_id": "thr-huge"}, ctx=_ctx_authed())
    assert len(raw) <= OUTPUT_CHAR_LIMIT
    payload = json.loads(raw)
    assert payload["truncated"] is True
    turn_ids = [row["turnId"] for row in payload["turns"]]
    assert "turn-late" in turn_ids
    assert "turn-early" not in turn_ids
    assert payload["turns"][-1]["agent_output"].endswith("__LATE_OUTPUT__")
    assert TOKEN not in raw


def test_t3_list_without_secret_is_not_authenticated(mock_http):
    mock_http(lambda _req: _json(200, _load("shell_snapshot.json")))
    ctx = FakeCtx(settings={"environments": {"laptop": LAPTOP}}, secrets={})
    payload = json.loads(handle_t3_list({}, ctx=ctx))
    assert "not authenticated" in payload["error"]
    assert "hermes t3code login" in payload["hint"]
    assert TOKEN not in json.dumps(payload)


def test_t3_thread_without_secret_is_not_authenticated(mock_http):
    mock_http(lambda _req: _json(200, _load("thread_detail.json")))
    ctx = FakeCtx(settings={"environments": {"laptop": LAPTOP}}, secrets={})
    payload = json.loads(handle_t3_thread({"thread_id": "thr-1"}, ctx=ctx))
    assert "not authenticated" in payload["error"]


def test_t3_list_401_is_json_not_raise(mock_http):
    mock_http(lambda _req: httpx.Response(401, json={"code": "auth_invalid"}))
    raw = handle_t3_list({}, ctx=_ctx_authed())
    payload = json.loads(raw)
    assert "not authenticated" in payload["error"]
    assert TOKEN not in raw


def test_t3_list_no_environments():
    payload = json.loads(handle_t3_list({}, ctx=FakeCtx(settings={"environments": {}}, secrets={})))
    assert payload["error"] == "no environments configured"


def test_t3_thread_requires_thread_id():
    payload = json.loads(handle_t3_thread({}, ctx=_ctx_authed()))
    assert payload["error"] == "thread_id is required"


def test_handlers_accept_kwargs_ctx_without_bind(mock_http):
    mock_http(lambda _req: _json(200, _load("shell_snapshot.json")))
    bind_ctx(None)
    payload = json.loads(handle_t3_list({}, extra=True, ctx=_ctx_authed()))
    assert payload["environment"] == "laptop"


def test_bind_ctx_used_when_kwargs_omit_ctx(mock_http):
    mock_http(lambda _req: _json(200, _load("environment_descriptor.json")))
    bind_ctx(_ctx_authed())
    payload = json.loads(handle_t3_environments({}))
    assert payload["environments"][0]["live"] is True


def test_handlers_never_raise_on_unexpected(mock_http):
    def factory(_ref, _headers_fn):
        raise RuntimeError("boom-factory")

    set_client_factory(factory)
    raw = handle_t3_list({}, ctx=_ctx_authed())
    payload = json.loads(raw)
    assert "error" in payload
    assert "hint" in payload
    assert TOKEN not in raw


def test_t3_environments_unknown_fields_do_not_break_parse(mock_http):
    desc = _load("environment_descriptor.json")
    mock_http(lambda _req: _json(200, desc))
    payload = json.loads(handle_t3_environments({}, ctx=_ctx_authed()))
    assert payload["environments"][0]["label"] == desc["label"]
    assert payload["environments"][0]["environmentId"] == desc["environmentId"]


def test_t3_environments_discovered_mode_is_t3connect(mock_http):
    mock_http(lambda _req: _json(200, _load("environment_descriptor.json")))
    ctx = FakeCtx(
        settings={"environments": {}},
        state={
            DISCOVERED_STATE_KEY: {
                "studio": {
                    "base_url": BASE,
                    "environment_id": "env-studio",
                }
            }
        },
        secrets={"T3CODE_TOKEN_STUDIO": TOKEN},
    )
    payload = json.loads(handle_t3_environments({}, ctx=ctx))
    row = payload["environments"][0]
    assert row["name"] == "studio"
    assert row["mode"] == "t3connect"
    assert row["auth"] == "ok"
    assert row["live"] is True


def test_t3_list_api_error_is_json(mock_http):
    mock_http(lambda _req: httpx.Response(502, text="upstream failed"))
    raw = handle_t3_list({}, ctx=_ctx_authed())
    payload = json.loads(raw)
    assert "502" in payload["error"]
    assert "hint" in payload
    assert TOKEN not in raw
