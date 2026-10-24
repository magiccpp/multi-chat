"""In-process pub/sub per session, so several clients can follow the same running turn."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any


class Subscription:
    """Registered on creation (synchronously), so no event published afterwards is missed."""

    def __init__(self, bus: SessionBus, session_id: int, *, replay: bool, until_turn_end: bool) -> None:
        self.bus = bus
        self.session_id = session_id
        self.until_turn_end = until_turn_end
        self.queue: asyncio.Queue = asyncio.Queue()
        if replay:
            for event in bus._replay.get(session_id, []):
                self.queue.put_nowait(event)
        bus._subscribers[session_id].add(self.queue)

    def close(self) -> None:
        self.bus._subscribers[self.session_id].discard(self.queue)

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        try:
            while True:
                event = await self.queue.get()
                yield event
                if self.until_turn_end and event["type"] == "turn_end":
                    return
        finally:
            self.close()


class SessionBus:
    def __init__(self) -> None:
        self._subscribers: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._replay: dict[int, list[dict[str, Any]]] = defaultdict(list)
        self._seq: dict[int, int] = defaultdict(int)

    def begin_turn(self, session_id: int) -> None:
        self._replay[session_id] = []

    def end_turn(self, session_id: int) -> None:
        self._replay.pop(session_id, None)

    def publish(self, session_id: int, event: dict[str, Any]) -> dict[str, Any]:
        self._seq[session_id] += 1
        event = {**event, "session_id": session_id, "seq": self._seq[session_id]}
        if session_id in self._replay:
            self._replay[session_id].append(event)
        for queue in list(self._subscribers[session_id]):
            queue.put_nowait(event)
        return event

    def subscribe(self, session_id: int, *, replay: bool = True, until_turn_end: bool = False) -> Subscription:
        return Subscription(self, session_id, replay=replay, until_turn_end=until_turn_end)


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


bus = SessionBus()
