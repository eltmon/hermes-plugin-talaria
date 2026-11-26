"""Protocol-2 projections at the T3 boundary; tools retain their stable API.

Wire contract: T3 v0.1.10, commit 7d32075e3757ded707914e2450c3e392a9665765.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from .errors import TalariaError

LIVE_STATES = frozenset({"queued", "preparing", "starting", "running", "waiting"})
RESPONDABLE_KINDS = frozenset({"command", "file-read", "file-change", "mcp-elicitation", "permission", "user_input"})


def turn_state(status: Any) -> str | None:
    if status in LIVE_STATES:
        return "running"
    return {"completed": "completed", "failed": "error", "cancelled": "interrupted",
            "interrupted": "interrupted", "rolled_back": "interrupted"}.get(status)


def shell_thread(raw: dict) -> dict:
    out = dict(raw)
    pending = raw.get("pendingRuntimeRequest")
    pending = pending if isinstance(pending, dict) else None
    state = turn_state(raw.get("status"))
    out.update({
        "latestTurn": {"turnId": raw.get("latestRunId"), "state": state} if state else None,
        "hasPendingApprovals": bool(pending and pending.get("kind") != "user_input"),
        "hasPendingUserInput": bool(pending and pending.get("kind") == "user_input"),
        "pendingRequest": pending,
    })
    return out


def shell_snapshot(raw: dict) -> dict:
    return {**raw, "threads": [shell_thread(row) for row in raw.get("threads", [])]}


def thread_snapshot(raw: dict, turn_limit: int | None = None) -> dict:
    projection = raw["projection"]
    thread = dict(projection["thread"])
    runs = sorted(projection.get("runs", []), key=lambda row: row.get("ordinal", 0))
    latest = runs[-1] if runs else {}
    thread["latestTurn"] = ({"turnId": latest.get("id"), "state": turn_state(latest.get("status"))}
                            if latest else None)
    messages = projection.get("messages", [])
    if turn_limit and len(runs) > turn_limit:
        run_ids = {run["id"] for run in runs[-turn_limit:]}
        messages = [row for row in messages if row.get("runId") in run_ids]
    thread["messages"] = [{**row, "turnId": row.get("runId")} for row in messages]
    thread["pendingRequests"] = [row for row in projection.get("runtimeRequests", [])
                                 if row.get("status") == "pending"]
    return {"thread": thread, "page": {"beforeCursor": raw.get("historyCursor"),
                                        "hasMore": bool(raw.get("hasMoreHistory"))}}


def history_snapshot(raw: dict, thread_id: str) -> dict:
    messages = []
    for row in raw.get("items", []):
        item = row.get("item", {})
        role = {"user_message": "user", "assistant_message": "assistant"}.get(item.get("type"))
        if role:
            messages.append({"role": role, "text": item.get("text", ""), "turnId": item.get("runId")})
    return {"thread": {"id": thread_id, "messages": messages}, "page": {
        "beforeCursor": raw.get("nextCursor"), "hasMore": bool(raw.get("hasMoreHistory")),
    }}


def stream_item(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return raw
    kind = raw.get("kind")
    if kind == "snapshot" and isinstance(raw.get("snapshot"), dict):
        snapshot = raw["snapshot"]
        if snapshot.get("schemaVersion") == 2 or any(
            "status" in row for row in snapshot.get("threads", [])
        ):
            return {**raw, "snapshot": shell_snapshot(snapshot)}
    if kind == "thread.updated":
        return {**raw, "kind": "thread-upserted", "thread": shell_thread(raw["thread"])}
    if kind == "thread.removed":
        return {**raw, "kind": "thread-removed"}
    if kind in ("project.updated", "project.removed"):
        return {**raw, "kind": kind.replace(".", "-").replace("updated", "upserted")}
    return raw


def dispatch_commands(command: dict, shell: dict | None = None) -> list[dict]:
    """Translate the stable tool commands into the pinned v2 public contract."""
    kind = command["type"]
    base = {key: command[key] for key in ("commandId", "threadId")}
    if kind == "thread.create":
        return [dict(command)]
    if kind == "thread.turn.start":
        return [
            {**base, "commandId": str(uuid4()), "type": "thread.runtime-mode.set",
             "runtimeMode": command["runtimeMode"]},
            {**base, "commandId": str(uuid4()), "type": "thread.interaction-mode.set",
             "interactionMode": command["interactionMode"]},
            {**base, "type": "message.dispatch", "messageId": command["message"]["messageId"],
             "text": command["message"]["text"], "attachments": [],
             "dispatchMode": {"type": "start_immediately"},
             **({"modelSelection": command["modelSelection"]} if "modelSelection" in command else {})},
        ]
    if kind == "thread.turn.interrupt":
        thread = next((t for t in (shell or {}).get("threads", [])
                       if t.get("id") == command["threadId"]), {})
        run_id = thread.get("activeRunId")
        if not run_id:
            raise TalariaError("thread has no active run", "refresh t3_list before interrupting")
        return [{**base, "type": "run.interrupt", "runId": run_id}]
    if kind in ("thread.approval.respond", "thread.user-input.respond"):
        thread = next((t for t in (shell or {}).get("threads", [])
                       if t.get("id") == command["threadId"]), {})
        pending = thread.get("pendingRequest") or {}
        if (pending.get("id") != command["requestId"] or pending.get("userOnly")
                or pending.get("kind") not in RESPONDABLE_KINDS):
            raise TalariaError("request is stale or requires the user", "refresh t3_list; user-only cards must be answered in T3")
        expected_input = kind == "thread.user-input.respond"
        if (pending.get("kind") == "user_input") != expected_input:
            raise TalariaError("request kind does not match", "use the pending request kind from t3_list")
        return [{**base, "type": "runtime-request.respond", "requestId": command["requestId"],
                 **{k: command[k] for k in ("decision", "answers") if k in command}}]
    raise TalariaError(f"unsupported command: {kind}", "update Talaria for this command")
