"""WI-1 scaffold: register() covers plugin.yaml tools; no sockets at load."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _declared_tools() -> list[str]:
    names: list[str] = []
    capturing = False
    for line in (REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("provides_tools:"):
            capturing = True
            continue
        if capturing:
            stripped = line.strip()
            if stripped.startswith("- "):
                names.append(stripped[2:].strip().strip("\"'"))
            elif stripped == "":
                continue
            elif not line.startswith((" ", "\t")):
                break
    return names


class FakeCtx:
    def __init__(self) -> None:
        self.tools: list[dict] = []
        self.cli: list[dict] = []
        self.unloads: list = []

    def register_tool(self, name, **kwargs):
        self.tools.append({"name": name, **kwargs})

    def register_cli_command(self, name, **kwargs):
        self.cli.append({"name": name, **kwargs})

    def on_unload(self, callback):
        self.unloads.append(callback)


def _register(socket_guard) -> tuple[object, FakeCtx]:
    import talaria

    ctx = FakeCtx()
    talaria.register(ctx)
    return talaria, ctx


def test_socket_guard_blocks_construction(socket_guard):
    import socket as sock

    try:
        sock.socket()
    except Exception as exc:
        assert "socket" in str(exc).lower()
    else:
        raise AssertionError("socket.socket() should have been blocked")


def test_register_every_declared_tool(socket_guard):
    """AC1: fake ctx receives every plugin.yaml provides_tools name."""
    _mod, ctx = _register(socket_guard)
    declared = _declared_tools()
    assert declared, "plugin.yaml provides_tools was empty or unparsable"
    registered = [row["name"] for row in ctx.tools]
    assert registered == declared
    for row in ctx.tools:
        assert row.get("toolset") == "t3code"
        schema = row["schema"]
        assert schema["name"] == row["name"]
        assert schema["description"].strip()
        props = schema["parameters"]["properties"]
        assert "environment" in props
        assert props["environment"]["type"] == "string"


def test_import_and_register_open_no_socket(socket_guard):
    """AC2: import talaria and register(ctx) construct no sockets."""
    _register(socket_guard)


def test_register_t3code_cli(socket_guard):
    _mod, ctx = _register(socket_guard)
    assert [row["name"] for row in ctx.cli] == ["t3code"]
    setup_fn = ctx.cli[0]["setup_fn"]
    handler_fn = ctx.cli[0]["handler_fn"]
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers()
    t3 = subs.add_parser("t3code")
    setup_fn(t3)
    for sub in ("login", "connect", "environments", "logout"):
        ns = parser.parse_args(["t3code", sub])
        assert ns.t3code_command == sub
        assert handler_fn(ns) == 0


def test_stub_handlers_return_json_error(socket_guard):
    _mod, ctx = _register(socket_guard)
    for row in ctx.tools:
        payload = json.loads(row["handler"]({}, extra=True))
        assert payload["error"] == "not implemented"
        assert "hint" in payload


def test_directory_loader_root_register_reexport(socket_guard):
    """Doctor/directory loader imports repo-root __init__.py, not the pip package."""
    import importlib.util

    module_name = "hermes_plugins_test_talaria"
    init_file = REPO_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        module_name,
        init_file,
        submodule_search_locations=[str(REPO_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(REPO_ROOT)]
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        ctx = FakeCtx()
        module.register(ctx)
        assert [row["name"] for row in ctx.tools] == _declared_tools()
    finally:
        for name in list(sys.modules):
            if name == module_name or name.startswith(module_name + "."):
                del sys.modules[name]
