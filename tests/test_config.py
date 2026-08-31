"""WI-2: environment resolution, secret-name mangling, secret store isolation."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from talaria.config import (
    DISCOVERED_STATE_KEY,
    EnvironmentRef,
    get_secret,
    resolve_environment,
    resolve_environments,
    secret_name_for,
    set_secret,
)
from talaria.errors import (
    EnvironmentNotFound,
    NotAuthenticated,
    T3ApiError,
    json_error,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

LAPTOP = {"base_url": "https://t3.example.test"}
STUDIO = {
    "base_url": "https://studio.example.test",
    "environment_id": "env-studio",
}


class FakeCtx:
    def __init__(self, settings=None, state=None) -> None:
        self._settings = dict(settings or {})
        self.state = dict(state or {})
        self.config_writes: list[tuple[str, object]] = []

    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def set_config(self, key, value):
        self.config_writes.append((key, value))
        self._settings[key] = value


def test_secret_name_mangles_hyphen():
    assert secret_name_for("my-laptop") == "T3CODE_TOKEN_MY_LAPTOP"


def test_secret_name_uppercases_plain_name():
    assert secret_name_for("laptop") == "T3CODE_TOKEN_LAPTOP"


def test_resolve_explicit_arg_by_name():
    ctx = FakeCtx(
        settings={
            "environments": {"laptop": LAPTOP, "studio": STUDIO},
            "default_environment": "studio",
        }
    )
    ref = resolve_environment(ctx, "laptop")
    assert ref == EnvironmentRef(name="laptop", base_url=LAPTOP["base_url"])


def test_resolve_explicit_arg_by_environment_id():
    ctx = FakeCtx(settings={"environments": {"studio": STUDIO}})
    ref = resolve_environment(ctx, "env-studio")
    assert ref.name == "studio"
    assert ref.environment_id == "env-studio"


def test_resolve_default_environment():
    ctx = FakeCtx(
        settings={
            "environments": {"laptop": LAPTOP, "studio": STUDIO},
            "default_environment": "studio",
        }
    )
    ref = resolve_environment(ctx)
    assert ref.name == "studio"


def test_resolve_default_environment_by_id():
    ctx = FakeCtx(
        settings={
            "environments": {"laptop": LAPTOP, "studio": STUDIO},
            "default_environment": "env-studio",
        }
    )
    assert resolve_environment(ctx).name == "studio"


def test_resolve_sole_available_environment():
    ctx = FakeCtx(settings={"environments": {"laptop": LAPTOP}})
    ref = resolve_environment(ctx)
    assert ref.name == "laptop"


def test_resolve_ambiguous_lists_options():
    ctx = FakeCtx(settings={"environments": {"laptop": LAPTOP, "studio": STUDIO}})
    with pytest.raises(EnvironmentNotFound) as ei:
        resolve_environment(ctx)
    payload = json.loads(ei.value.to_json())
    assert payload["error"] == "multiple environments available"
    assert "laptop" in payload["hint"]
    assert "studio" in payload["hint"]
    assert "env-studio" in payload["hint"]


def test_resolve_unknown_name_lists_options():
    ctx = FakeCtx(settings={"environments": {"laptop": LAPTOP}})
    with pytest.raises(EnvironmentNotFound) as ei:
        resolve_environment(ctx, "missing")
    payload = json.loads(ei.value.to_json())
    assert "missing" in payload["error"]
    assert "laptop" in payload["hint"]


def test_resolve_no_environments():
    ctx = FakeCtx(settings={"environments": {}})
    with pytest.raises(EnvironmentNotFound) as ei:
        resolve_environment(ctx)
    payload = json.loads(ei.value.to_json())
    assert payload["error"] == "no environments configured"
    assert "hermes t3code login" in payload["hint"]


def test_explicit_arg_wins_over_default():
    ctx = FakeCtx(
        settings={
            "environments": {"laptop": LAPTOP, "studio": STUDIO},
            "default_environment": "studio",
        }
    )
    assert resolve_environment(ctx, "laptop").name == "laptop"


def test_bad_default_does_not_fall_through_to_sole():
    ctx = FakeCtx(
        settings={
            "environments": {"laptop": LAPTOP},
            "default_environment": "ghost",
        }
    )
    with pytest.raises(EnvironmentNotFound):
        resolve_environment(ctx)


def test_discovered_environments_merge():
    ctx = FakeCtx(
        settings={"environments": {"laptop": LAPTOP}},
        state={
            DISCOVERED_STATE_KEY: {
                "studio": {
                    "base_url": STUDIO["base_url"],
                    "environment_id": "env-studio",
                }
            }
        },
    )
    envs = resolve_environments(ctx)
    assert set(envs) == {"laptop", "studio"}
    assert envs["laptop"].mode == "direct"
    assert envs["studio"].mode == "t3connect"
    assert resolve_environment(ctx, "env-studio").name == "studio"


def test_mode_a_name_wins_over_discovered():
    ctx = FakeCtx(
        settings={"environments": {"laptop": LAPTOP}},
        state={
            DISCOVERED_STATE_KEY: {
                "laptop": {
                    "base_url": "https://other.example.test",
                    "environment_id": "env-other",
                    "mode": "t3connect",
                }
            }
        },
    )
    ref = resolve_environments(ctx)["laptop"]
    assert ref.base_url == LAPTOP["base_url"]
    assert ref.mode == "direct"
    assert ref.environment_id is None


def test_get_set_secret_uses_store_not_config():
    ctx = FakeCtx(settings={"environments": {"laptop": LAPTOP}})
    store: dict[str, str] = {}
    key = secret_name_for("laptop")
    set_secret(key, "tok-test", store=store)
    assert store == {key: "tok-test"}
    assert get_secret(key, store=store) == "tok-test"
    assert ctx.config_writes == []
    assert "tok-test" not in str(ctx._settings)
    assert get_secret("T3CODE_TOKEN_NOPE", default=None, store=store) is None
    assert get_secret("T3CODE_TOKEN_NOPE", default="missing", store=store) == "missing"


def test_config_module_never_writes_via_set_config():
    source = (REPO_ROOT / "talaria/config.py").read_text(encoding="utf-8")
    assert "set_config" not in source


def test_config_module_does_not_import_hermes_at_top_level():
    tree = ast.parse((REPO_ROOT / "talaria/config.py").read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    blob = " ".join(names)
    assert "hermes" not in blob
    assert "secret_scope" not in blob
    assert "plugin_utils" not in blob


def test_json_error_contract_unchanged():
    payload = json.loads(json_error("boom", "try this"))
    assert payload == {"error": "boom", "hint": "try this"}


def test_not_authenticated_hint_names_login():
    payload = json.loads(NotAuthenticated("laptop").to_json())
    assert "not authenticated" in payload["error"]
    assert "hermes t3code login <pairing-url>" in payload["hint"]


def test_t3_api_error_carries_status_and_excerpt():
    err = T3ApiError(502, "upstream failed extra detail")
    assert err.status == 502
    payload = json.loads(err.to_json())
    assert "502" in payload["error"]
    assert "upstream failed" in payload["hint"]
