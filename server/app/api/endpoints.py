import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import availability, crypto
from app.config_sync import is_openrouter
from app.db import get_db
from app.providers.base import ChatMessage, ProviderError, TextDelta
from app.providers.registry import PRESETS, provider_for
from app.schemas import EndpointIn, EndpointOut, EndpointPatch, ModelOut
from app.tables import Endpoint, Model

router = APIRouter(prefix="/api/endpoints", tags=["endpoints"])


def endpoint_out(row: Endpoint) -> EndpointOut:
    out = EndpointOut.model_validate(row)
    out.api_key_masked = crypto.mask(crypto.decrypt(row.api_key_enc))
    return out


async def _get(db: AsyncSession, endpoint_id: int) -> Endpoint:
    row = await db.get(Endpoint, endpoint_id)
    if row is None:
        raise HTTPException(404, "endpoint not found")
    return row


def _editable(row: Endpoint) -> None:
    if row.source == "config":
        raise HTTPException(409, "this endpoint is managed by config.yaml on the server")


@router.get("/presets")
async def presets() -> list[dict]:
    return PRESETS


@router.get("", response_model=list[EndpointOut])
async def list_endpoints(db: AsyncSession = Depends(get_db)) -> list[EndpointOut]:
    rows = (await db.execute(select(Endpoint).order_by(Endpoint.name))).scalars()
    return [endpoint_out(r) for r in rows]


@router.post("", response_model=EndpointOut, status_code=201)
async def create_endpoint(body: EndpointIn, db: AsyncSession = Depends(get_db)) -> EndpointOut:
    if body.kind == "agent_cli" and not body.driver:
        raise HTTPException(422, "agent_cli endpoints need a driver (claude_code, cursor or copilot)")
    if (await db.execute(select(Endpoint).where(Endpoint.name == body.name))).scalar_one_or_none():
        raise HTTPException(409, "an endpoint with this name exists")
    row = Endpoint(
        name=body.name,
        kind=body.kind,
        driver=body.driver,
        base_url=body.base_url,
        api_key_enc=crypto.encrypt(body.api_key),
        enabled=body.enabled,
        auto_models=body.auto_models if body.auto_models is not None else (is_openrouter(body.base_url) or body.driver == "cursor"),
        source="api",
    )
    db.add(row)
    await db.commit()
    asyncio.create_task(availability.check_all(row.id))
    return endpoint_out(row)


@router.patch("/{endpoint_id}", response_model=EndpointOut)
async def update_endpoint(endpoint_id: int, body: EndpointPatch, db: AsyncSession = Depends(get_db)) -> EndpointOut:
    row = await _get(db, endpoint_id)
    _editable(row)
    if body.name is not None:
        row.name = body.name
    if body.base_url is not None:
        row.base_url = body.base_url or None
    if body.api_key is not None:
        row.api_key_enc = crypto.encrypt(body.api_key)
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.auto_models is not None:
        row.auto_models = body.auto_models
    await db.commit()
    asyncio.create_task(availability.check_all(row.id))
    return endpoint_out(row)


@router.delete("/{endpoint_id}", status_code=204)
async def delete_endpoint(endpoint_id: int, db: AsyncSession = Depends(get_db)) -> None:
    row = await _get(db, endpoint_id)
    _editable(row)
    await db.delete(row)
    await db.commit()


@router.post("/check")
async def check_endpoints() -> list[dict]:
    """Re-check every endpoint's key and model list now (also runs at start-up and periodically)."""
    return await availability.check_all()


@router.post("/{endpoint_id}/check")
async def check_endpoint(endpoint_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    await _get(db, endpoint_id)
    return (await availability.check_all(endpoint_id))[0]


@router.post("/{endpoint_id}/test")
async def test_endpoint(endpoint_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await _get(db, endpoint_id)
    if row.kind == "agent_cli":
        return {"ok": True, "detail": "agent CLIs are tested on the runner/SSH host when a session uses them"}
    provider = provider_for(row)
    try:
        models = await provider.list_models()
        if models:
            return {"ok": True, "detail": f"{len(models)} models available"}
        model = (await db.execute(select(Model).where(Model.endpoint_id == row.id))).scalars().first()
        if model is None:
            return {"ok": True, "detail": "endpoint reachable; add a model to run a test prompt"}
        text = ""
        async for ev in provider.stream(model.model_name, "", [ChatMessage(role="user", content="Say OK")], []):
            if isinstance(ev, TextDelta):
                text += ev.text
        return {"ok": True, "detail": f"{model.display_name} replied: {text[:80]}"}
    except ProviderError as exc:
        return {"ok": False, "detail": str(exc)[:500]}


@router.get("/{endpoint_id}/available-models")
async def available_models(endpoint_id: int, db: AsyncSession = Depends(get_db)) -> list[dict]:
    row = await _get(db, endpoint_id)
    if row.kind == "agent_cli":
        return []
    try:
        return await provider_for(row).list_models()
    except ProviderError as exc:
        raise HTTPException(502, str(exc)[:500]) from exc


@router.post("/{endpoint_id}/import-models", response_model=list[ModelOut])
async def import_models(endpoint_id: int, ids: list[str], db: AsyncSession = Depends(get_db)) -> list[ModelOut]:
    """Add the given vendor model ids (with vendor pricing when the vendor reports it)."""
    from app.api.models import PALETTE, model_out

    row = await _get(db, endpoint_id)
    catalog = {m["id"]: m for m in await available_models(endpoint_id, db)}
    taken = len((await db.execute(select(Model.id))).all())
    added = []
    for model_id in ids:
        existing = (
            await db.execute(select(Model).where(Model.endpoint_id == row.id, Model.model_name == model_id))
        ).scalar_one_or_none()
        if existing:
            continue
        info = catalog.get(model_id, {})
        m = Model(
            endpoint_id=row.id,
            model_name=model_id,
            display_name=info.get("display_name") or model_id.split("/")[-1],
            color=PALETTE[(taken + len(added)) % len(PALETTE)],
            input_price=info.get("input_price", 0.0),
            output_price=info.get("output_price", 0.0),
            source="api",
        )
        db.add(m)
        added.append(m)
    await db.commit()
    return [model_out(m, row) for m in added]
