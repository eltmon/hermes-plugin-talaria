"""WI-11: remote file tools — fixture RPCs, fake WS client, no live T3."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from talaria.t3_env import T3EnvClient
from talaria.t3_ws import RpcExitFailure
from talaria.tools import (
    READ_BYTE_LIMIT,
    bind_ctx,
    handle_t3_ls,
    handle_t3_read_file,
    handle_t3_search,
    handle_t3_write_file,
    set_client_factory,
    set_ws_client_factory,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FILES = FIXTURES / "files"
BASE = "https://t3.example.test"
TOKEN = "tok-test"
LAPTOP = {"base_url": BASE}
PROJECT_ID = "proj-hermes"
CWD = "/Users/eltmon/Projects/hermes-plugin-talaria"
INJECTED_CWD = "/tmp/demo"

LS_TAG = "projects.listEntries"
READ_TAG = "projects.readFile"
WRITE_TAG = "projects.writeFile"
SEARCH_TAG = "projects.searchContents"

HANDLERS = (
    handle_t3_ls,
    handle_t3_read_file,
    handle_t3_write_file,
    handle_t3_search,
)


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _file_fix(name: str):
    return json.loads((FILES / name).read_text(encoding="utf-8"))


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


class FakeWs:
    """Canned unary Effect RPC peer. No sockets."""

    def __init__(self, values=None, errors=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.values = dict(values or {})
        self.errors = dict(errors or {})
        self.closed = False

    async def request(self, tag: str, payload=None, **_kwargs):
        self.calls.append((tag, dict(payload or {})))
        if tag in self.errors:
            raise self.errors[tag]
        if tag in self.values:
            value = self.values[tag]
            if callable(value):
                return value(payload)
            return json.loads(json.dumps(value))
        return {}

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_seams():
    bind_ctx(None)
    set_client_factory(None)
    set_ws_client_factory(None)
    yield
    bind_ctx(None)
    set_client_factory(None)
    set_ws_client_factory(None)


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


def _install_ws(values=None, errors=None) -> FakeWs:
    fake = FakeWs(values=values, errors=errors)
    set_ws_client_factory(lambda _ref: fake)
    return fake


def _authz_failure(fixture_name: str) -> RpcExitFailure:
    return RpcExitFailure(_file_fix(fixture_name))


def _call_ls(**more):
    kwargs = {"ctx": _ctx_authed(), "cwd": INJECTED_CWD, "extra": True}
    kwargs.update(more)
    return handle_t3_ls({"project_id": PROJECT_ID}, **kwargs)


def _call_read(path="README.md", **more):
    kwargs = {"ctx": _ctx_authed(), "cwd": INJECTED_CWD, "extra": True}
    kwargs.update(more)
    return handle_t3_read_file({"project_id": PROJECT_ID, "path": path}, **kwargs)


def _call_write(path="notes.txt", contents="hi\n", **more):
    kwargs = {"ctx": _ctx_authed(), "cwd": INJECTED_CWD, "extra": True}
    kwargs.update(more)
    return handle_t3_write_file(
        {"project_id": PROJECT_ID, "path": path, "contents": contents},
        **kwargs,
    )


def _call_search(query="Talaria", **more):
    kwargs = {"ctx": _ctx_authed(), "cwd": INJECTED_CWD, "extra": True}
    kwargs.update(more)
    args = {"project_id": PROJECT_ID, "query": query}
    extra_args = more.pop("args", None)
    if extra_args:
        args.update(extra_args)
    return handle_t3_search(args, **kwargs)


def test_import_opens_no_socket(socket_guard):
    import talaria.tools as mod

    assert callable(mod.handle_t3_ls)
    assert callable(mod.handle_t3_read_file)
    assert callable(mod.handle_t3_write_file)
    assert callable(mod.handle_t3_search)


def test_missing_ctx_is_stub_json():
    for fn in HANDLERS:
        payload = json.loads(fn({}, extra=True))
        assert payload["error"] == "not implemented"
        assert "hint" in payload


def test_ls_fixture_rpc_payload_and_entries():
    value = _file_fix("list_entries.success.json")
    fake = _install_ws({LS_TAG: value})
    raw = _call_ls()
    payload = json.loads(raw)
    assert fake.calls == [(LS_TAG, {"cwd": INJECTED_CWD})]
    assert payload["entries"] == value["entries"]
    assert payload["truncated"] is False
    assert payload["unexpectedField"] == "keep"
    assert payload["environment"] == "laptop"
    assert payload["projectId"] == PROJECT_ID
    assert TOKEN not in raw
    assert fake.closed is True


def test_read_fixture_rpc_payload_and_contents():
    value = _file_fix("read_file.success.json")
    fake = _install_ws({READ_TAG: value})
    raw = _call_read()
    payload = json.loads(raw)
    assert fake.calls == [
        (READ_TAG, {"cwd": INJECTED_CWD, "relativePath": "README.md"})
    ]
    assert payload["relativePath"] == "README.md"
    assert payload["contents"] == "# Talaria\n"
    assert payload["byteLength"] == 10
    assert payload["truncated"] is False
    assert payload["serverExtra"] is True
    assert TOKEN not in raw


def test_write_fixture_rpc_payload_and_relative_path():
    value = _file_fix("write_file.success.json")
    fake = _install_ws({WRITE_TAG: value})
    raw = _call_write()
    payload = json.loads(raw)
    assert fake.calls == [
        (
            WRITE_TAG,
            {
                "cwd": INJECTED_CWD,
                "relativePath": "notes.txt",
                "contents": "hi\n",
            },
        )
    ]
    assert payload["relativePath"] == "notes.txt"
    assert payload["environment"] == "laptop"
    assert TOKEN not in raw


def test_search_fixture_rpc_payload_and_matches():
    value = _file_fix("search_contents.success.json")
    fake = _install_ws({SEARCH_TAG: value})
    raw = _call_search()
    payload = json.loads(raw)
    assert fake.calls == [
        (
            SEARCH_TAG,
            {
                "cwd": INJECTED_CWD,
                "query": "Talaria",
                "limit": 50,
                "caseSensitive": False,
                "wholeWord": False,
                "useRegex": False,
            },
        )
    ]
    assert payload["matches"] == value["matches"]
    assert payload["truncated"] is False
    assert TOKEN not in raw


def test_search_flags_and_limit_map_to_wire():
    fake = _install_ws({SEARCH_TAG: _file_fix("search_contents.success.json")})
    handle_t3_search(
        {
            "project_id": PROJECT_ID,
            "query": " foo",
            "limit": 12,
            "case_sensitive": True,
            "whole_word": True,
            "use_regex": True,
        },
        extra=True,
        ctx=_ctx_authed(),
        cwd=INJECTED_CWD,
    )
    assert fake.calls[0][1] == {
        "cwd": INJECTED_CWD,
        "query": " foo",
        "limit": 12,
        "caseSensitive": True,
        "wholeWord": True,
        "useRegex": True,
    }


def test_search_limit_caps_at_500():
    fake = _install_ws({SEARCH_TAG: {"matches": [], "truncated": True}})
    handle_t3_search(
        {"project_id": PROJECT_ID, "query": "x", "limit": 9999},
        ctx=_ctx_authed(),
        cwd=INJECTED_CWD,
    )
    assert fake.calls[0][1]["limit"] == 500


def test_ls_resolves_cwd_from_shell(mock_http):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, _load("shell_snapshot.json"))

    mock_http(handler)
    fake = _install_ws({LS_TAG: _file_fix("list_entries.success.json")})
    raw = handle_t3_ls(
        {"project_id": PROJECT_ID},
        extra=True,
        ctx=_ctx_authed(),
    )
    assert seen[0].url.path == "/api/orchestration/shell"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert fake.calls[0][1]["cwd"] == CWD
    assert json.loads(raw)["entries"][0]["path"] == "README.md"


def test_ls_optional_path_joins_cwd():
    fake = _install_ws({LS_TAG: _file_fix("list_entries.success.json")})
    handle_t3_ls(
        {"project_id": PROJECT_ID, "path": "talaria"},
        ctx=_ctx_authed(),
        cwd=INJECTED_CWD,
    )
    assert fake.calls[0][1]["cwd"] == f"{INJECTED_CWD}/talaria"


def test_read_over_cap_sets_truncated():
    blob = "x" * (READ_BYTE_LIMIT + 50)
    fake = _install_ws(
        {
            READ_TAG: {
                "relativePath": "big.txt",
                "contents": blob,
                "byteLength": len(blob.encode("utf-8")),
                "truncated": False,
            }
        }
    )
    raw = _call_read("big.txt")
    payload = json.loads(raw)
    assert payload["truncated"] is True
    assert len(payload["contents"].encode("utf-8")) <= READ_BYTE_LIMIT
    assert payload["byteLength"] == len(blob.encode("utf-8"))
    assert fake.calls[0][0] == READ_TAG


@pytest.mark.parametrize(
    ("call", "tag", "scope_fix", "scope"),
    [
        (lambda: _call_ls(), LS_TAG, "authz_read.json", "orchestration:read"),
        (lambda: _call_read(), READ_TAG, "authz_read.json", "orchestration:read"),
        (lambda: _call_search(), SEARCH_TAG, "authz_read.json", "orchestration:read"),
        (lambda: _call_write(), WRITE_TAG, "authz_write.json", "orchestration:operate"),
    ],
)
def test_authorization_error_names_missing_scope(call, tag, scope_fix, scope):
    fake = _install_ws(errors={tag: _authz_failure(scope_fix)})
    raw = call()
    payload = json.loads(raw)
    assert payload["requiredScope"] == scope
    assert scope in payload["error"]
    assert "scope" in payload["hint"]
    assert TOKEN not in raw
    assert fake.calls[0][0] == tag
    # Cause list form (Effect Sequential) is also named.
    fake_list = _install_ws(
        errors={tag: RpcExitFailure([_file_fix(scope_fix)])}
    )
    listed = json.loads(call())
    assert listed["requiredScope"] == scope
    assert fake_list.calls[0][0] == tag


def test_write_requires_project_id_and_path():
    fake = _install_ws({WRITE_TAG: _file_fix("write_file.success.json")})
    missing_project = json.loads(
        handle_t3_write_file(
            {"path": "notes.txt", "contents": "x"},
            ctx=_ctx_authed(),
            cwd=INJECTED_CWD,
        )
    )
    missing_path = json.loads(
        handle_t3_write_file(
            {"project_id": PROJECT_ID, "contents": "x"},
            ctx=_ctx_authed(),
            cwd=INJECTED_CWD,
        )
    )
    missing_contents = json.loads(
        handle_t3_write_file(
            {"project_id": PROJECT_ID, "path": "notes.txt"},
            ctx=_ctx_authed(),
            cwd=INJECTED_CWD,
        )
    )
    assert missing_project["error"] == "project_id is required"
    assert missing_path["error"] == "path is required"
    assert missing_contents["error"] == "contents is required"
    assert fake.calls == []


def test_read_requires_path():
    fake = _install_ws({READ_TAG: _file_fix("read_file.success.json")})
    payload = json.loads(
        handle_t3_read_file(
            {"project_id": PROJECT_ID},
            ctx=_ctx_authed(),
            cwd=INJECTED_CWD,
        )
    )
    assert payload["error"] == "path is required"
    assert fake.calls == []


def test_ls_unknown_project_from_shell(mock_http):
    mock_http(lambda _req: _json(200, _load("shell_snapshot.json")))
    fake = _install_ws({LS_TAG: _file_fix("list_entries.success.json")})
    payload = json.loads(
        handle_t3_ls({"project_id": "proj-missing"}, extra=True, ctx=_ctx_authed())
    )
    assert "proj-missing" in payload["error"]
    assert fake.calls == []


def test_missing_auth_is_not_authenticated(mock_http):
    mock_http(lambda _req: _json(200, _load("shell_snapshot.json")))
    ctx = FakeCtx(settings={"environments": {"laptop": LAPTOP}}, secrets={})
    payload = json.loads(handle_t3_ls({"project_id": PROJECT_ID}, ctx=ctx))
    assert "not authenticated" in payload["error"]
    assert "hermes t3code login" in payload["hint"]


def test_handlers_never_raise_on_ws_boom():
    def factory(_ref):
        raise RuntimeError("boom-ws")

    set_ws_client_factory(factory)
    raw = _call_ls()
    payload = json.loads(raw)
    assert "error" in payload
    assert "hint" in payload
    assert TOKEN not in raw


def test_project_rpc_error_is_json():
    fake = _install_ws(
        errors={
            READ_TAG: RpcExitFailure(
                {
                    "_tag": "Fail",
                    "error": {
                        "_tag": "ProjectReadFileError",
                        "message": "Failed to read workspace file 'nope' in '/tmp/demo'.",
                        "failure": "path_not_file",
                    },
                }
            )
        }
    )
    payload = json.loads(_call_read("nope"))
    assert "Failed to read" in payload["error"]
    assert "ProjectReadFileError" in payload["hint"]
    assert fake.calls[0][1]["relativePath"] == "nope"


def test_bind_ctx_used_when_kwargs_omit_ctx():
    fake = _install_ws({LS_TAG: _file_fix("list_entries.success.json")})
    bind_ctx(_ctx_authed())
    payload = json.loads(handle_t3_ls({"project_id": PROJECT_ID}, cwd=INJECTED_CWD))
    assert payload["environment"] == "laptop"
    assert fake.calls[0][0] == LS_TAG


def test_search_requires_query():
    fake = _install_ws({SEARCH_TAG: _file_fix("search_contents.success.json")})
    payload = json.loads(
        handle_t3_search(
            {"project_id": PROJECT_ID},
            ctx=_ctx_authed(),
            cwd=INJECTED_CWD,
        )
    )
    assert payload["error"] == "query is required"
    assert fake.calls == []
