"""Talaria — Hermes plugin (id t3code) that drives remote T3 Code environments."""

from __future__ import annotations

from .cli import register_cli
from .schemas import (
    T3_ENVIRONMENTS_SCHEMA,
    T3_INTERRUPT_SCHEMA,
    T3_LIST_SCHEMA,
    T3_LS_SCHEMA,
    T3_NEW_THREAD_SCHEMA,
    T3_PROMPT_SCHEMA,
    T3_READ_FILE_SCHEMA,
    T3_RESPOND_SCHEMA,
    T3_SEARCH_SCHEMA,
    T3_THREAD_SCHEMA,
    T3_UNWATCH_SCHEMA,
    T3_WAIT_SCHEMA,
    T3_WATCH_SCHEMA,
    T3_WRITE_FILE_SCHEMA,
)
from .tools import (
    bind_ctx,
    handle_t3_environments,
    handle_t3_interrupt,
    handle_t3_list,
    handle_t3_ls,
    handle_t3_new_thread,
    handle_t3_prompt,
    handle_t3_read_file,
    handle_t3_respond,
    handle_t3_search,
    handle_t3_thread,
    handle_t3_unwatch,
    handle_t3_wait,
    handle_t3_watch,
    handle_t3_write_file,
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
    ("t3_ls", T3_LS_SCHEMA, handle_t3_ls),
    ("t3_read_file", T3_READ_FILE_SCHEMA, handle_t3_read_file),
    ("t3_write_file", T3_WRITE_FILE_SCHEMA, handle_t3_write_file),
    ("t3_search", T3_SEARCH_SCHEMA, handle_t3_search),
    ("t3_watch", T3_WATCH_SCHEMA, handle_t3_watch),
    ("t3_unwatch", T3_UNWATCH_SCHEMA, handle_t3_unwatch),
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
