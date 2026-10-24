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


async def test_shared_mode_models_see_each_other(client):
    models = await models_by_name(client)
    ids = [models[n]["id"] for n in ("mock-alpha", "mock-beta", "mock-gamma")]
    s = (await client.post("/api/sessions", json={"title": "shared", "context_mode": "shared", "model_ids": ids})).json()
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
