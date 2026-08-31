"""LLM-facing tool schemas. The model routes on ``description``."""

from __future__ import annotations

ENVIRONMENT = {
    "type": "string",
    "description": (
        "Environment name or environmentId. Optional when default_environment "
        "is set or only one environment is available. Resolution order: "
        "explicit argument → default_environment → sole available environment."
    ),
}


def _schema(name: str, description: str, properties: dict, required: list | None = None) -> dict:
    parameters = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required
    return {"name": name, "description": description, "parameters": parameters}


T3_ENVIRONMENTS_SCHEMA = _schema(
    "t3_environments",
    (
        "List configured (mode A, direct URL) and T3 Connect-discovered (mode B) "
        "T3 Code environments, with label, connectivity mode, auth status, and "
        "liveness. Call this first when you need to know which environments exist "
        "or whether login/connect is required before listing threads or sending "
        "prompts."
    ),
    {"environment": ENVIRONMENT},
)

T3_LIST_SCHEMA = _schema(
    "t3_list",
    (
        "List projects and threads in a T3 Code environment from the orchestration "
        "shell snapshot. Each project includes id, title, and path. Each thread "
        "includes id, title, worktreePath, latestTurn status, hasPendingApprovals, "
        "and hasPendingUserInput. Use this to find a thread to read, prompt, "
        "interrupt, or respond to."
    ),
    {"environment": ENVIRONMENT},
)

T3_THREAD_SCHEMA = _schema(
    "t3_thread",
    (
        "Read a T3 Code thread's turn history and agent output. Use after t3_list "
        "when you need conversation contents, not just the snapshot. turn_limit "
        "defaults to 5; before_cursor pages further back through older turns."
    ),
    {
        "environment": ENVIRONMENT,
        "thread_id": {
            "type": "string",
            "description": "Thread id from t3_list or t3_new_thread.",
        },
        "turn_limit": {
            "type": "integer",
            "description": "Maximum turns to return (default 5).",
        },
        "before_cursor": {
            "type": "string",
            "description": "Paging cursor from a previous t3_thread response.",
        },
    },
    required=["thread_id"],
)

T3_NEW_THREAD_SCHEMA = _schema(
    "t3_new_thread",
    (
        "Create a new T3 Code thread in a project (dispatch thread.create). Use "
        "this when the user wants a fresh conversation rather than prompting an "
        "existing thread. The client generates the thread id."
    ),
    {
        "environment": ENVIRONMENT,
        "project_id": {
            "type": "string",
            "description": "Project id from t3_list to create the thread in.",
        },
        "title": {
            "type": "string",
            "description": "Title for the new thread.",
        },
    },
    required=["project_id"],
)

T3_PROMPT_SCHEMA = _schema(
    "t3_prompt",
    (
        "Start a turn on a T3 Code thread by sending a user prompt (dispatch "
        "thread.turn.start). Optional model_selection {instance_id, model}, "
        "runtime_mode (default approval-required — never full-access unless the "
        "user explicitly asks), and interaction_mode (default | plan). After "
        "sending, use t3_wait rather than guessing when the turn has settled."
    ),
    {
        "environment": ENVIRONMENT,
        "thread_id": {
            "type": "string",
            "description": "Thread id to prompt.",
        },
        "text": {
            "type": "string",
            "description": "User prompt text to send as the turn message.",
        },
        "runtime_mode": {
            "type": "string",
            "description": (
                "approval-required | auto-accept-edits | auto | full-access. "
                "Default approval-required. Do not use full-access unless asked."
            ),
        },
        "interaction_mode": {
            "type": "string",
            "description": "default | plan.",
        },
    },
    required=["thread_id", "text"],
)

T3_INTERRUPT_SCHEMA = _schema(
    "t3_interrupt",
    (
        "Interrupt the in-progress turn on a T3 Code thread (dispatch "
        "thread.turn.interrupt). Use when the user wants the remote agent to stop "
        "the current turn."
    ),
    {
        "environment": ENVIRONMENT,
        "thread_id": {
            "type": "string",
            "description": "Thread id whose in-progress turn should be interrupted.",
        },
    },
    required=["thread_id"],
)

T3_RESPOND_SCHEMA = _schema(
    "t3_respond",
    (
        "Respond to a pending approval request or user-input request on a T3 Code "
        "thread (dispatch thread.approval.respond or thread.user-input.respond). "
        "Use kind=approval or kind=user-input. Check t3_list pending flags to "
        "see which kind is needed."
    ),
    {
        "environment": ENVIRONMENT,
        "thread_id": {
            "type": "string",
            "description": "Thread id with a pending approval or user-input request.",
        },
        "kind": {
            "type": "string",
            "description": "approval | user-input. Which pending request to answer.",
        },
    },
    required=["thread_id", "kind"],
)

T3_WAIT_SCHEMA = _schema(
    "t3_wait",
    (
        "Poll a T3 Code thread until the latest turn settles, an approval or "
        "user-input request appears, or timeout elapses. Use after t3_prompt "
        "instead of guessing when the remote agent is done. interval defaults to "
        "5s (floor 2s); timeout defaults to 300s."
    ),
    {
        "environment": ENVIRONMENT,
        "thread_id": {
            "type": "string",
            "description": "Thread id to poll.",
        },
        "timeout": {
            "type": "integer",
            "description": "Seconds to wait before returning a timeout result (default 300).",
        },
        "interval": {
            "type": "integer",
            "description": "Seconds between polls (default 5, floor 2).",
        },
    },
    required=["thread_id"],
)
