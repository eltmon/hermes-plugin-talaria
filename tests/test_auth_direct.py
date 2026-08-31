"""WI-4: Mode A pairing login — MockTransport only, token never in config."""

from __future__ import annotations

import argparse
import json
from urllib.parse import parse_qs

import httpx
import pytest

from talaria.auth_direct import (
    PairingUrlError,
    login,
    logout,
    parse_pairing_url,
    slugify_label,
)
from talaria.cli import register_cli, t3code_command
from talaria.config import secret_name_for
from talaria.errors import NotAuthenticated
from talaria.t3_env import STANDARD_SCOPE

ACCESS = "tok-test"
PAIR = "tok-pair"
DIRECT_ORIGIN = "https://remote.example.com/"
HOSTED_ORIGIN = "https://desktop.tailnet.ts.net:44342/"
HASH_URL = "https://remote.example.com/pair#token=tok-pair"
QUERY_URL = "https://remote.example.com/pair?token=tok-pair"
HOSTED_URL = (
    "https://app.t3.codes/pair"
    "?host=https%3A%2F%2Fdesktop.tailnet.ts.net%3A44342%2F"
    "#token=tok-pair"
)
DESCRIPTOR = {
    "environmentId": "env-1",
    "label": "My Laptop",
    "capabilities": [],
    "sessionMethods": ["bearer-access-token"],
    "unexpectedField": {"nested": True},
}
EXCHANGE = {
    "access_token": ACCESS,
    "token_type": "Bearer",
    "expires_in": 2592000,
    "scope": STANDARD_SCOPE,
    "unexpectedField": "keep-me",
}


class FakeCtx:
    def __init__(self, settings=None) -> None:
        self._settings = dict(settings or {})
        self.config_writes: list[tuple[str, object]] = []
        self.cli: list[dict] = []

    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def set_config(self, key, value):
        self.config_writes.append((key, value))
        self._settings[key] = value

    def register_cli_command(self, name, **kwargs):
        self.cli.append({"name": name, **kwargs})


@pytest.fixture
def make_http():
    clients: list[httpx.Client] = []

    def factory(handler) -> httpx.Client:
        http = httpx.Client(transport=httpx.MockTransport(handler), timeout=30.0)
        clients.append(http)
        return http

    yield factory
    for http in clients:
        http.close()


def _json(status: int, payload) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _handler(seen, *, descriptor=DESCRIPTOR, exchange=EXCHANGE):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/.well-known/t3/environment":
            return _json(200, descriptor)
        if path == "/oauth/token":
            return _json(200, exchange)
        return httpx.Response(404, text="missing")

    return handler


def _config_blob(ctx: FakeCtx) -> str:
    return json.dumps(ctx._settings) + json.dumps(
        [value for _key, value in ctx.config_writes]
    )


def test_import_opens_no_socket(socket_guard):
    import talaria.auth_direct as mod  # noqa: F401
    import talaria.cli as cli_mod  # noqa: F401

    assert callable(mod.parse_pairing_url)
    assert callable(cli_mod.register_cli)


def test_parse_hash_token():
    target = parse_pairing_url(HASH_URL)
    assert target.credential == PAIR
    assert target.http_base_url == DIRECT_ORIGIN


def test_parse_legacy_query_token():
    target = parse_pairing_url(QUERY_URL)
    assert target.credential == PAIR
    assert target.http_base_url == DIRECT_ORIGIN


def test_parse_hosted_host_and_hash_token():
    target = parse_pairing_url(HOSTED_URL)
    assert target.credential == PAIR
    assert target.http_base_url == HOSTED_ORIGIN


def test_parse_hosted_protocol_relative_host():
    url = "https://app.t3.codes/pair?host=%2F%2Fremote.example.com#token=tok-pair"
    target = parse_pairing_url(url)
    assert target.http_base_url == DIRECT_ORIGIN
    assert target.credential == PAIR


def test_parse_prefers_hash_token_over_query():
    url = "https://remote.example.com/pair?token=tok-query#token=tok-pair"
    assert parse_pairing_url(url).credential == PAIR


def test_parse_ws_pairing_url_uses_http_origin():
    target = parse_pairing_url("ws://remote.example.com/pair#token=tok-pair")
    assert target.http_base_url == "http://remote.example.com/"


def test_parse_missing_token():
    with pytest.raises(PairingUrlError, match="missing its token"):
        parse_pairing_url("https://remote.example.com/pair")


def test_parse_unsupported_protocol():
    with pytest.raises(PairingUrlError, match="invalid"):
        parse_pairing_url("ftp://remote.example.com/pair#token=tok-pair")


def test_parse_hosted_unsupported_backend_protocol():
    url = (
        "https://app.t3.codes/pair?host=ftp%3A%2F%2Fremote.example.com"
        "#token=tok-pair"
    )
    with pytest.raises(PairingUrlError, match="invalid"):
        parse_pairing_url(url)


def test_slugify_label_collapses_non_alnum():
    assert slugify_label("My Laptop") == "my-laptop"
    assert slugify_label("Hello---World!!") == "hello-world"
    assert slugify_label("foo_bar") == "foo-bar"


def test_login_hash_url_stores_secret_not_config(make_http):
    seen: list[httpx.Request] = []
    ctx = FakeCtx(settings={"environments": {"studio": {"base_url": "https://studio.example.test"}}})
    store: dict[str, str] = {}
    http = make_http(_handler(seen))

    result = login(HASH_URL, ctx=ctx, store=store, client=http)

    assert result.name == "my-laptop"
    assert result.base_url == DIRECT_ORIGIN
    assert result.scope == STANDARD_SCOPE
    assert result.expires_in == 2592000
    assert store == {secret_name_for("my-laptop"): ACCESS}
    assert ctx._settings["environments"]["my-laptop"] == {"base_url": DIRECT_ORIGIN}
    assert ctx._settings["environments"]["studio"]["base_url"] == "https://studio.example.test"
    blob = _config_blob(ctx)
    assert ACCESS not in blob
    assert PAIR not in blob
    assert seen[0].url.path == "/.well-known/t3/environment"
    assert seen[0].url.host == "remote.example.com"
    assert "authorization" not in seen[0].headers
    assert seen[1].url.path == "/oauth/token"
    form = parse_qs(seen[1].content.decode())
    assert form["subject_token"] == [PAIR]


def test_login_hosted_url_hits_host_origin_not_app(make_http):
    seen: list[httpx.Request] = []
    ctx = FakeCtx()
    store: dict[str, str] = {}
    http = make_http(_handler(seen))

    result = login(HOSTED_URL, ctx=ctx, store=store, client=http)

    assert result.base_url == HOSTED_ORIGIN
    assert store[secret_name_for("my-laptop")] == ACCESS
    assert ctx._settings["environments"]["my-laptop"] == {"base_url": HOSTED_ORIGIN}
    hosts = {req.url.host for req in seen}
    assert hosts == {"desktop.tailnet.ts.net"}
    assert all(req.url.port == 44342 for req in seen)
    assert "app.t3.codes" not in _config_blob(ctx)
    assert ACCESS not in _config_blob(ctx)


def test_login_legacy_query_token(make_http):
    seen: list[httpx.Request] = []
    ctx = FakeCtx()
    store: dict[str, str] = {}
    result = login(QUERY_URL, ctx=ctx, store=store, client=make_http(_handler(seen)))
    assert result.name == "my-laptop"
    form = parse_qs(seen[1].content.decode())
    assert form["subject_token"] == [PAIR]
    assert store[secret_name_for("my-laptop")] == ACCESS


def test_login_name_flag_wins_over_descriptor_label(make_http):
    ctx = FakeCtx()
    store: dict[str, str] = {}
    result = login(
        HASH_URL,
        name="work-machine",
        ctx=ctx,
        store=store,
        client=make_http(_handler([])),
    )
    assert result.name == "work-machine"
    assert secret_name_for("my-laptop") not in store
    assert store[secret_name_for("work-machine")] == ACCESS
    assert "work-machine" in ctx._settings["environments"]
    assert "my-laptop" not in ctx._settings["environments"]


def test_login_tolerates_unknown_descriptor_fields(make_http):
    ctx = FakeCtx()
    store: dict[str, str] = {}
    login(HASH_URL, ctx=ctx, store=store, client=make_http(_handler([])))
    assert store[secret_name_for("my-laptop")] == ACCESS


def test_login_empty_label_falls_back_to_environment_id(make_http):
    ctx = FakeCtx()
    store: dict[str, str] = {}
    descriptor = {**DESCRIPTOR, "label": "???"}
    result = login(
        HASH_URL,
        ctx=ctx,
        store=store,
        client=make_http(_handler([], descriptor=descriptor)),
    )
    assert result.name == "env-1"
    assert store[secret_name_for("env-1")] == ACCESS


def test_login_does_not_store_on_exchange_failure(make_http):
    ctx = FakeCtx()
    store: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/t3/environment":
            return _json(200, DESCRIPTOR)
        return httpx.Response(401, json={"error": "invalid"})

    with pytest.raises(NotAuthenticated):
        login(HASH_URL, ctx=ctx, store=store, client=make_http(handler))
    assert store == {}
    assert ctx._settings.get("environments") in (None, {})
    assert ACCESS not in _config_blob(ctx)


def test_logout_removes_secret_and_environment_entry(make_http):
    ctx = FakeCtx(
        settings={
            "environments": {
                "my-laptop": {"base_url": DIRECT_ORIGIN},
                "studio": {"base_url": "https://studio.example.test"},
            }
        }
    )
    store = {secret_name_for("my-laptop"): ACCESS, secret_name_for("studio"): "tok-other"}
    logout("my-laptop", ctx=ctx, store=store)
    assert secret_name_for("my-laptop") not in store
    assert store[secret_name_for("studio")] == "tok-other"
    assert "my-laptop" not in ctx._settings["environments"]
    assert "studio" in ctx._settings["environments"]
    assert ACCESS not in _config_blob(ctx)


def test_cli_missing_pairing_url_returns_zero(capsys):
    ctx = FakeCtx()
    register_cli(ctx)
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers()
    t3 = subs.add_parser("t3code")
    ctx.cli[0]["setup_fn"](t3)
    ns = parser.parse_args(["t3code", "login"])
    assert ctx.cli[0]["handler_fn"](ns) == 0
    err = capsys.readouterr().err
    assert "pairing-url" in err
    assert ACCESS not in err


def test_cli_login_prints_scope_and_expiry_not_token(make_http, capsys):
    ctx = FakeCtx()
    store: dict[str, str] = {}
    ns = argparse.Namespace(t3code_command="login", url=HASH_URL, name=None)
    code = t3code_command(ns, ctx, store=store, client=make_http(_handler([])))
    assert code == 0
    captured = capsys.readouterr()
    out = captured.out
    err = captured.err
    assert "my-laptop" in out
    assert STANDARD_SCOPE in out
    assert "2592000" in out
    assert ACCESS not in out
    assert ACCESS not in err
    assert PAIR not in out
    assert PAIR not in err
    assert store[secret_name_for("my-laptop")] == ACCESS
    assert ACCESS not in _config_blob(ctx)


def test_cli_login_name_flag(make_http):
    ctx = FakeCtx()
    store: dict[str, str] = {}
    ns = argparse.Namespace(t3code_command="login", url=HASH_URL, name="desk")
    assert t3code_command(ns, ctx, store=store, client=make_http(_handler([]))) == 0
    assert store[secret_name_for("desk")] == ACCESS
    assert ctx._settings["environments"]["desk"] == {"base_url": DIRECT_ORIGIN}


def test_cli_logout_missing_env_returns_zero(capsys):
    ctx = FakeCtx()
    ns = argparse.Namespace(t3code_command="logout", env=None)
    assert t3code_command(ns, ctx) == 0
    assert "logout" in capsys.readouterr().err


def test_cli_logout(make_http, capsys):
    ctx = FakeCtx(settings={"environments": {"desk": {"base_url": DIRECT_ORIGIN}}})
    store = {secret_name_for("desk"): ACCESS}
    ns = argparse.Namespace(t3code_command="logout", env="desk")
    assert t3code_command(ns, ctx, store=store) == 0
    assert store == {}
    assert ctx._settings["environments"] == {}
    assert ACCESS not in capsys.readouterr().out
