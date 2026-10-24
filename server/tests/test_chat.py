import re

import httpx

from tests.conftest import chat, finished, models_by_name


async def test_auth_required(server):
    async with httpx.AsyncClient(base_url=server) as anon:
        assert (await anon.get("/api/health")).status_code == 200
        assert (await anon.get("/api/models")).status_code == 401
        assert (await anon.get("/api/models", headers={"Authorization": "Bearer wrong"})).status_code == 401


async def test_config_sync_and_masking(client):
    endpoints = (await client.get("/api/endpoints")).json()
    mock = next(e for e in endpoints if e["name"] == "mock")
    assert mock["source"] == "config"
    assert mock["api_key_masked"] == "sk-…1234"
    assert "sk-mock-secret-1234" not in str(endpoints)
    # config-managed rows are read-only through the API
    assert (await client.patch(f"/api/endpoints/{mock['id']}", json={"name": "x"})).status_code == 409
    models = await models_by_name(client)
    assert {"mock-alpha", "mock-beta", "mock-gamma"} <= set(models)
    assert models["mock-beta"]["output_price"] == 15.0


async def test_isolated_plan_fans_out_in_parallel(client):
    models = await models_by_name(client)
    ids = [models[n]["id"] for n in ("mock-alpha", "mock-beta", "mock-gamma")]
    s = (await client.post("/api/sessions", json={"title": "iso", "model_ids": ids})).json()
    assert [p["display_name"] for p in s["participants"]] == ["Alpha", "Beta", "Gamma"]

    events = await chat(client, s["id"], "How should we fix the login bug?")
    replies = finished(events)
    assert sorted(r["model_id"] for r in replies) == sorted(ids)
    for r in replies:
        assert "fix the login bug" in r["content"]
        assert "other answer" not in r["content"]  # isolated: nobody sees the others
    assert events[-1]["type"] == "turn_end" and events[-1]["status"] == "complete"

    usage = (await client.get(f"/api/sessions/{s['id']}/usage")).json()
    assert len(usage) == 3
    assert all(u["input_tokens"] > 0 and u["output_tokens"] > 0 and u["cost"] > 0 for u in usage)

    # Second turn addressed to one model only.
    events = await chat(client, s["id"], "Alpha, elaborate", recipients=[ids[0]])
    assert [r["model_id"] for r in finished(events)] == [ids[0]]


async def test_shared_mode_runs_in_parallel_and_sees_earlier_turns(client):
    # a slow mock so the overlap is visible (the default test mock answers instantly)
    ep = (await client.post("/api/endpoints", json={"name": "slow-mock", "kind": "mock", "base_url": "mock://local?delay=0.03"})).json()
    ids = [(await client.post("/api/models", json={"endpoint_id": ep["id"], "model_name": f"slow-{n}", "display_name": n})).json()["id"]
           for n in ("alpha", "beta", "gamma")]
    s = (await client.post("/api/sessions", json={"title": "shared", "context_mode": "shared", "model_ids": ids})).json()
    events = await chat(client, s["id"], "Design a cache layer")
    starts = [i for i, e in enumerate(events) if e["type"] == "message_start" and e["message"]["role"] == "assistant"]
    first_end = next(i for i, e in enumerate(events) if e["type"] == "message_end")
    assert len(starts) == 3 and max(starts) < first_end  # all three started before any finished: parallel
    replies = finished(events)
    assert all("other answer" not in r["content"] for r in replies), [r["content"] for r in replies]  # same-turn replies are still being written
    replies = finished(await chat(client, s["id"], "Which design is best?"))
    # every model now sees last turn's replies; the mock counts those after its own reply, so the
    # counts are 0, 1 and 2 depending on the order the turn-1 replies were created
    counts = sorted(int(m.group(1)) if (m := re.search(r"I read (\d) other", r["content"])) else 0 for r in replies)
    assert counts == [0, 1, 2], [r["content"] for r in replies]


async def test_relay_mode_answers_one_after_another(client):
    models = await models_by_name(client)
    ids = [models[n]["id"] for n in ("mock-alpha", "mock-beta", "mock-gamma")]
    s = (await client.post("/api/sessions", json={"title": "relay", "context_mode": "relay", "model_ids": ids})).json()
    replies = finished(await chat(client, s["id"], "Design a cache layer"))
    assert [r["model_id"] for r in replies] == ids  # sequential in session order
    assert "other answer" not in replies[0]["content"]
    assert "I read 1 other answer." in replies[1]["content"]
    assert "I read 2 other answers." in replies[2]["content"]


async def test_votes_builder_and_stats(client):
    models = await models_by_name(client)
    ids = [models[n]["id"] for n in ("mock-alpha", "mock-beta")]
    s = (await client.post("/api/sessions", json={"title": "votes", "model_ids": ids})).json()
    replies = finished(await chat(client, s["id"], "Plan the refactor"))
    by_model = {r["model_id"]: r for r in replies}

    r = await client.put(f"/api/messages/{by_model[ids[0]]['id']}/vote", json={"value": 1})
    assert r.status_code == 200
    r = await client.put(f"/api/messages/%23{by_model[ids[1]]['seq']}/vote", params={"session_id": s["id"]}, json={"value": -1})
    assert r.status_code == 200 and r.json()["message_id"] == by_model[ids[1]]["id"]
    msgs = (await client.get(f"/api/sessions/{s['id']}/messages")).json()
    assert {m["id"]: m["vote"] for m in msgs}[by_model[ids[0]]["id"]] == 1
    user_msg = next(m for m in msgs if m["role"] == "user")
    assert (await client.put(f"/api/messages/{user_msg['id']}/vote", json={"value": 1})).status_code == 422

    # BUILD without a builder is refused; pick one and build.
    assert (await client.patch(f"/api/sessions/{s['id']}", json={"phase": "build"})).status_code == 422
    s2 = (await client.post(f"/api/sessions/{s['id']}/pick-builder", json={"model_id": ids[0]})).json()
    assert s2["phase"] == "build" and s2["builder_model_id"] == ids[0]
    replies = finished(await chat(client, s["id"], 'Implement it\n@tool write_file {"path": "a.txt", "content": "x"}'))
    assert [r["model_id"] for r in replies] == [ids[0]]
    assert "Calling" not in replies[0]["content"]  # no folder attached: file tools are not offered

    stats = (await client.get("/api/stats")).json()
    rows = {r["model_id"]: r for r in stats["models"]}
    assert rows[ids[0]]["upvotes"] >= 1 and rows[ids[0]]["builder_picks"] >= 1
    assert rows[ids[1]]["downvotes"] >= 1
    assert rows[ids[0]]["win_rate"] is not None
    assert stats["totals"]["cost"] > 0


async def test_join_and_leave_mid_session(client):
    models = await models_by_name(client)
    a, b, g = (models[n]["id"] for n in ("mock-alpha", "mock-beta", "mock-gamma"))
    s = (await client.post("/api/sessions", json={"title": "join", "context_mode": "shared", "model_ids": [a]})).json()
    await chat(client, s["id"], "First question")
    s = (await client.put(f"/api/sessions/{s['id']}/models", json={"model_ids": [a, g]})).json()
    assert [p["model_id"] for p in s["participants"]] == [a, g]
    replies = finished(await chat(client, s["id"], "Second question"))
    gamma = next(r for r in replies if r["model_id"] == g)
    assert "I read" in gamma["content"]  # shared: the newcomer sees earlier replies
    s = (await client.put(f"/api/sessions/{s['id']}/models", json={"model_ids": [g]})).json()
    assert [r["model_id"] for r in finished(await chat(client, s["id"], "Third"))] == [g]
    assert b not in [p["model_id"] for p in s["participants"]]


async def test_config_sync_recovers_from_changed_secret_key(client):
    from app import db as dbmod
    from app.config_sync import load_config, sync_config
    from app.config import get_settings
    from app.tables import Endpoint
    from sqlalchemy import select

    async with dbmod.sessionmaker()() as db:
        row = (await db.execute(select(Endpoint).where(Endpoint.name == "mock"))).scalar_one()
        row.api_key_enc = "gAAAAAB-not-decryptable-with-this-key"
        await db.commit()
        await sync_config(db, load_config(get_settings().config_file))
    masked = next(e for e in (await client.get("/api/endpoints")).json() if e["name"] == "mock")["api_key_masked"]
    assert masked == "sk-…1234"


async def test_session_and_message_costs(client):
    models = await models_by_name(client)
    ids = [models["mock-alpha"]["id"], models["mock-beta"]["id"]]
    s = (await client.post("/api/sessions", json={"title": "costs", "model_ids": ids})).json()
    assert s["cost"] == 0 and s["tokens"] == 0
    await chat(client, s["id"], "Compare two approaches")
    usage = (await client.get(f"/api/sessions/{s['id']}/usage")).json()
    total = sum(u["cost"] for u in usage)
    assert total > 0
    session = (await client.get(f"/api/sessions/{s['id']}")).json()
    assert abs(session["cost"] - total) < 1e-9 and session["tokens"] == sum(u["input_tokens"] + u["output_tokens"] for u in usage)
    listed = next(x for x in (await client.get("/api/sessions")).json() if x["id"] == s["id"])
    assert abs(listed["cost"] - total) < 1e-9
    messages = (await client.get(f"/api/sessions/{s['id']}/messages")).json()
    replies = [m for m in messages if m["role"] == "assistant"]
    assert all(m["usage"] and m["usage"]["cost"] > 0 for m in replies)
    assert abs(sum(m["usage"]["cost"] for m in replies) - total) < 1e-9
    assert next(m for m in messages if m["role"] == "user")["usage"] is None


async def test_effort_and_context_window_per_model(client):
    await client.post("/api/endpoints/check")  # sets the demo models' context length and effort support
    models = await models_by_name(client)
    a, b = models["mock-alpha"]["id"], models["mock-beta"]["id"]
    assert models["mock-alpha"]["context_length"] == 128_000 and models["mock-alpha"]["supports_effort"]
    s = (await client.post("/api/sessions", json={
        "title": "settings", "model_ids": [a, b],
        "model_settings": {str(a): {"effort": "high", "context_window": 32_000}},
    })).json()
    parts = {p["model_id"]: p for p in s["participants"]}
    assert (parts[a]["effort"], parts[a]["context_window"], parts[a]["context_length"]) == ("high", 32_000, 128_000)
    assert (parts[b]["effort"], parts[b]["context_window"]) == (None, None)
    # too big for the model
    r = await client.patch(f"/api/sessions/{s['id']}/models/{b}", json={"context_window": 500_000})
    assert r.status_code == 422 and "at most 128,000" in r.json()["detail"]
    # still empty: the context window can be changed
    r = await client.patch(f"/api/sessions/{s['id']}/models/{b}", json={"context_window": 64_000})
    assert r.status_code == 200

    replies = {m["model_id"]: m for m in finished(await chat(client, s["id"], "Plan it"))}
    assert "(effort: high)" in replies[a]["content"] and "effort" not in replies[b]["content"]

    # effort changes mid-session; the context window is fixed now
    r = await client.patch(f"/api/sessions/{s['id']}/models/{a}", json={"effort": "low"})
    assert r.status_code == 200
    r = await client.patch(f"/api/sessions/{s['id']}/models/{a}", json={"context_window": 64_000})
    assert r.status_code == 409 and "can't be changed" in r.json()["detail"]
    r = await client.patch(f"/api/sessions/{s['id']}/models/{b}", json={"effort": None})  # back to default: fine
    assert r.status_code == 200
    replies = {m["model_id"]: m for m in finished(await chat(client, s["id"], "And now?"))}
    assert "(effort: low)" in replies[a]["content"]


async def test_compaction_at_80_percent_of_the_context_window(client):
    await client.post("/api/endpoints/check")
    a = (await models_by_name(client))["mock-gamma"]["id"]
    s = (await client.post("/api/sessions", json={"title": "window", "model_ids": [a],
                                                  "model_settings": {str(a): {"context_window": 4096}}})).json()
    long = "details " * 800  # ~1600 tokens per message
    first = finished(await chat(client, s["id"], "first " + long))[0]
    assert "summary" not in first["content"]
    # the second long message takes the history past 80% of 4096 tokens: the older part is summarised first
    events = await chat(client, s["id"], "second " + long)
    done = finished(events)
    summary = next(m for m in done if m["role"] == "summary")
    reply = next(m for m in done if m["role"] == "assistant")
    assert summary["content"].startswith("Context compacted: 2 earlier messages") and "Summary:" in summary["content"]
    assert "(working from a summary)" in reply["content"] and "left out" not in reply["content"]
    usage = (await client.get(f"/api/sessions/{s['id']}/usage")).json()[0]
    assert usage["replies"] == 3  # two replies + the summarising request, all billed to the model
    messages = (await client.get(f"/api/sessions/{s['id']}/messages")).json()
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "summary", "assistant"]
    # the summary stays in use on the next turn
    third = finished(await chat(client, s["id"], "third, short"))
    assert "(working from a summary)" in next(m for m in third if m["role"] == "assistant")["content"]


async def test_memory_reaches_every_model(client):
    await client.put("/api/memory", json={"text": ""})
    r = (await client.post("/api/memory/append", json={"text": "# Always answer in British English"})).json()
    assert r["text"] == "- Always answer in British English"
    r = (await client.post("/api/memory/append", json={"text": "The prod host is nuc2"})).json()
    assert r["text"].splitlines() == ["- Always answer in British English", "- The prod host is nuc2"]
    assert (await client.get("/api/memory")).json()["text"] == r["text"]
    models = await models_by_name(client)
    s = (await client.post("/api/sessions", json={"title": "memory", "model_ids": [models["mock-alpha"]["id"], models["mock-beta"]["id"]]})).json()
    replies = finished(await chat(client, s["id"], "hello"))
    assert all("(memory: 2 items)" in r["content"] for r in replies)
    await client.put("/api/memory", json={"text": ""})
    replies = finished(await chat(client, s["id"], "again"))
    assert all("memory:" not in r["content"] for r in replies)


async def test_context_report_and_manual_compact(client):
    await client.post("/api/endpoints/check")
    a = (await models_by_name(client))["mock-alpha"]["id"]
    s = (await client.post("/api/sessions", json={"title": "ctx", "model_ids": [a],
                                                  "model_settings": {str(a): {"context_window": 64_000}}})).json()
    await chat(client, s["id"], "one " + "words " * 400)
    await chat(client, s["id"], "two " + "words " * 400)
    report = (await client.get(f"/api/sessions/{s['id']}/context")).json()[0]
    assert report["window"] == 64_000 and 0 < report["percent"] < 80 and report["compactions"] == 0
    before = report["used"]
    result = (await client.post(f"/api/sessions/{s['id']}/compact", json={})).json()
    assert result == [{"model_id": a, "display_name": "Alpha", "compacted": True}]
    report = (await client.get(f"/api/sessions/{s['id']}/context")).json()[0]
    assert report["compactions"] == 1 and report["used"] < before
    roles = [m["role"] for m in (await client.get(f"/api/sessions/{s['id']}/messages")).json()]
    assert roles[-1] == "summary"
    reply = finished(await chat(client, s["id"], "three"))[0]
    assert "(working from a summary)" in reply["content"]
