"""
sse.py -- a tiny in-process publish/subscribe broker for Server-Sent Events.

The data volume is microscopic (the status of ~12-16 bays), so this does not
need to be clever -- it just needs to be *stable*. Each connected browser
(dashboard TV or console) opens one long-lived HTTP connection to /events and
gets its own Queue here. When anything changes, the app publishes a message and
every subscriber's queue receives a copy.

Reconnection is handled on the client side (EventSource auto-reconnects, plus we
have a polling fallback), so this broker can stay dead simple: add a queue on
subscribe, drop it on disconnect, fan-out on publish.
"""

import json
import queue
import threading
from typing import Dict, List


class Broker:
    """Fan-out message broker. Thread-safe; safe under waitress' thread pool."""

    def __init__(self) -> None:
        self._subscribers: List[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        """Register a new subscriber and return its personal message queue."""
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event_type: str, data: Dict) -> None:
        """Send a message to every subscriber.

        ``event_type`` becomes the SSE "event:" line (e.g. "state", "delay",
        "heartbeat"); ``data`` is JSON-encoded into the "data:" line. If a
        client's queue is full (a wedged/slow browser), we drop the message for
        that client rather than block everyone -- the client will self-correct on
        its next poll/heartbeat.
        """
        payload = {"type": event_type, "data": data}
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

    @staticmethod
    def format_sse(payload: Dict) -> str:
        """Render a payload as a Server-Sent Events wire message."""
        return f"event: {payload['type']}\ndata: {json.dumps(payload['data'])}\n\n"

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


# A single process-wide broker instance shared by the whole app.
broker = Broker()
