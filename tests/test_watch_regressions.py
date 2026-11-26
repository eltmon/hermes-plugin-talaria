"""Conversation ownership and recovery through registered tool handlers."""
import asyncio
import json
import pytest

from talaria.tools import handle_t3_watch, handle_t3_unwatch, set_ws_client_factory
from talaria.t3_ws import TicketRejected


class Context:
    def __init__(self):
        self.state, self.injected = {}, []
        self.changed = asyncio.Queue()
    def get_config(self, key, default=None):
        return {"allow_gateway_injection": True, "environments": {
            "laptop": {"base_url": "https://t3.example.test"}}}.get(key, default)
    def inject_message(self, text, role="user", *, session_key=None):
        self.injected.append((session_key, text))
        self.changed.put_nowait((session_key, text))


def update(sequence, status="completed", pending=None):
    return {"kind": "thread.updated", "sequence": sequence, "thread": {
        "id": "t1", "latestRunId": f"r{sequence}", "status": status,
        "pendingRuntimeRequest": pending,
    }}


class Stream:
    def __init__(self):
        self.queue = asyncio.Queue()
    async def subscribe(self, tag, payload):
        while True:
            item = await self.queue.get()
            if isinstance(item, Exception):
                raise item
            yield item
    async def close(self):
        pass


def test_conversations_receive_independent_events_and_can_unwatch_independently():
    async def scenario():
        # Arrange.
        ctx, streams, tasks = Context(), [], []
        def factory(ref):
            stream = Stream()
            streams.append(stream)
            return stream
        def spawn(coro, name=None):
            task = asyncio.create_task(coro, name=name)
            tasks.append(task)
            return task
        set_ws_client_factory(factory)
        try:
            # Act: two conversations watch the same target.
            for session in ("a", "b"):
                result = json.loads(handle_t3_watch({"kind": "shell"}, ctx=ctx,
                    spawn=spawn, session_key=session))
                assert result["watched"] is True
            await asyncio.sleep(0)
            for stream in streams:
                stream.queue.put_nowait(update(1))
            for _ in range(4):
                await asyncio.sleep(0)
            # Assert: both recipients receive the completion.
            assert {session for session, _ in ctx.injected} == {"a", "b"}
            handle_t3_unwatch({"kind": "shell"}, ctx=ctx, session_key="a")
            for stream in streams:
                stream.queue.put_nowait(update(2, "waiting", {"id": "q2", "kind": "command"}))
            for _ in range(4):
                await asyncio.sleep(0)
            assert [(session, "approval requested" in text) for session, text in ctx.injected][-1] == ("b", True)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(scenario())


def test_watch_resumes_after_disconnect_and_delivers_later_completion():
    async def scenario():
        # Arrange: a remote subscription drops after cursor 7.
        ctx, subscriptions, tasks = Context(), [], []
        class ReconnectingStream:
            async def subscribe(self, tag, payload):
                subscriptions.append(dict(payload))
                if len(subscriptions) == 1:
                    yield update(7, "running")
                    raise ConnectionError("synthetic drop")
                yield update(8)
            async def close(self):
                pass
        async def no_delay(seconds):
            pass
        def spawn(coro, name=None):
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task
        set_ws_client_factory(lambda ref: ReconnectingStream())
        # Act.
        handle_t3_watch({"kind": "shell"}, ctx=ctx, spawn=spawn,
            session_key="a", watch_sleep=no_delay)
        await asyncio.gather(*tasks)
        # Assert: resumes from the consumed cursor and actually delivers completion.
        assert len(subscriptions) == 2
        assert subscriptions[1]["afterSequence"] == 7
        assert len(ctx.injected) == 1
        assert "turn settled" in ctx.injected[0][1]
    asyncio.run(scenario())


def test_distinct_pending_requests_are_not_suppressed_by_a_true_flag():
    # Arrange: two requests can arrive without an intervening no-pending update.
    ctx = Context()
    class Requests:
        async def subscribe(self, tag, payload):
            yield update(1, "waiting", {"id": "q1", "kind": "command"})
            yield update(2, "waiting", {"id": "q2", "kind": "command"})
        async def close(self):
            pass
    set_ws_client_factory(lambda ref: Requests())
    def spawn(coro, name=None):
        asyncio.run(coro)
    # Act.
    result = json.loads(handle_t3_watch({"kind": "thread", "thread_id": "t1"},
        ctx=ctx, spawn=spawn, session_key="a"))
    # Assert.
    assert result["watched"] is True
    assert len(ctx.injected) == 2
    assert all("approval requested" in text for _, text in ctx.injected)


@pytest.mark.parametrize("failure_at_creation", [True, False])
def test_terminal_watch_failure_notifies_owner_without_returning_exception_secrets(failure_at_creation):
    async def scenario():
        # Arrange.
        ctx, tasks = Context(), []
        class Denied:
            async def subscribe(self, tag, payload):
                raise TicketRejected("fake-sensitive-token")
                yield
            async def close(self):
                pass
        def factory(ref):
            if failure_at_creation:
                raise RuntimeError("fake-sensitive-token")
            return Denied()
        def spawn(coro, name=None):
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task
        set_ws_client_factory(factory)
        # Act.
        handle_t3_watch({"kind": "shell"}, ctx=ctx, spawn=spawn, session_key="a")
        await asyncio.gather(*tasks)
        # Assert.
        assert len(ctx.injected) == 1
        assert ctx.injected[0][0] == "a"
        assert "watch failed" in ctx.injected[0][1]
        assert "fake-sensitive-token" not in ctx.injected[0][1]
    asyncio.run(scenario())


def test_scoped_watch_refuses_a_host_that_can_only_broadcast():
    # Arrange: an older host cannot target an individual conversation.
    class UnscopedContext(Context):
        def inject_message(self, text):
            self.injected.append(("broadcast", text))
    ctx = UnscopedContext()
    started = []
    def spawn(coro, name=None):
        started.append(coro)
        coro.close()
    # Act.
    result = json.loads(handle_t3_watch({"kind": "shell"}, ctx=ctx,
        spawn=spawn, session_key="private-conversation"))
    # Assert: never downgrade private delivery to a broadcast.
    assert "scoped" in result.get("error", "")
    assert started == []
    assert ctx.injected == []


def test_rehydrated_snapshot_does_not_repeat_a_completed_run_notification():
    ctx = Context()
    class RehydratingStream:
        async def subscribe(self, tag, payload):
            yield update(1)
            yield {"kind": "snapshot", "snapshot": {"schemaVersion": 2,
                "snapshotSequence": 2, "threads": []}}
            yield {**update(3), "thread": update(1)["thread"]}
            yield update(4)
        async def close(self):
            pass
    set_ws_client_factory(lambda ref: RehydratingStream())
    result = json.loads(handle_t3_watch({"kind": "thread", "thread_id": "t1"},
        ctx=ctx, spawn=lambda coro, **kw: asyncio.run(coro), session_key="a"))
    assert result["watched"] is True
    assert len(ctx.injected) == 2


def test_watch_uses_published_hermes_context_when_dispatch_omits_routing_key(monkeypatch):
    import sys
    from types import ModuleType
    from contextvars import ContextVar
    route = ContextVar("test_hermes_route", default="")
    api = ModuleType("gateway.session_context")
    api.get_session_env = lambda key, default="": route.get() if key == "HERMES_SESSION_KEY" else default
    monkeypatch.setitem(sys.modules, "gateway.session_context", api)
    ctx = Context()
    class Completed:
        async def subscribe(self, tag, payload):
            yield update(1)
        async def close(self):
            pass
    set_ws_client_factory(lambda ref: Completed())
    token = route.set("private-a")
    try:
        result = json.loads(handle_t3_watch({"kind": "shell"}, ctx=ctx,
            spawn=lambda coro, **kw: asyncio.run(coro), session_id="different-id"))
    finally:
        route.reset(token)
    assert result["watched"] is True
    assert [key for key, _ in ctx.injected] == ["private-a"]
