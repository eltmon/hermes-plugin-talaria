"""Plugin settings and secret resolution.

Do not import plugins.plugin_utils, agent.secret_scope, or hermes_cli at
module import — tests run without hermes-agent. Secret reads/writes lazy-import.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from .errors import EnvironmentNotFound

# WI-9 writes a normalized map here via ctx.state.set/get (PluginState) or a
# dict on fake ctx. Shape: {name: {base_url, environment_id?, mode?}}.
DISCOVERED_STATE_KEY = "discovered_environments"

_TOKEN_PREFIX = "T3CODE_TOKEN_"
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


@dataclass(frozen=True)
class EnvironmentRef:
    name: str
    base_url: str
    environment_id: str | None = None
    mode: str = "direct"


def secret_name_for(env_name: str) -> str:
    """``my-laptop`` → ``T3CODE_TOKEN_MY_LAPTOP``."""
    slug = _NON_ALNUM.sub("_", env_name).upper()
    return f"{_TOKEN_PREFIX}{slug}"


def get_secret(
    name: str,
    default: str | None = None,
    *,
    store: Mapping[str, str] | None = None,
) -> str | None:
    """Scoped secret read; ``store`` is an in-memory stand-in for tests."""
    if store is not None:
        val = store.get(name)
        return val if val is not None else default
    try:
        from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
        from agent.secret_scope import get_secret as _scoped_get_secret
    except ImportError:
        val = os.getenv(name)
        return val if val is not None else default
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


def set_secret(
    name: str,
    value: str,
    *,
    store: MutableMapping[str, str] | None = None,
) -> None:
    """Write a secret to hermes ``~/.hermes/.env`` (or ``store`` in tests).

    Never writes plugin config, plugin-data, or the install dir.
    """
    if store is not None:
        store[name] = value
        return
    try:
        from hermes_cli.config import save_env_value
    except ImportError:
        os.environ[name] = value
        return
    save_env_value(name, value)


def resolve_environments(ctx) -> dict[str, EnvironmentRef]:
    """Mode-A config entries, plus any discovered map already in ``ctx.state``."""
    result: dict[str, EnvironmentRef] = {}
    for name, spec in _iter_env_specs(ctx.get_config("environments", {}) or {}):
        ref = _parse_ref(name, spec, default_mode="direct")
        if ref is not None:
            result[ref.name] = ref
    for name, spec in _iter_env_specs(_state_get(ctx, DISCOVERED_STATE_KEY, {}) or {}):
        ref = _parse_ref(name, spec, default_mode="t3connect")
        if ref is not None and ref.name not in result:
            result[ref.name] = ref
    return result


def resolve_environment(ctx, name: str | None = None) -> EnvironmentRef:
    """explicit arg → default_environment → sole entry → error listing options."""
    envs = resolve_environments(ctx)
    requested = _optional_str(name)
    if requested is None:
        requested = _optional_str(ctx.get_config("default_environment", None))
    if requested is not None:
        hit = _lookup(envs, requested)
        if hit is not None:
            return hit
        raise EnvironmentNotFound(requested, _option_labels(envs))
    if len(envs) == 1:
        return next(iter(envs.values()))
    raise EnvironmentNotFound(None, _option_labels(envs))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value or None


def _iter_env_specs(raw: Any):
    if not isinstance(raw, Mapping):
        return
    for name, spec in raw.items():
        key = str(name).strip()
        if key:
            yield key, spec


def _parse_ref(name: str, raw: Any, *, default_mode: str) -> EnvironmentRef | None:
    if isinstance(raw, EnvironmentRef):
        return EnvironmentRef(
            name=name,
            base_url=raw.base_url,
            environment_id=raw.environment_id,
            mode=raw.mode or default_mode,
        )
    if not isinstance(raw, Mapping):
        return None
    base_url = raw.get("base_url")
    if not isinstance(base_url, str):
        return None
    base_url = base_url.strip()
    if not base_url:
        return None
    env_id = raw.get("environment_id", raw.get("environmentId"))
    if env_id is not None:
        env_id = str(env_id).strip() or None
    mode = raw.get("mode")
    if not isinstance(mode, str) or not mode.strip():
        mode = default_mode
    else:
        mode = mode.strip()
    return EnvironmentRef(
        name=name,
        base_url=base_url,
        environment_id=env_id,
        mode=mode,
    )


def _lookup(envs: dict[str, EnvironmentRef], key: str) -> EnvironmentRef | None:
    hit = envs.get(key)
    if hit is not None:
        return hit
    for ref in envs.values():
        if ref.environment_id == key:
            return ref
    return None


def _option_labels(envs: dict[str, EnvironmentRef]) -> list[str]:
    labels = []
    for ref in envs.values():
        if ref.environment_id and ref.environment_id != ref.name:
            labels.append(f"{ref.name} ({ref.environment_id})")
        else:
            labels.append(ref.name)
    return labels


def _state_get(ctx, key: str, default: Any = None) -> Any:
    state = getattr(ctx, "state", None)
    if state is None:
        return default
    getter = getattr(state, "get", None)
    if not callable(getter):
        return default
    return getter(key, default)
