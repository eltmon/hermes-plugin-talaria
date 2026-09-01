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
        "T3 Code environments. Each row has name, environmentId, descriptor label, "
        "mode (direct | t3connect), auth (ok if a pairing secret is stored, expired "
        "if not — the token is never returned), and live (true when GET "
        "/.well-known/t3/environment succeeds). Call this first when you need to "
        "know which environments exist or whether login/connect is required before "
        "listing threads or sending prompts. Optional environment filters to one."
    ),
    {"environment": ENVIRONMENT},
)

T3_LIST_SCHEMA = _schema(
    "t3_list",
    (
        "List projects and threads in a T3 Code environment from GET "
        "/api/orchestration/shell. Each project includes id, title, and path "
        "(workspaceRoot). Each thread includes id, projectId, title, worktreePath, "
        "latestTurn {turnId, state}, hasPendingApprovals, and hasPendingUserInput. "
        "Use this to find a thread to read, prompt, interrupt, or respond to. "
        "Respond with t3_respond when a pending flag is true."
    ),
    {"environment": ENVIRONMENT},
)

T3_THREAD_SCHEMA = _schema(
    "t3_thread",
    (
        "Read a T3 Code thread's turn history and extracted agent (assistant) "
        "output text from GET /api/orchestration/threads/:id. Use after t3_list "
        "when you need conversation contents, not just the snapshot. turn_limit "
        "defaults to 5 (sent as turnLimit). before_cursor is passed through as "
        "beforeCursor; the response page.beforeCursor is the next paging token. "
        "If the JSON would exceed 32768 characters, older turns are omitted "
        "(latest kept) and truncated is true."
    ),
    {
        "environment": ENVIRONMENT,
        "thread_id": {
            "type": "string",
            "description": "Thread id from t3_list or t3_new_thread.",
        },
        "turn_limit": {
            "type": "integer",
            "description": "Maximum turns to return (default 5). Sent as turnLimit.",
        },
        "before_cursor": {
            "type": "string",
            "description": (
                "Paging cursor from a previous t3_thread page.beforeCursor; "
                "passed through to the environment as beforeCursor."
            ),
        },
    },
    required=["thread_id"],
)

_MODEL_SELECTION = {
    "type": "object",
    "description": (
        "Provider instance and model. instance_id is the provider instance slug "
        "(sent on the wire as instanceId)."
    ),
    "properties": {
        "instance_id": {
            "type": "string",
            "description": "Provider instance slug (wire instanceId).",
        },
        "model": {
            "type": "string",
            "description": "Model id on that instance.",
        },
    },
    "required": ["instance_id", "model"],
}

_RUNTIME_MODE = {
    "type": "string",
    "description": (
        "approval-required | auto-accept-edits | auto | full-access. "
        "Default approval-required. Do not use full-access unless asked."
    ),
}

_INTERACTION_MODE = {
    "type": "string",
    "description": "default | plan. Default default.",
}

T3_NEW_THREAD_SCHEMA = _schema(
    "t3_new_thread",
    (
        "Create a new T3 Code thread in a project (dispatch thread.create). Use "
        "this when the user wants a fresh conversation rather than prompting an "
        "existing thread. The client generates the thread id. runtime_mode "
        "defaults to approval-required (never full-access unless the user "
        "explicitly asks). interaction_mode defaults to default. branch and "
        "worktreePath are sent as null."
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
        "model_selection": _MODEL_SELECTION,
        "runtime_mode": _RUNTIME_MODE,
        "interaction_mode": _INTERACTION_MODE,
    },
    required=["project_id", "title", "model_selection"],
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
        "model_selection": _MODEL_SELECTION,
        "runtime_mode": _RUNTIME_MODE,
        "interaction_mode": _INTERACTION_MODE,
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
        "request_id": {
            "type": "string",
            "description": (
                "Pending request id. Sent on the wire as requestId for both "
                "thread.approval.respond and thread.user-input.respond."
            ),
        },
        "decision": {
            "type": "string",
            "description": (
                "accept | acceptForSession | acceptAlways | decline | cancel. "
                "Required when kind=approval."
            ),
        },
        "answers": {
            "type": "object",
            "description": (
                "Answers map (string keys → values) for kind=user-input. "
                "Wire field answers."
            ),
        },
    },
    required=["thread_id", "kind", "request_id"],
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

_PROJECT_ID = {
    "type": "string",
    "description": (
        "Project id from t3_list. The RPC cwd is that project's workspaceRoot."
    ),
}

_RELATIVE_PATH = {
    "type": "string",
    "description": (
        "Path relative to the project's workspaceRoot (wire field relativePath)."
    ),
}

T3_LS_SCHEMA = _schema(
    "t3_ls",
    (
        "List files and directories in a T3 Code project (WS RPC "
        "projects.listEntries). Requires project_id from t3_list. Optional path "
        "lists that subdirectory under workspaceRoot. Returns entries "
        "[{path, kind}] where kind is file or directory, plus truncated. "
        "Needs orchestration:read. Use t3_read_file / t3_write_file / t3_search "
        "for contents."
    ),
    {
        "environment": ENVIRONMENT,
        "project_id": _PROJECT_ID,
        "path": {
            "type": "string",
            "description": (
                "Optional subdirectory relative to workspaceRoot. Omitted or "
                "empty lists the project root. Joined into the RPC cwd; this "
                "RPC has no relativePath field."
            ),
        },
    },
    required=["project_id"],
)

T3_READ_FILE_SCHEMA = _schema(
    "t3_read_file",
    (
        "Read a text file in a T3 Code project (WS RPC projects.readFile). "
        "Requires project_id and path (relativePath). Returns relativePath, "
        "contents, byteLength, and truncated. Contents longer than 256 KiB "
        "are cut and truncated is true. Needs orchestration:read. Binary "
        "files fail with path_not_file / binary_file from the environment."
    ),
    {
        "environment": ENVIRONMENT,
        "project_id": _PROJECT_ID,
        "path": _RELATIVE_PATH,
    },
    required=["project_id", "path"],
)

T3_WRITE_FILE_SCHEMA = _schema(
    "t3_write_file",
    (
        "Write a text file in a T3 Code project (WS RPC projects.writeFile). "
        "Requires explicit project_id and path (relativePath) plus contents. "
        "Creates parent directories as needed on the environment. Returns "
        "relativePath. Needs orchestration:operate. Do not write without a "
        "path the user asked for."
    ),
    {
        "environment": ENVIRONMENT,
        "project_id": _PROJECT_ID,
        "path": _RELATIVE_PATH,
        "contents": {
            "type": "string",
            "description": "Full file contents to write (wire field contents).",
        },
    },
    required=["project_id", "path", "contents"],
)

T3_SEARCH_SCHEMA = _schema(
    "t3_search",
    (
        "Search file contents in a T3 Code project (WS RPC "
        "projects.searchContents). Requires project_id and query (not trimmed; "
        "whitespace is significant). Optional limit (default 50, max 500), "
        "case_sensitive, whole_word, use_regex (all default false). Returns "
        "matches [{path, lineNumber, lineContent, matchRanges}], truncated, "
        "and optional regexFallbackError. Needs orchestration:read."
    ),
    {
        "environment": ENVIRONMENT,
        "project_id": _PROJECT_ID,
        "query": {
            "type": "string",
            "description": (
                "Content search string (wire query). Not trimmed; max 256 "
                "characters. Empty is invalid."
            ),
        },
        "limit": {
            "type": "integer",
            "description": "Max matches to return (default 50, max 500).",
        },
        "case_sensitive": {
            "type": "boolean",
            "description": "Wire caseSensitive. Default false.",
        },
        "whole_word": {
            "type": "boolean",
            "description": "Wire wholeWord. Default false.",
        },
        "use_regex": {
            "type": "boolean",
            "description": "Wire useRegex. Default false.",
        },
    },
    required=["project_id", "query"],
)

_WATCH_KIND = {
    "type": "string",
    "description": (
        "thread | shell. thread watches orchestration.subscribeThread for one "
        "thread_id; shell watches orchestration.subscribeShell for the "
        "environment."
    ),
}

T3_WATCH_SCHEMA = _schema(
    "t3_watch",
    (
        "Start a background reader on a T3 Code subscribeThread (kind=thread) "
        "or subscribeShell (kind=shell) stream. Salient events (turn settled, "
        "approval requested, user-input requested) are injected into the "
        "Hermes conversation via inject_message. Requires "
        "plugins.entries.t3code.settings.allow_gateway_injection: true; "
        "without that grant this tool returns instructions and injects "
        "nothing. Gateway inject_message also needs the host grant "
        "plugins.entries.t3code.allow_gateway_injection: true. Resume cursor "
        "is afterSequence stored in plugin state. Stop with t3_unwatch."
    ),
    {
        "environment": ENVIRONMENT,
        "kind": _WATCH_KIND,
        "thread_id": {
            "type": "string",
            "description": "Thread id to subscribe to. Required when kind=thread.",
        },
    },
    required=["kind"],
)

T3_UNWATCH_SCHEMA = _schema(
    "t3_unwatch",
    (
        "Stop a t3_watch background reader for kind=thread or kind=shell and "
        "clear that watch from plugin state. Does not require the injection "
        "grant. Pass the same environment / thread_id used with t3_watch."
    ),
    {
        "environment": ENVIRONMENT,
        "kind": _WATCH_KIND,
        "thread_id": {
            "type": "string",
            "description": "Thread id of the watch to stop. Required when kind=thread.",
        },
    },
    required=["kind"],
)
