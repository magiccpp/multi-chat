import asyncio
import json
import os
import socket
import tempfile
from pathlib import Path

import pytest

_tmp = Path(tempfile.mkdtemp(prefix="multichat-test-"))
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://multichat:multichat@localhost:5432/multichat_test"
)
os.environ["MULTICHAT_API_TOKEN"] = "test-token"
os.environ["DATA_DIR"] = str(_tmp / "data")
os.environ["CONFIG_FILE"] = str(_tmp / "config.yaml")
os.environ["MOCK_KEY"] = "sk-mock-secret-1234"

(_tmp / "config.yaml").write_text(
    """
endpoints:
  - name: mock
    kind: mock
    base_url: mock://local
    api_key: ${MOCK_KEY}
    models:
      - {name: mock-alpha, display_name: Alpha, color: "#2563eb", input_price: 1.0, output_price: 2.0}
      - {name: mock-beta, display_name: Beta, color: "#d97706", input_price: 3.0, output_price: 15.0}
      - {name: mock-gamma, display_name: Gamma, color: "#059669", input_price: 0.5, output_price: 1.0}
web:
  allow_private: true
defaults:
  approve_edits: auto
  approve_commands: ask
"""
)

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import db as dbmod  # noqa: E402
from app.tables import Base  # noqa: E402

TOKEN = "test-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
async def server():
    engine = dbmod.engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    from app.main import app

    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    await task


@pytest.fixture
async def client(server):
    async with httpx.AsyncClient(base_url=server, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30) as c:
        yield c


async def models_by_name(client) -> dict[str, dict]:
    return {m["model_name"]: m for m in (await client.get("/api/models")).json()}


async def chat(client, session_id: int, content: str, recipients=None, attachment_ids=None) -> list[dict]:
    """Send a message and collect all SSE events until turn_end."""
    events = []
    body = {"content": content, "recipients": recipients, "attachment_ids": attachment_ids or []}
    async with client.stream("POST", f"/api/sessions/{session_id}/chat", json=body) as resp:
        assert resp.status_code == 200, await resp.aread()
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def finished(events: list[dict]) -> list[dict]:
    return [e["message"] for e in events if e["type"] == "message_end"]
