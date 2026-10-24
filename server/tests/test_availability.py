"""Endpoint checks: keys, vendor model lists, catalog import, and greying out unavailable models."""

import asyncio

import pytest
import uvicorn
from fastapi import FastAPI, Header, HTTPException

from tests.conftest import free_port

vendor = FastAPI()
CATALOG = ["good-model", "vision-model", "text-embedding-3-small", "whisper-1"]


@vendor.get("/v1/models")
async def models(authorization: str = Header(default="")) -> dict:
    if authorization != "Bearer right-key":
        raise HTTPException(401, {"error": {"message": "Incorrect API key provided"}})
    return {"object": "list", "data": [{"id": m, "object": "model", "created": 0, "owned_by": "x"} for m in CATALOG]}


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
