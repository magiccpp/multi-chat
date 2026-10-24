"""Endpoint checks: keys, vendor model lists, catalog import, and greying out unavailable models."""

import asyncio
import json

import pytest
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from tests.conftest import free_port

vendor = FastAPI()
CATALOG = ["good-model", "vision-model", "text-embedding-3-small", "whisper-1"]


@vendor.get("/v1/models")
async def models(authorization: str = Header(default="")) -> dict:
    if authorization != "Bearer right-key":
        raise HTTPException(401, {"error": {"message": "Incorrect API key provided"}})
    return {"object": "list", "data": [{"id": m, "object": "model", "created": 0, "owned_by": "x"} for m in CATALOG]}


CHAT_REQUESTS: list[dict] = []


@vendor.post("/v1/chat/completions")
async def chat_completions(body: dict) -> StreamingResponse:
    """Answers "ok" as a stream and remembers the request (to see which tools a model was offered)."""
    CHAT_REQUESTS.append(body)

    def chunks():
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": body["model"]}
        yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': None}]})}\n\n"
        yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
        yield f"data: {json.dumps({**base, 'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 1, 'total_tokens': 11}})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(chunks(), media_type="text/event-stream")


@pytest.fixture(scope="module")
async def vendor_url():
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(vendor, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}/v1"
    srv.should_exit = True
    await task


async def models_of(client, endpoint_id):
    return {m["model_name"]: m for m in (await client.get("/api/models")).json() if m["endpoint_id"] == endpoint_id}


async def test_catalog_import_and_availability(client, vendor_url):
    ep = (await client.post("/api/endpoints", json={"name": "vendor-ok", "kind": "openai_compatible", "base_url": vendor_url,
                                                    "api_key": "right-key", "auto_models": True})).json()
    await client.post("/api/models", json={"endpoint_id": ep["id"], "model_name": "gone-model", "display_name": "Gone"})
    status = (await client.post(f"/api/endpoints/{ep['id']}/check")).json()
    assert status["status"] == "ok" and "4 models" in status["detail"]
    models = await models_of(client, ep["id"])
    assert {"good-model", "vision-model"} <= set(models)  # chat models imported from the catalog
    assert "text-embedding-3-small" not in models and "whisper-1" not in models  # non-chat models skipped
    assert models["good-model"]["available"] and models["good-model"]["source"] == "catalog"
    assert not models["gone-model"]["available"] and "not offered" in models["gone-model"]["availability_detail"]

    # only available models can join a session
    r = await client.post("/api/sessions", json={"title": "x", "model_ids": [models["gone-model"]["id"]]})
    assert r.status_code == 422 and "unavailable" in r.json()["detail"]
    s = (await client.post("/api/sessions", json={"title": "x", "model_ids": [models["good-model"]["id"]]})).json()
    assert [p["model_id"] for p in s["participants"]] == [models["good-model"]["id"]]

    eps = {e["name"]: e for e in (await client.get("/api/endpoints")).json()}
    assert eps["vendor-ok"]["status"] == "ok" and eps["vendor-ok"]["checked_at"]


async def test_rejected_key_greys_out_models(client, vendor_url):
    ep = (await client.post("/api/endpoints", json={"name": "vendor-bad", "kind": "openai_compatible", "base_url": vendor_url,
                                                    "api_key": "wrong-key"})).json()
    m = (await client.post("/api/models", json={"endpoint_id": ep["id"], "model_name": "good-model"})).json()
    status = (await client.post(f"/api/endpoints/{ep['id']}/check")).json()
    assert status["status"] == "error" and "Incorrect API key" in status["detail"]
    model = (await models_of(client, ep["id"]))["good-model"]
    assert not model["available"] and "vendor-bad" in model["availability_detail"]
    # fixing the key makes it available again
    await client.patch(f"/api/endpoints/{ep['id']}", json={"api_key": "right-key"})
    assert (await client.post(f"/api/endpoints/{ep['id']}/check")).json()["status"] == "ok"
    assert (await models_of(client, ep["id"]))["good-model"]["available"]
    assert m["id"] == (await models_of(client, ep["id"]))["good-model"]["id"]


async def test_missing_key_and_check_all(client):
    ep = (await client.post("/api/endpoints", json={"name": "nokey", "kind": "openai_compatible",
                                                    "base_url": "https://api.example.com/v1"})).json()
    await client.post("/api/models", json={"endpoint_id": ep["id"], "model_name": "m1"})
    results = {r["name"]: r for r in (await client.post("/api/endpoints/check")).json()}
    assert results["nokey"]["status"] == "no_key"
    assert results["mock"]["status"] == "ok"
    model = (await models_of(client, ep["id"]))["m1"]
    assert not model["available"] and "no API key" in model["availability_detail"]


def test_builtin_openrouter_endpoint():
    from app.config_sync import with_builtin_endpoints

    mock = {"name": "mock", "kind": "mock", "base_url": "mock://local"}
    # no key in the environment: nothing added
    assert with_builtin_endpoints([mock], {}) == [mock]
    # key set, config doesn't mention OpenRouter: it is added with the whole catalog
    eps = with_builtin_endpoints([mock], {"OPENROUTER_API_KEY": "sk-or-test"})
    assert eps[1] == {"name": "openrouter", "kind": "openai_compatible", "base_url": "https://openrouter.ai/api/v1",
                      "api_key": "sk-or-test", "auto_models": True, "models": []}
    # an OpenRouter entry without a key (old example config) gets the key; no duplicate
    old = {"name": "openrouter", "kind": "openai_compatible", "base_url": "https://openrouter.ai/api/v1", "api_key": "", "models": []}
    eps = with_builtin_endpoints([old], {"OPENROUTER_API_KEY": "sk-or-test"})
    assert len(eps) == 1 and eps[0]["api_key"] == "sk-or-test"
    # one added from a client already covers it
    assert with_builtin_endpoints([mock], {"OPENROUTER_API_KEY": "k"}, skip_openrouter=True) == [mock]


async def test_endpoint_without_models_imports_catalog(client, vendor_url):
    ep = (await client.post("/api/endpoints", json={"name": "vendor-empty", "kind": "openai_compatible", "base_url": vendor_url,
                                                    "api_key": "right-key"})).json()
    assert ep["auto_models"] is False
    assert (await client.post(f"/api/endpoints/{ep['id']}/check")).json()["status"] == "ok"
    models = await models_of(client, ep["id"])
    assert {"good-model", "vision-model"} <= set(models) and all(m["available"] for m in models.values())


async def test_openrouter_endpoint_imports_by_default(client):
    ep = (await client.post("/api/endpoints", json={"name": "or-from-app", "kind": "openai_compatible",
                                                    "base_url": "https://openrouter.ai/api/v1"})).json()
    assert ep["auto_models"] is True
    await client.patch(f"/api/endpoints/{ep['id']}", json={"enabled": False})


async def test_config_without_models_fetches_vendor_list(client, vendor_url):
    from app import db as dbmod
    from app.config import get_settings
    from app.config_sync import load_config, sync_config

    base = load_config(get_settings().config_file)  # with ${ENV} references expanded, like the server

    async def sync(models):
        ep = {"name": "dyn", "kind": "openai_compatible", "base_url": vendor_url, "api_key": "right-key"}
        if models is not None:
            ep["models"] = models
        async with dbmod.sessionmaker()() as db:
            await sync_config(db, {**base, "endpoints": [*base["endpoints"], ep]})
        eid = next(e["id"] for e in (await client.get("/api/endpoints")).json() if e["name"] == "dyn")
        assert (await client.post(f"/api/endpoints/{eid}/check")).json()["status"] == "ok"
        return await models_of(client, eid)

    # one model listed: only that one
    assert set(await sync([{"name": "good-model", "display_name": "Good"}])) == {"good-model"}
    # models removed from the file (key omitted): the vendor's list is fetched
    models = await sync(None)
    assert {"good-model", "vision-model"} <= set(models)
    assert models["good-model"]["source"] == "catalog" and all(m["available"] for m in models.values())
    async with dbmod.sessionmaker()() as db:
        await sync_config(db, base)  # restore: "dyn" disabled again


async def test_missing_prices_come_from_openrouter_and_reprice_usage(client, vendor_url, monkeypatch):
    """OpenAI-style vendors report no prices: OpenRouter's list price fills them and earlier $0 usage is re-priced."""
    from sqlalchemy import select

    from app import db as dbmod
    from app import pricing
    from app.tables import Usage

    async def fake_prices():
        return {pricing._norm("openai/good-model"): (2.0, 8.0)}

    monkeypatch.setattr(pricing, "openrouter_prices", fake_prices)
    monkeypatch.setattr(pricing, "VENDORS", [("127.0.0.1", "openai/")])  # the fake vendor plays OpenAI
    ep = (await client.post("/api/endpoints", json={"name": "vendor-priced", "kind": "openai_compatible", "base_url": vendor_url,
                                                    "api_key": "right-key"})).json()
    m = (await client.post("/api/models", json={"endpoint_id": ep["id"], "model_name": "good-model"})).json()
    s = (await client.post("/api/sessions", json={"title": "pricing", "model_ids": [m["id"]]})).json()
    # usage recorded while the price was unknown
    async with dbmod.sessionmaker()() as db:
        from app.tables import Message
        msg = Message(session_id=s["id"], seq=1, turn_id="t", role="assistant", model_id=m["id"], phase="plan", content="hi", status="complete")
        db.add(msg)
        await db.flush()
        db.add(Usage(message_id=msg.id, model_id=m["id"], session_id=s["id"], input_tokens=1_000_000, output_tokens=500_000, cost=0.0))
        await db.commit()
    before = (await client.get(f"/api/sessions/{s['id']}/usage")).json()[0]
    assert before["cost"] == 0 and before["price_known"] is False
    await client.post(f"/api/endpoints/{ep['id']}/check")
    model = (await models_of(client, ep["id"]))["good-model"]
    assert (model["input_price"], model["output_price"]) == (2.0, 8.0)
    after = (await client.get(f"/api/sessions/{s['id']}/usage")).json()[0]
    assert after["price_known"] and abs(after["cost"] - 6.0) < 1e-9  # 1M * $2 + 0.5M * $8
    assert abs(after["input_cost"] - 2.0) < 1e-9 and abs(after["output_cost"] - 4.0) < 1e-9 and after["replies"] == 1
    async with dbmod.sessionmaker()() as db:
        assert (await db.execute(select(Usage.cost).where(Usage.session_id == s["id"]))).scalar_one() == 6.0


def test_price_lookup_matches_vendor_ids():
    from app import pricing

    prices = {pricing._norm(k): v for k, v in {"anthropic/claude-sonnet-4.5": (3, 15), "openai/gpt-5.6-luna": (1, 4),
                                                "deepseek/deepseek-chat": (0.3, 1.2)}.items()}
    assert pricing.lookup(prices, "anthropic/", "claude-sonnet-4-5-20250929") == (3, 15)
    assert pricing.lookup(prices, "openai/", "gpt-5.6-luna") == (1, 4)
    assert pricing.lookup(prices, "", "openai/gpt-5.6-luna") == (1, 4)  # GitHub Models ids
    assert pricing.lookup(prices, "deepseek/", "deepseek-chat") == (0.3, 1.2)
    assert pricing.lookup(prices, "openai/", "unknown-model") is None
    assert pricing.vendor_prefix("openai_compatible", "https://api.openai.com/v1") == "openai/"
    assert pricing.vendor_prefix("anthropic", None) == "anthropic/"
    assert pricing.vendor_prefix("openai_compatible", "https://openrouter.ai/api/v1") is None


async def test_listed_models_all_gone_imports_the_vendor_list(client, vendor_url):
    # e.g. config.yaml still lists a model the vendor renamed: offer what the vendor has instead of a grey endpoint
    ep = (await client.post("/api/endpoints", json={"name": "vendor-renamed", "kind": "openai_compatible",
                                                    "base_url": vendor_url, "api_key": "right-key"})).json()
    await client.post("/api/models", json={"endpoint_id": ep["id"], "model_name": "old-name", "display_name": "Old"})
    assert (await client.post(f"/api/endpoints/{ep['id']}/check")).json()["status"] == "ok"
    models = await models_of(client, ep["id"])
    assert models["good-model"]["available"] and not models["old-name"]["available"]


async def test_vendor_without_capabilities_keeps_tools(client, vendor_url, monkeypatch):
    """DeepSeek / OpenAI list models without capabilities: imported models keep tools (so web_fetch etc. are offered),
    OpenRouter's entry for the same model fills in images / effort, and a row saved earlier with tools off is repaired."""
    from sqlalchemy import update

    from app import db as dbmod
    from app import pricing
    from app.tables import Model

    async def fake_caps():
        return {pricing._norm("deepseek/good-model"): {"tools": True, "images": False, "effort": True}}

    monkeypatch.setattr(pricing, "openrouter_capabilities", fake_caps)
    monkeypatch.setattr(pricing, "VENDORS", [("127.0.0.1", "deepseek/")])  # the fake vendor plays DeepSeek
    ep = (await client.post("/api/endpoints", json={"name": "vendor-deepseek", "kind": "openai_compatible",
                                                    "base_url": vendor_url, "api_key": "right-key"})).json()
    await client.post(f"/api/endpoints/{ep['id']}/check")
    models = await models_of(client, ep["id"])
    good, vision = models["good-model"], models["vision-model"]
    assert good["supports_tools"] and not good["supports_images"] and good["supports_effort"]
    assert vision["supports_tools"] and vision["supports_images"]  # unknown to OpenRouter: assume yes
    async with dbmod.sessionmaker()() as db:  # what the bug saved before this fix
        await db.execute(update(Model).where(Model.id == vision["id"]).values(supports_tools=False))
        await db.commit()
    await client.post(f"/api/endpoints/{ep['id']}/check")
    assert (await models_of(client, ep["id"]))["vision-model"]["supports_tools"]


async def test_imported_vendor_model_is_offered_tools_in_a_turn(client, vendor_url, monkeypatch):
    """End to end: a model imported from a vendor that reports no capabilities (DeepSeek direct) gets tools in its
    chat request, so it can use web_fetch (and run_command when a folder is attached)."""
    from app import pricing
    from tests.conftest import chat

    async def no_caps():
        return {}

    monkeypatch.setattr(pricing, "openrouter_capabilities", no_caps)
    monkeypatch.setattr(pricing, "VENDORS", [("127.0.0.1", "deepseek/")])
    ep = (await client.post("/api/endpoints", json={"name": "vendor-deepseek-chat", "kind": "openai_compatible",
                                                    "base_url": vendor_url, "api_key": "right-key"})).json()
    await client.post(f"/api/endpoints/{ep['id']}/check")
    model = (await models_of(client, ep["id"]))["good-model"]
    s = (await client.post("/api/sessions", json={"title": "tools", "model_ids": [model["id"]]})).json()
    CHAT_REQUESTS.clear()
    await chat(client, s["id"], "check the server")
    names = [t["function"]["name"] for t in CHAT_REQUESTS[-1].get("tools") or []]
    assert "web_fetch" in names, CHAT_REQUESTS[-1].keys()
