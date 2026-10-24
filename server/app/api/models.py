from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.providers.agent_cli import cursor_models
from app.schemas import ModelIn, ModelOut, ModelPatch
from app.tables import Endpoint, Model

router = APIRouter(prefix="/api/models", tags=["models"])

PALETTE = ["#2563eb", "#d97706", "#059669", "#db2777", "#7c3aed", "#0891b2", "#dc2626", "#65a30d"]


def model_out(m: Model, ep: Endpoint) -> ModelOut:
    out = ModelOut.model_validate(m)
    out.endpoint_name = ep.name
    out.endpoint_kind = ep.kind
    out.enabled = m.enabled and ep.enabled  # a disabled endpoint (e.g. removed from config.yaml) hides its models
    out.effort_levels = cursor_models.effort_levels(m.cli_variants)
    out.context_choices = cursor_models.context_choices(m.cli_variants)
    return out


@router.get("", response_model=list[ModelOut])
async def list_models(enabled_only: bool = False, db: AsyncSession = Depends(get_db)) -> list[ModelOut]:
    q = select(Model, Endpoint).join(Endpoint, Model.endpoint_id == Endpoint.id).order_by(Endpoint.name, Model.id)
    if enabled_only:
        q = q.where(Model.enabled.is_(True), Endpoint.enabled.is_(True))
    return [model_out(m, ep) for m, ep in (await db.execute(q)).all()]


@router.post("", response_model=ModelOut, status_code=201)
async def create_model(body: ModelIn, db: AsyncSession = Depends(get_db)) -> ModelOut:
    ep = await db.get(Endpoint, body.endpoint_id)
    if ep is None:
        raise HTTPException(404, "endpoint not found")
    count = len((await db.execute(select(Model.id))).all())
    color = body.color if body.color != "#6b7280" else PALETTE[count % len(PALETTE)]
    m = Model(
        endpoint_id=ep.id,
        model_name=body.model_name,
        display_name=body.display_name or body.model_name,
        color=color,
        input_price=body.input_price,
        output_price=body.output_price,
        enabled=body.enabled,
        supports_tools=body.supports_tools,
        source="api",
    )
    db.add(m)
    await db.commit()
    return model_out(m, ep)


@router.patch("/{model_id}", response_model=ModelOut)
async def update_model(model_id: int, body: ModelPatch, db: AsyncSession = Depends(get_db)) -> ModelOut:
    m = await db.get(Model, model_id)
    if m is None:
        raise HTTPException(404, "model not found")
    if m.source == "config":
        raise HTTPException(409, "this model is managed by config.yaml on the server")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(m, field, value)
    await db.commit()
    ep = await db.get(Endpoint, m.endpoint_id)
    return model_out(m, ep)


@router.delete("/{model_id}", status_code=204)
async def delete_model(model_id: int, db: AsyncSession = Depends(get_db)) -> None:
    m = await db.get(Model, model_id)
    if m is None:
        raise HTTPException(404, "model not found")
    if m.source == "config":
        raise HTTPException(409, "this model is managed by config.yaml on the server")
    # Keep history/stats: disable instead of deleting when the model was ever used.
    from app.tables import Message

    used = (await db.execute(select(Message.id).where(Message.model_id == m.id).limit(1))).first()
    if used:
        m.enabled = False
    else:
        await db.delete(m)
    await db.commit()
