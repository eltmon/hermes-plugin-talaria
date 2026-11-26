"""Public tools against protocol-2 wire shapes pinned to T3 v0.1.10.

Contract source: pingdotgg/t3code@7d32075e3757ded707914e2450c3e392a9665765
packages/contracts/src/{environmentHttp,environment,orchestrationV2}.ts.
"""
import json
import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from talaria.t3_env import T3EnvClient
from talaria.config import resolve_environment
from talaria.tools import (handle_t3_list, handle_t3_prompt, handle_t3_thread, handle_t3_watch,
                          handle_t3_new_thread, handle_t3_respond, handle_t3_interrupt, set_client_factory)
from talaria.tools import set_ws_client_factory
from talaria.tools import handle_t3_read_file
from talaria.tools import handle_t3_wait
from talaria.t3_ws import T3WsClient


class Context:
    secrets = {"T3CODE_TOKEN_LAPTOP": "fake-token"}
    state = {}

    def __init__(self):
        self.state = {}
        self.injected = []

    def inject_message(self, text, role="user", *, session_key=None):
        self.injected.append({"text": text, "session": session_key})

    def get_config(self, key, default=None):
        if key == "allow_gateway_injection":
            return True
        return {"environments": {"laptop": {
            "base_url": "https://t3.example.test", "protocol_version": 2,
        }}}.get(key, default)


def test_list_current_t3_preserves_running_and_pending_status():
    # Arrange: protocol-2 shell fields come from the pinned upstream contract.
    def peer(request):
        if request.url.path == "/.well-known/t3/environment":
            return httpx.Response(200, json={"orchestrationProtocolVersion": 2})
        assert request.headers["x-t3-orchestration-protocol"] == "2"
        assert request.url.path == "/api/orchestration/shell"
        return httpx.Response(200, json={
            "schemaVersion": 2, "snapshotSequence": 12,
            "projects": [{"id": "p1", "title": "Project", "workspaceRoot": "C:/work"}],
            "threads": [{"id": "t1", "projectId": "p1", "title": "Task",
                "worktreePath": None, "latestRunId": "r1", "activeRunId": "r1",
                "status": "waiting", "pendingRuntimeRequest": {
                    "id": "q1", "kind": "command", "blocking": True,
                }}],
        })
    with httpx.Client(transport=httpx.MockTransport(peer)) as http:
        set_client_factory(lambda ref, headers: T3EnvClient(
            ref.base_url, headers, client=http, protocol_version=2))
        # Act: use the public Hermes tool, not a normalization helper.
        result = json.loads(handle_t3_list({}, ctx=Context()))
    # Assert: callers can tell work is active and find the pending decision.
    assert "error" not in result, result
    assert result["threads"][0]["latestTurn"] == {"turnId": "r1", "state": "running"}
    assert result["threads"][0]["hasPendingApprovals"] is True
    assert result["threads"][0]["pendingRequest"]["id"] == "q1"


class RpcPeer:
    def __init__(self):
        self.commands = []
        self.incoming = asyncio.Queue()

    async def send(self, raw):
        frame = json.loads(raw)
        if frame["_tag"] == "Request":
            assert frame["tag"] == "orchestration.dispatchCommand"
            self.commands.append(frame["payload"])
            self.incoming.put_nowait(json.dumps({"_tag": "Exit", "requestId": frame["id"],
                "exit": {"_tag": "Success", "value": {"accepted": True}}}))

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        pass


def test_prompt_current_t3_uses_rpc_and_keeps_approval_required():
    # Arrange: HTTP has no dispatch endpoint in protocol 2.
    peer = RpcPeer()
    async def open_ws(url):
        assert parse_qs(urlparse(url).query)["orchestrationProtocol"] == ["2"]
        return peer
    def http_peer(request):
        if request.url.path == "/.well-known/t3/environment":
            return httpx.Response(200, json={"orchestrationProtocolVersion": 2})
        assert request.url.path == "/api/auth/websocket-ticket"
        return httpx.Response(200, json={"ticket": "fake-ticket"})
    with httpx.Client(transport=httpx.MockTransport(http_peer)) as http:
        set_client_factory(lambda ref, headers: T3EnvClient(ref.base_url, headers,
            client=http, protocol_version=2, open_ws=open_ws))
        # Act.
        result = json.loads(handle_t3_prompt({"thread_id": "t1", "text": "Investigate"}, ctx=Context()))
    # Assert: permission mode is set before message dispatch; no old HTTP mutation.
    assert "error" not in result, result
    assert [c["type"] for c in peer.commands] == [
        "thread.runtime-mode.set", "thread.interaction-mode.set", "message.dispatch"]
    assert peer.commands[0]["runtimeMode"] == "approval-required"
    assert peer.commands[-1]["text"] == "Investigate"
    assert peer.commands[-1]["dispatchMode"] == {"type": "start_immediately"}


def test_thread_current_t3_returns_assistant_result_and_request_identity():
    # Arrange: v2 detail wraps the conversation and requests in a projection.
    def peer(request):
        if request.url.path == "/.well-known/t3/environment":
            return httpx.Response(200, json={"orchestrationProtocolVersion": 2})
        assert request.headers["x-t3-orchestration-protocol"] == "2"
        return httpx.Response(200, json={"snapshotSequence": 15, "projection": {
            "thread": {"id": "t1", "projectId": "p1", "title": "Task"},
            "runs": [{"id": "r1", "ordinal": 1, "status": "completed"}],
            "messages": [{"id": "m1", "runId": "r1", "role": "assistant", "text": "Found the cause"}],
            "runtimeRequests": [{"id": "q2", "kind": "user_input", "status": "pending"}],
        }, "hasMoreHistory": False, "historyCursor": None})
    with httpx.Client(transport=httpx.MockTransport(peer)) as http:
        set_client_factory(lambda ref, headers: T3EnvClient(ref.base_url, headers, client=http, protocol_version=2))
        # Act.
        result = json.loads(handle_t3_thread({"thread_id": "t1"}, ctx=Context()))
    # Assert.
    assert "error" not in result, result
    assert result["turns"][0]["agent_output"] == "Found the cause"
    assert result["latestTurn"] == {"turnId": "r1", "state": "completed"}
    assert result["pendingRequests"][0]["id"] == "q2"


def test_config_defaults_to_current_protocol_and_rejects_unknown_versions():
    # Arrange / Act / Assert: plugin wiring must actually select the new adapter.
    assert resolve_environment(Context()).protocol_version == 2
    class FutureContext(Context):
        def get_config(self, key, default=None):
            return {"environments": {"laptop": {
                "base_url": "https://t3.example.test", "protocol_version": 99,
            }}}.get(key, default)
    result = json.loads(handle_t3_list({}, ctx=FutureContext()))
    assert "unsupported" in result["error"]


def test_current_history_cursor_reads_older_conversation():
    # Arrange: v2 history returns projected turn items on a dedicated route.
    def peer(request):
        if request.url.path == "/.well-known/t3/environment":
            return httpx.Response(200, json={"orchestrationProtocolVersion": 2})
        assert request.url.path == "/api/orchestration/threads/t1/history"
        assert request.url.params["cursor"] == "opaque-history"
        return httpx.Response(200, json={"items": [{"item": {
            "type": "assistant_message", "runId": "r0", "text": "Earlier finding",
        }}], "nextCursor": "even-older", "hasMoreHistory": True})
    with httpx.Client(transport=httpx.MockTransport(peer)) as http:
        set_client_factory(lambda ref, headers: T3EnvClient(ref.base_url, headers, client=http, protocol_version=2))
        # Act.
        result = json.loads(handle_t3_thread({"thread_id": "t1", "before_cursor": "opaque-history"}, ctx=Context()))
    # Assert.
    assert "error" not in result, result
    assert result["turns"][0]["agent_output"] == "Earlier finding"
    assert result["page"] == {"beforeCursor": "even-older", "hasMore": True}


@pytest.mark.parametrize("tool,args,expected_type", [
    (handle_t3_new_thread, {"project_id": "p1", "title": "Task",
        "model_selection": {"instance_id": "codex", "model": "test-model"}}, "thread.create"),
    (handle_t3_interrupt, {"thread_id": "t1"}, "run.interrupt"),
    (handle_t3_respond, {"thread_id": "t1", "kind": "approval", "request_id": "q1",
        "decision": "decline"}, "runtime-request.respond"),
])
def test_current_task_controls_use_protocol_two(tool, args, expected_type):
    # Arrange: fake network peers enforce current routes and RPC names.
    peer = RpcPeer()
    async def open_ws(url):
        return peer
    def http_peer(request):
        if request.url.path == "/.well-known/t3/environment":
            return httpx.Response(200, json={"orchestrationProtocolVersion": 2})
        if request.url.path == "/api/orchestration/shell":
            return httpx.Response(200, json={"threads": [{"id": "t1", "status": "waiting",
                "activeRunId": "r1", "pendingRuntimeRequest": {"id": "q1", "kind": "command"}}]})
        assert request.url.path == "/api/auth/websocket-ticket"
        return httpx.Response(200, json={"ticket": "fake-ticket"})
    with httpx.Client(transport=httpx.MockTransport(http_peer)) as http:
        set_client_factory(lambda ref, headers: T3EnvClient(ref.base_url, headers,
            client=http, protocol_version=2, open_ws=open_ws))
        # Act.
        result = json.loads(tool(args, ctx=Context()))
    # Assert.
    assert "error" not in result, result
    assert peer.commands[-1]["type"] == expected_type
    if expected_type == "run.interrupt":
        assert peer.commands[-1]["runId"] == "r1"


def test_current_thread_watch_reports_completion_only_for_selected_thread():
    # Arrange: real transport decodes current shell frames from a fake peer.
    class StreamPeer(RpcPeer):
        async def send(self, raw):
            frame = json.loads(raw)
            if frame["_tag"] == "Request":
                assert frame["tag"] == "orchestration.subscribeShell"
                assert "threadId" not in frame["payload"]
                values = [{"kind": "thread.updated", "sequence": n,
                    "thread": {"id": tid, "latestRunId": "r1", "status": "completed",
                               "pendingRuntimeRequest": None}}
                    for n, tid in enumerate(("other-thread", "t1"), 1)]
                self.incoming.put_nowait(json.dumps({"_tag": "Chunk", "requestId": frame["id"], "values": values}))
                self.incoming.put_nowait(json.dumps({"_tag": "Exit", "requestId": frame["id"],
                    "exit": {"_tag": "Success", "value": None}}))
    peer, ctx = StreamPeer(), Context()
    async def open_ws(url):
        return peer
    set_ws_client_factory(lambda ref: T3WsClient(ref.base_url, lambda: "fake-ticket",
        protocol_version=2, open_ws=open_ws))
    def spawn(coro, name=None):
        return asyncio.run(coro)
    # Act.
    result = json.loads(handle_t3_watch({"kind": "thread", "thread_id": "t1"},
        ctx=ctx, spawn=spawn, session_key="conversation-a"))
    # Assert.
    assert "error" not in result, result
    assert len(ctx.injected) == 1
    assert "thread t1: turn settled" in ctx.injected[0]["text"]


def test_file_tool_uses_current_socket_protocol_and_remote_project_root():
    # Arrange: public RPC transport with an injected network peer.
    class FilePeer(RpcPeer):
        async def send(self, raw):
            frame = json.loads(raw)
            if frame["_tag"] == "Request":
                assert frame["tag"] == "projects.readFile"
                assert frame["payload"] == {"cwd": "C:/remote-work", "relativePath": "notes.txt"}
                self.incoming.put_nowait(json.dumps({"_tag": "Exit", "requestId": frame["id"],
                    "exit": {"_tag": "Success", "value": {"contents": "remote text"}}}))
    peer = FilePeer()
    async def open_ws(url):
        assert parse_qs(urlparse(url).query)["orchestrationProtocol"] == ["2"]
        return peer
    set_ws_client_factory(lambda ref: T3WsClient(ref.base_url, lambda: "fake-ticket",
        protocol_version=ref.protocol_version, open_ws=open_ws))
    def http_peer(request):
        if request.url.path == "/.well-known/t3/environment":
            return httpx.Response(200, json={"orchestrationProtocolVersion": 2})
        if request.url.path == "/api/orchestration/shell":
            return httpx.Response(200, json={"projects": [{"id": "p1", "workspaceRoot": "C:/remote-work"}], "threads": []})
        assert request.url.path == "/api/auth/websocket-ticket"
        return httpx.Response(200, json={"ticket": "fake-ticket"})
    with httpx.Client(transport=httpx.MockTransport(http_peer)) as http:
        set_client_factory(lambda ref, headers: T3EnvClient(ref.base_url, headers,
            client=http, protocol_version=ref.protocol_version))
        # Act.
        result = json.loads(handle_t3_read_file({"project_id": "p1", "path": "notes.txt"}, ctx=Context()))
    # Assert.
    assert "error" not in result, result
    assert result["contents"] == "remote text"


@pytest.mark.parametrize("request_kind,user_only,response_kind", [
    ("user_input", True, "user-input"),
    ("auth_refresh", False, "approval"),
])
def test_user_only_requests_are_not_answered_by_the_coordinating_agent(request_kind, user_only, response_kind):
    # Arrange: the shell identifies a request that must stay with the human.
    def peer(request):
        if request.url.path == "/.well-known/t3/environment":
            return httpx.Response(200, json={"orchestrationProtocolVersion": 2})
        assert request.url.path == "/api/orchestration/shell"
        return httpx.Response(200, json={"threads": [{"id": "t1", "status": "waiting",
            "pendingRuntimeRequest": {"id": "q1", "kind": request_kind, "userOnly": user_only}}]})
    with httpx.Client(transport=httpx.MockTransport(peer)) as http:
        set_client_factory(lambda ref, headers: T3EnvClient(ref.base_url, headers, client=http, protocol_version=2))
        # Act.
        result = json.loads(handle_t3_respond({"thread_id": "t1", "kind": response_kind,
            "request_id": "q1", "decision": "decline", "answers": {"question": "anything"}}, ctx=Context()))
    # Assert: no ticket or mutation was requested.
    assert "requires the user" in result["error"]


def test_future_protocol_is_rejected_before_any_task_mutation():
    # Arrange.
    def peer(request):
        assert request.url.path == "/.well-known/t3/environment"
        return httpx.Response(200, json={"orchestrationProtocolVersion": 3})
    with httpx.Client(transport=httpx.MockTransport(peer)) as http:
        set_client_factory(lambda ref, headers: T3EnvClient(ref.base_url, headers, client=http, protocol_version=2))
        # Act.
        result = json.loads(handle_t3_prompt({"thread_id": "t1", "text": "Investigate"}, ctx=Context()))
    # Assert.
    assert "unsupported server orchestration protocol" in result["error"]


def test_wait_recognizes_a_rolled_back_run_as_terminal():
    # Arrange: rolled_back is a terminal status in the pinned v2 run contract.
    def peer(request):
        if request.url.path == "/.well-known/t3/environment":
            return httpx.Response(200, json={"orchestrationProtocolVersion": 2})
        return httpx.Response(200, json={"threads": [{"id": "t1", "latestRunId": "r1",
            "status": "rolled_back", "pendingRuntimeRequest": None}]})
    with httpx.Client(transport=httpx.MockTransport(peer)) as http:
        set_client_factory(lambda ref, headers: T3EnvClient(ref.base_url, headers, client=http, protocol_version=2))
        # Act.
        result = json.loads(handle_t3_wait({"thread_id": "t1", "timeout": 0}, ctx=Context()))
    # Assert: no unnecessary wait or false completion after rollback.
    assert result["status"] == "settled"
    assert result["latestTurn"]["state"] == "interrupted"
