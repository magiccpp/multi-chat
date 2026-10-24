from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.tables import BuilderPick, Endpoint, Message, Model, SessionModel, Usage, Vote

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("")
async def stats(
    date_from: datetime | None = None, date_to: datetime | None = None, db: AsyncSession = Depends(get_db)
) -> dict:
    def window(col):  # noqa: ANN001, ANN202
        conds = []
        if date_from:
            conds.append(col >= date_from)
        if date_to:
            conds.append(col < date_to)
        return conds

    usage = {
        r.model_id: r
        for r in (
            await db.execute(
                select(
                    Usage.model_id,
                    func.sum(Usage.input_tokens).label("input_tokens"),
                    func.sum(Usage.output_tokens).label("output_tokens"),
                    func.sum(Usage.cost).label("cost"),
                    func.count(Usage.id).label("calls"),
                    func.avg(Usage.latency_ms).label("avg_latency_ms"),
                    func.avg(Usage.ttft_ms).label("avg_ttft_ms"),
                    func.sum(Usage.latency_ms).label("total_latency_ms"),
                    func.bool_or(Usage.estimated).label("estimated"),
                )
                .where(*window(Usage.created_at))
                .group_by(Usage.model_id)
            )
        ).all()
    }
    replies = {
        r.model_id: r
        for r in (
            await db.execute(
                select(
                    Message.model_id,
                    func.count(distinct(Message.turn_id)).label("turns"),
                    func.sum(case((Vote.value > 0, 1), else_=0)).label("up"),
                    func.sum(case((Vote.value < 0, 1), else_=0)).label("down"),
                )
                .outerjoin(Vote, Vote.message_id == Message.id)
                .where(Message.role == "assistant", Message.model_id.is_not(None), *window(Message.created_at))
                .group_by(Message.model_id)
            )
        ).all()
    }
    picks = dict(
        (
            await db.execute(
                select(BuilderPick.model_id, func.count(distinct(BuilderPick.session_id)))
                .where(*window(BuilderPick.created_at))
                .group_by(BuilderPick.model_id)
            )
        ).all()
    )
    joined = dict(
        (
            await db.execute(
                select(SessionModel.model_id, func.count(distinct(SessionModel.session_id)))
                .where(*window(SessionModel.joined_at))
                .group_by(SessionModel.model_id)
            )
        ).all()
    )

    models = (await db.execute(select(Model, Endpoint).join(Endpoint, Endpoint.id == Model.endpoint_id))).all()
    rows = []
    for m, ep in models:
        u, r = usage.get(m.id), replies.get(m.id)
        if u is None and r is None and m.id not in joined:
            continue
        up = int(r.up or 0) if r else 0
        down = int(r.down or 0) if r else 0
        out_tokens = int(u.output_tokens or 0) if u else 0
        total_s = (u.total_latency_ms or 0) / 1000 if u else 0
        cost = float(u.cost or 0) if u else 0.0
        sessions = int(joined.get(m.id, 0))
        rows.append(
            {
                "model_id": m.id,
                "display_name": m.display_name,
                "color": m.color,
                "endpoint": ep.name,
                "endpoint_kind": ep.kind,
                "input_tokens": int(u.input_tokens or 0) if u else 0,
                "output_tokens": out_tokens,
                "cost": round(cost, 6),
                "calls": int(u.calls) if u else 0,
                "replies": int(r.turns or 0) if r else 0,
                "avg_latency_ms": round(float(u.avg_latency_ms or 0)) if u else None,
                "avg_ttft_ms": round(float(u.avg_ttft_ms)) if u and u.avg_ttft_ms is not None else None,
                "tokens_per_s": round(out_tokens / total_s, 1) if total_s else None,
                "upvotes": up,
                "downvotes": down,
                "approval": round(up / (up + down), 3) if up + down else None,
                "builder_picks": int(picks.get(m.id, 0)),
                "sessions": sessions,
                "win_rate": round(picks.get(m.id, 0) / sessions, 3) if sessions else None,
                "cost_per_upvote": round(cost / up, 6) if up else None,
                "estimated": bool(u.estimated) if u else False,
            }
        )
    rows.sort(key=lambda x: x["cost"], reverse=True)

    day = func.date_trunc("day", Usage.created_at)
    daily = [
        {"day": d.date().isoformat(), "model_id": mid, "cost": float(c or 0), "tokens": int(t or 0)}
        for d, mid, c, t in (
            await db.execute(
                select(day, Usage.model_id, func.sum(Usage.cost), func.sum(Usage.input_tokens + Usage.output_tokens))
                .where(*window(Usage.created_at))
                .group_by(day, Usage.model_id)
                .order_by(day)
            )
        ).all()
    ]
    totals = {
        "cost": round(sum(r["cost"] for r in rows), 6),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "upvotes": sum(r["upvotes"] for r in rows),
        "downvotes": sum(r["downvotes"] for r in rows),
    }
    return {"models": rows, "daily": daily, "totals": totals}
