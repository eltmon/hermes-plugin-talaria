"""Talaria — Hermes plugin (id t3code) that drives remote T3 Code environments."""

from __future__ import annotations

from .cli import register_cli
from .schemas import (
    T3_ENVIRONMENTS_SCHEMA,
    T3_INTERRUPT_SCHEMA,
    T3_LIST_SCHEMA,
    T3_NEW_THREAD_SCHEMA,
    T3_PROMPT_SCHEMA,
    T3_RESPOND_SCHEMA,
    T3_THREAD_SCHEMA,
    T3_WAIT_SCHEMA,
)
from .tools import (
    bind_ctx,
    handle_t3_environments,
    handle_t3_interrupt,
    handle_t3_list,
    handle_t3_new_thread,
    handle_t3_prompt,
    handle_t3_respond,
    handle_t3_thread,
    handle_t3_wait,
)

_TOOLS = (
    ("t3_environments", T3_ENVIRONMENTS_SCHEMA, handle_t3_environments),
    ("t3_list", T3_LIST_SCHEMA, handle_t3_list),
    ("t3_thread", T3_THREAD_SCHEMA, handle_t3_thread),
    ("t3_new_thread", T3_NEW_THREAD_SCHEMA, handle_t3_new_thread),
    ("t3_prompt", T3_PROMPT_SCHEMA, handle_t3_prompt),
    ("t3_interrupt", T3_INTERRUPT_SCHEMA, handle_t3_interrupt),
    ("t3_respond", T3_RESPOND_SCHEMA, handle_t3_respond),
    ("t3_wait", T3_WAIT_SCHEMA, handle_t3_wait),
)


def _on_unload() -> None:
    """Drop bound ctx; later items close HTTP/WS clients here."""
    bind_ctx(None)


def register(ctx) -> None:
    """Register stub tools and ``hermes t3code`` CLI. No network I/O."""
    bind_ctx(ctx)
    for name, schema, handler in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="t3code",
            schema=schema,
            handler=handler,
        )
    register_cli(ctx)
    ctx.on_unload(_on_unload)
