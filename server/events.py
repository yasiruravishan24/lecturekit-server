"""Real-time events: every client of a user (web page, extension, later the mobile app) keeps one WebSocket open and the server
pushes what changed, so nothing needs a refresh. One server process only (see main.py), so a plain in-memory hub is enough.

Event shapes (JSON, sent as text; the contract the mobile app must follow too):
  {"type": "hello"}                                                              right after a successful log-in on the socket
  {"type": "ping"}                                                               every 25 s, to keep proxies from closing the socket
  {"type": "sync", "reason": str, "session_id": int | null}                      something of this user changed: fetch the lists again
  {"type": "job", "session_id": int, "kind": str, "stage": str, "percent": int, "message": str}   progress of a job
  {"type": "notification", "notification": {id, kind, session_id, title, message, created_at, read}}
  {"type": "resync"}                                                             too many events were missed: fetch everything again
"""
import asyncio
import logging

logger = logging.getLogger("lecturekit.server")


class Hub:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: dict[int, set[asyncio.Queue]] = {}

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add(self, user_id: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._clients.setdefault(user_id, set()).add(queue)
        return queue

    def remove(self, user_id: int, queue: asyncio.Queue) -> None:
        self._clients.get(user_id, set()).discard(queue)
        if not self._clients.get(user_id):
            self._clients.pop(user_id, None)

    def count(self, user_id: int | None = None) -> int:
        if user_id is not None:
            return len(self._clients.get(user_id, ()))
        return sum(len(v) for v in self._clients.values())

    def publish(self, user_id: int | None, event: dict) -> None:
        """Safe to call from any thread (routes run in a thread pool, jobs in the worker threads)."""
        if self._loop is None or user_id is None or user_id not in self._clients:
            return
        try:
            self._loop.call_soon_threadsafe(self._deliver, user_id, event)
        except RuntimeError:                                  # the loop is closed: the server is stopping
            pass

    def _deliver(self, user_id: int, event: dict) -> None:
        for queue in list(self._clients.get(user_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:                          # a client that does not read: tell it to start over
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "resync"})


def sync(hub: Hub | None, user_id: int | None, reason: str, session_id: int | None = None) -> None:
    if hub is not None:
        hub.publish(user_id, {"type": "sync", "reason": reason, "session_id": session_id})
