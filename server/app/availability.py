"""Which models can be used right now.

At start-up and periodically, every endpoint's key is checked and the vendor's model list fetched.
Endpoints with `auto_models` import the vendor catalog (chat models, with prices when the vendor
reports them). Each model is then marked available or not, with a reason the clients show.
"""

from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crypto
from app import db as dbmod
from app.providers.base import ProviderError
from app.providers.registry import provider_for
from app.tables import Endpoint, Model, Runner, SshConnection

log = logging.getLogger(__name__)
CHECK_TIMEOUT = 45
PALETTE = ["#2563eb", "#d97706", "#059669", "#db2777", "#7c3aed", "#0891b2", "#dc2626", "#65a30d"]
CLI_NAMES = {"claude_code": "claude", "cursor": "cursor-agent", "copilot": "copilot"}
_lock = asyncio.Lock()


def needs_key(ep: Endpoint) -> bool:
    if ep.kind == "anthropic":
        return True
    if ep.kind != "openai_compatible":
        return False
    host = urlparse(ep.base_url or "").hostname or ""
    if host in ("localhost", "host.docker.internal") or host.endswith(".local"):
        return False
    try:
        return not ipaddress.ip_address(host).is_private
    except ValueError:
        return True


def wanted(patterns: Any, model_id: str) -> bool:
    if patterns is True:
        return True
    if isinstance(patterns, list):
        return any(fnmatch.fnmatch(model_id, str(p)) for p in patterns)
    return False


def import_patterns(ep: Endpoint, models: list[Model]) -> Any:
    """Which vendor models to import: `auto_models` when set; an endpoint without any configured models imports all
    of them, since there would otherwise be nothing to choose from."""
    if ep.auto_models:
        return ep.auto_models
    return not any(m.source != "catalog" for m in models)


def _set(models: list[Model], available: bool, detail: str) -> None:
    for m in models:
        m.available = available
        m.availability_detail = detail


async def online_clis(db: AsyncSession) -> set[str]:
    from app.runners.hub import hub

    drivers: set[str] = set()
    for r in (await db.execute(select(Runner))).scalars():
        if hub.online(r.id):
            drivers.update((r.clis or {}).keys())
    return drivers


async def import_catalog(db: AsyncSession, ep: Endpoint, catalog: list[dict[str, Any]], models: list[Model], patterns: Any) -> list[Model]:
    by_name = {m.model_name: m for m in models}
    taken = len((await db.execute(select(Model.id))).all())
    for item in catalog:
        if not item.get("chat", True) or not wanted(patterns, item["id"]):
            continue
        m = by_name.get(item["id"])
        if m is None:
            m = Model(
                endpoint_id=ep.id,
                model_name=item["id"],
                display_name=(item.get("name") or item["id"].split("/")[-1])[:100],
                color=PALETTE[taken % len(PALETTE)],
                source="catalog",
                enabled=True,
            )
            taken += 1
            db.add(m)
            models.append(m)
            by_name[m.model_name] = m
        if m.source != "catalog":
            continue  # rows from config.yaml or the API keep their own name and prices
        m.display_name = (item.get("name") or m.display_name)[:100]
        m.input_price = float(item.get("input_price", m.input_price or 0) or 0)
        m.output_price = float(item.get("output_price", m.output_price or 0) or 0)
        m.supports_images = bool(item.get("supports_images", m.supports_images))
        m.supports_tools = bool(item.get("supports_tools", m.supports_tools))
    await db.flush()
    return models


async def check_endpoint(db: AsyncSession, ep: Endpoint) -> None:
    models = list((await db.execute(select(Model).where(Model.endpoint_id == ep.id))).scalars())
    ep.checked_at = datetime.now(UTC)
    if not ep.enabled:
        ep.status, ep.status_detail = "error", "endpoint disabled"
        _set(models, False, "endpoint disabled")
        return
    if ep.kind == "mock":
        ep.status, ep.status_detail = "ok", ""
        _set(models, True, "")
        return
    if ep.kind == "agent_cli":
        exe = CLI_NAMES.get(ep.driver or "", ep.driver or "?")
        if ep.driver in await online_clis(db):
            ep.status, ep.status_detail = "ok", ""
            _set(models, True, "")
        elif (await db.execute(select(SshConnection.id).limit(1))).first():
            ep.status, ep.status_detail = "ok", f"{exe} not seen on a connected runner; may still work on an SSH host"
            _set(models, True, ep.status_detail)
        else:
            ep.status, ep.status_detail = "error", f"no connected runner has {exe} installed"
            _set(models, False, ep.status_detail)
        return
    if needs_key(ep) and not crypto.decrypt(ep.api_key_enc):
        ep.status, ep.status_detail = "no_key", "no API key configured"
        _set(models, False, "no API key configured for " + ep.name)
        return
    provider = provider_for(ep)
    try:
        await asyncio.wait_for(provider.check_key(), CHECK_TIMEOUT)
        catalog = await asyncio.wait_for(provider.list_models(), CHECK_TIMEOUT)
    except (ProviderError, TimeoutError, OSError) as exc:
        detail = str(exc) or type(exc).__name__
        ep.status, ep.status_detail = "error", detail[:500]
        _set(models, False, f"{ep.name}: {detail[:200]}")
        return
    patterns = import_patterns(ep, models)
    if patterns:
        models = await import_catalog(db, ep, catalog, models, patterns)
    offered = {item["id"] for item in catalog}
    for m in models:
        if offered and m.model_name not in offered:
            m.available, m.availability_detail = False, f"not offered by {ep.name}"
        else:
            m.available, m.availability_detail = True, ""
    ep.status, ep.status_detail = "ok", f"{len(offered)} models offered" if offered else ""


async def check_all(endpoint_id: int | None = None) -> list[dict[str, Any]]:
    """Check one or all endpoints; returns their status."""
    async with _lock, dbmod.sessionmaker()() as db:
        q = select(Endpoint).order_by(Endpoint.id)
        if endpoint_id is not None:
            q = q.where(Endpoint.id == endpoint_id)
        endpoints = list((await db.execute(q)).scalars())
        for ep in endpoints:
            try:
                await check_endpoint(db, ep)
            except Exception as exc:  # noqa: BLE001 - one bad endpoint must not stop the rest
                log.exception("checking endpoint %s failed", ep.name)
                ep.status, ep.status_detail = "error", f"{type(exc).__name__}: {exc}"[:500]
            await db.commit()
        result = [{"id": ep.id, "name": ep.name, "status": ep.status, "detail": ep.status_detail} for ep in endpoints]
    for r in result:
        log.info("endpoint %s: %s %s", r["name"], r["status"], r["detail"])
    return result


async def refresh_agent_clis() -> None:
    """Cheap re-check of agent-CLI endpoints when a runner connects or disconnects."""
    async with _lock, dbmod.sessionmaker()() as db:
        for ep in (await db.execute(select(Endpoint).where(Endpoint.kind == "agent_cli"))).scalars():
            await check_endpoint(db, ep)
        await db.commit()


async def periodic(interval: float) -> None:
    while True:
        try:
            await check_all()
        except Exception:  # noqa: BLE001
            log.exception("endpoint check failed")
        await asyncio.sleep(interval)
