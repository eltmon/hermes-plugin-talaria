"""Run an async transport from Hermes' synchronous tool interface."""
import asyncio
import threading


def run_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    results, errors = [], []
    def worker():
        try:
            results.append(asyncio.run(coro))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return results[0]
