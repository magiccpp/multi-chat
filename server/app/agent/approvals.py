"""Pending tool approvals: the agent awaits a future that a client resolves through the API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.agent.events import bus
from app.agent.status import APPROVAL, approval_text, board


@dataclass
class Decision:
    approve: bool
    note: str | None = None


class Approvals:
    def __init__(self) -> None:
        self._pending: dict[str, tuple[int, asyncio.Future[Decision]]] = {}

    async def request(self, session_id: int, call_id: str, payload: dict[str, Any]) -> Decision:
        future: asyncio.Future[Decision] = asyncio.get_running_loop().create_future()
        self._pending[call_id] = (session_id, future)
        bus.publish(session_id, {"type": "approval_required", "call_id": call_id, **payload})
        model_id = payload.get("model_id")
        prev = board.set(session_id, model_id, APPROVAL, approval_text(payload)) if model_id else None
        try:
            return await future
        finally:
            self._pending.pop(call_id, None)
            bus.publish(session_id, {"type": "approval_resolved", "call_id": call_id})
            if model_id:
                board.restore(session_id, model_id, prev)

    def resolve(self, session_id: int, call_id: str, decision: Decision) -> bool:
        entry = self._pending.get(call_id)
        if entry is None or entry[0] != session_id or entry[1].done():
            return False
        entry[1].set_result(decision)
        return True

    def pending_for(self, session_id: int) -> list[str]:
        return [cid for cid, (sid, _) in self._pending.items() if sid == session_id]


approvals = Approvals()
