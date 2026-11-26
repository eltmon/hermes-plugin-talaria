"""Deadlines at the WebSocket boundary, using the event loop's fake clock."""
import asyncio
import json

from talaria.t3_ws import T3WsClient


def test_silent_rpc_returns_a_bounded_unknown_outcome_without_retrying_write():
    async def scenario():
        # Arrange: connection stays alive but never replies to the write request.
        loop = asyncio.get_running_loop()
        original_time = loop.time
        now = [original_time()]
        loop.time = lambda: now[0]
        sent = []
        class SilentPeer:
            async def send(self, raw):
                sent.append(json.loads(raw))
            async def recv(self):
                await asyncio.Event().wait()
            async def close(self):
                pass
        async def open_ws(url):
            return SilentPeer()
        client = T3WsClient("https://example.test", lambda: "fake-ticket",
            open_ws=open_ws, request_timeout=1)
        task = asyncio.create_task(client.request("projects.writeFile", {
            "cwd": "C:/work", "relativePath": "a.txt", "contents": "test"}))
        try:
            # Act: let I/O start, then advance the fake deadline.
            for _ in range(6):
                await asyncio.sleep(0)
            now[0] += 2
            for _ in range(12):
                await asyncio.sleep(0)
            # Assert: the operation settles and the write was not replayed.
            assert task.done(), "request did not honor its deadline"
            try:
                task.result()
                raise AssertionError("silent RPC unexpectedly succeeded")
            except Exception as exc:
                assert "outcome is unknown" in str(exc)
            assert sum(frame["_tag"] == "Request" for frame in sent) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.close()
            loop.time = original_time
    asyncio.run(scenario())
