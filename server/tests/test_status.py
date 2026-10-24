"""`status` events: what each model is waiting for, so the apps can show a tip during long waits."""

import asyncio
import json

from tests.conftest import chat, finished, models_by_name
from tests.test_runner import build_session, runner  # noqa: F401 - fixture


def statuses(events: list[dict], model_id: int | None = None) -> list[dict]:
    return [e for e in events if e["type"] == "status" and (model_id is None or e["model_id"] == model_id)]


async def test_relay_queue_waiting_writing_and_cleared(client):
    models = await models_by_name(client)
    ids = [models[n]["id"] for n in ("mock-alpha", "mock-beta", "mock-gamma")]
    s = (await client.post("/api/sessions", json={"title": "relay-status", "context_mode": "relay", "model_ids": ids})).json()
    events = await chat(client, s["id"], "Plan the migration")
    first = [e["state"] for e in statuses(events, ids[0])]
    assert first == ["waiting_model", "writing", None]
    for mid in ids[1:]:  # the later models wait for their turn first
        seq = [e["state"] for e in statuses(events, mid)]
        assert seq == ["queued", "waiting_model", "writing", None], seq
    waiting = statuses(events, ids[0])[0]
    assert waiting["text"] == "waiting for the reply to start (mock)"
    assert "Stop cancels the turn" in waiting["hint"]
    # every status is cleared before the turn ends
    end = next(i for i, e in enumerate(events) if e["type"] == "turn_end")
    assert all(i < end for i, e in enumerate(events) if e["type"] == "status")


async def test_thinking_models_say_so(client):
    ep = (await client.post("/api/endpoints", json={"name": "thinking-mock", "kind": "mock",
                                                     "base_url": "mock://local?think=0.05"})).json()
    mid = (await client.post("/api/models", json={"endpoint_id": ep["id"], "model_name": "slow-thinker",
                                                  "display_name": "Thinker"})).json()["id"]
    s = (await client.post("/api/sessions", json={"title": "think", "model_ids": [mid]})).json()
    events = await chat(client, s["id"], "Hard question")
    seq = [(e["state"], e["text"]) for e in statuses(events, mid)]
    assert seq[:3] == [("waiting_model", "waiting for the reply to start (thinking-mock)"),
                       ("thinking", "thinking before it answers"), ("writing", "writing its answer")]
    assert seq[-1] == (None, None)


async def test_tools_and_approval_are_described(client, runner):  # noqa: F811
    r, root = runner
    s = await build_session(client, r.runner_id, root, approve_commands="ask")

    async def approver():
        async with client.stream("GET", f"/api/sessions/{s['id']}/events") as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: ") and '"approval_required"' in line:
                    ev = json.loads(line[6:])
                    await client.post(f"/api/sessions/{s['id']}/approvals/{ev['call_id']}", json={"approve": True})
                    return

    task = asyncio.create_task(approver())
    await asyncio.sleep(0.2)
    events = await chat(client, s["id"], 'Check\n@tool read_file {"path": "hello.py"}\n@tool run_command {"command": "echo hi"}')
    await asyncio.wait_for(task, 5)
    seen = [(e["state"], e["text"]) for e in statuses(events)]
    states = [x[0] for x in seen]
    assert any(st == "tool" and t.startswith("reading hello.py on ") for st, t in seen), seen
    run = next(t for st, t in seen if st == "tool" and t.startswith("running `echo hi`"))
    assert "stops after" in run
    approval = states.index("approval")
    assert seen[approval][1] == "waiting for your approval to run `echo hi`"
    assert seen[approval + 1] == ("tool", run)  # back to the command once approved
    assert states[-1] is None
    assert finished(events)[-1]["content"].startswith("[mock-alpha] Done.")
