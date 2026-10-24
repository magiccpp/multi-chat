"""Live smoke test against OpenRouter with cheap tool-capable models.

Skipped unless OPENROUTER_API_KEY is set. Picks the three cheapest models that support
tool calling, runs a shared PLAN turn, votes, picks a builder and lets it edit a file
through a runner. Costs a fraction of a cent.
"""

import asyncio
import os

import httpx
import pytest
from multichat_client.runner.agent import Runner

from tests.conftest import TOKEN, chat, finished

KEY = os.environ.get("OPENROUTER_API_KEY")
pytestmark = pytest.mark.skipif(not KEY, reason="set OPENROUTER_API_KEY to run live OpenRouter tests")
PREFERRED = os.environ.get("MULTICHAT_LIVE_MODELS", "")  # optional comma-separated override


async def cheap_models(n: int = 3) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as c:
        data = (await c.get("https://openrouter.ai/api/v1/models")).json()["data"]
    if PREFERRED:
        wanted = [m.strip() for m in PREFERRED.split(",")]
        return [m for m in data if m["id"] in wanted]
    rows = []
    for m in data:
        p = m.get("pricing") or {}
        try:
            pin, pout = float(p.get("prompt", 0)), float(p.get("completion", 0))
        except (TypeError, ValueError):
            continue
        if pin <= 0 or "tools" not in (m.get("supported_parameters") or []):
            continue
        rows.append((pin + pout, m))
    rows.sort(key=lambda r: r[0])
    picked, vendors = [], set()
    for _, m in rows:  # one model per vendor for variety
        vendor = m["id"].split("/")[0]
        if vendor not in vendors:
            picked.append(m)
            vendors.add(vendor)
        if len(picked) == n:
            break
    return picked


async def test_live_openrouter_plan_vote_build(client, server, tmp_path):
    models = await cheap_models()
    assert len(models) >= 2, "no cheap tool-capable models found"
    ep = (await client.post("/api/endpoints", json={"name": f"openrouter-live-{tmp_path.name}", "kind": "openai_compatible",
                                                    "base_url": "https://openrouter.ai/api/v1", "api_key": KEY})).json()
    imported = (await client.post(f"/api/endpoints/{ep['id']}/import-models", json=[m["id"] for m in models])).json()
    assert all(m["input_price"] > 0 for m in imported)  # OpenRouter prices were imported
    ids = [m["id"] for m in imported]

    root = tmp_path / "proj"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    runner = Runner(server, TOKEN, [str(root)], name=f"live-{tmp_path.name}")
    task = asyncio.create_task(runner.run_forever())
    for _ in range(100):
        if runner.runner_id:
            break
        await asyncio.sleep(0.05)

    s = (await client.post("/api/sessions", json={"title": "live", "context_mode": "shared", "model_ids": ids,
                                                  "workspace": {"kind": "runner", "runner_id": runner.runner_id, "path": str(root)}})).json()
    events = await chat(client, s["id"], "calc.py has a bug in add(). Look at the file and say in two sentences what is wrong.")
    replies = [r for r in finished(events) if r["role"] == "assistant" and r["content"].strip()]
    print("\n".join(f"--- {r['model_id']}: {r['content'][:300]}" for r in replies))
    assert len({r["model_id"] for r in replies}) == len(ids)

    usage = (await client.get(f"/api/sessions/{s['id']}/usage")).json()
    assert all(u["input_tokens"] > 0 and not u["estimated"] for u in usage)
    print("usage:", usage)

    best = replies[0]
    await client.put(f"/api/messages/{best['id']}/vote", json={"value": 1})
    await client.post(f"/api/sessions/{s['id']}/pick-builder", json={"model_id": best["model_id"]})
    await client.patch(f"/api/sessions/{s['id']}", json={"approve_edits": "auto", "approve_commands": "auto"})
    events = await chat(client, s["id"], "Fix the bug in calc.py with edit_file, then run `python3 -c \"import calc; print(calc.add(2, 3))\"` to verify.")
    print([e["message"]["content"][:200] for e in events if e["type"] == "tool_result"])
    assert "a + b" in (root / "calc.py").read_text()

    stats = (await client.get("/api/stats")).json()
    print("total cost $", stats["totals"]["cost"])
    await runner.stop()
    task.cancel()
