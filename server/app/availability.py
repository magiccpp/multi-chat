"""Which models can be used right now.

At start-up and periodically, every endpoint's key is checked and the vendor's model list fetched.
Endpoints with `auto_models` import the vendor catalog (chat models, with prices when the vendor
reports them). Each model is then marked available or not, with a reason the clients show.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import ipaddress
import logging
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import crypto, pricing
from app import db as dbmod
from app.providers.agent_cli import cursor_models
from app.providers.base import ProviderError
from app.providers.registry import provider_for
from app.tables import Endpoint, Model, Runner, SshConnection, Usage

log = logging.getLogger(__name__)
CHECK_TIMEOUT = 45
PALETTE = ["#2563eb", "#d97706", "#059669", "#db2777", "#7c3aed", "#0891b2", "#dc2626", "#65a30d"]
CLI_NAMES = {"claude_code": "claude", "cursor": "cursor-agent", "copilot": "copilot"}
CLI_LABELS = {"claude_code": "Claude Code", "cursor": "Cursor", "copilot": "GitHub Copilot"}
CLI_LIST_TTL = 1800  # seconds a runner's `cursor-agent --list-models` answer is reused
_lock = asyncio.Lock()
_cli_lists: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}


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


async def cli_model_list(db: AsyncSession, ep: Endpoint) -> list[dict[str, Any]] | None:
    """Cursor's model families, asked from a connected runner that has the CLI (cached for 30 minutes)."""
    from app.runners.hub import RunnerError, RunnerOffline, hub

    key = crypto.decrypt(ep.api_key_enc) or ""
    for r in (await db.execute(select(Runner))).scalars():
        info = (r.clis or {}).get(ep.driver or "")
        if not info or not hub.online(r.id):
            continue
        cache_key = (r.id, info.get("path"), info.get("version"), hashlib.sha256(key.encode()).hexdigest())
        hit = _cli_lists.get(cache_key)
        if hit and time.monotonic() - hit[0] < CLI_LIST_TTL:
            return hit[1]
        try:
            result = await hub.get(r.id).call("agent_cli.models", {"driver": ep.driver, "env": {"CURSOR_API_KEY": key} if key else {}},
                                              timeout=CHECK_TIMEOUT)
        except (RunnerError, RunnerOffline, TimeoutError) as exc:
            log.warning("could not list %s models on runner %s: %s", ep.driver, r.name, exc)
            continue
        found = cursor_models.families(cursor_models.parse_list(result.get("output") or ""))
        if not found:
            log.warning("no models in %s's model list on runner %s (exit %s): %.300r", ep.driver, r.name,
                        result.get("exit_code"), result.get("output"))
            continue
        _cli_lists[cache_key] = (time.monotonic(), found)
        return found
    return None


async def import_cli_models(db: AsyncSession, ep: Endpoint, models: list[Model]) -> None:
    """One model per Cursor family; effort and context window pick the slug when a turn runs."""
    found = await cli_model_list(db, ep)
    if found is None:
        return
    label = CLI_LABELS.get(ep.driver or "", ep.name)
    items = []
    for fam in found:
        cli = {"variants": fam["variants"], "default_context": fam["default_context"]} if fam["variants"] else None
        choices = cursor_models.context_choices(cli)
        items.append({"id": fam["id"], "name": f"{label} · {fam['name']}", "supports_effort": bool(cursor_models.effort_levels(cli)),
                      "supports_images": False, "supports_tools": True, "_cli": cli,
                      **({"context_length": max(choices)} if choices else {})})
    patterns = import_patterns(ep, models)
    models[:] = await import_catalog(db, ep, items, models, patterns or [])
    by_id = {item["id"]: item for item in items}
    slugs = {v["slug"] for fam in found for v in fam["variants"]}  # a slug pinned in config.yaml still works
    for m in models:
        item = by_id.get(m.model_name)
        if item is not None:
            m.cli_variants = item["_cli"]
        elif m.model_name != "default" and m.model_name not in slugs:
            m.available, m.availability_detail = False, f"not offered by {label} on a connected runner"
    ep.status_detail = f"{len(items)} models offered"


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
        # facts about the model, whoever created the row
        if item.get("context_length"):
            m.context_length = int(item["context_length"])
        if "supports_effort" in item:
            m.supports_effort = bool(item["supports_effort"])
        if m.source != "catalog":
            continue  # rows from config.yaml or the API keep their own name and prices
        m.display_name = (item.get("name") or m.display_name)[:100]
        m.input_price = float(item.get("input_price", m.input_price or 0) or 0)
        m.output_price = float(item.get("output_price", m.output_price or 0) or 0)
        m.supports_images = bool(item.get("supports_images", m.supports_images))
        m.supports_tools = bool(item.get("supports_tools", m.supports_tools))
    await db.flush()
    return models


async def fill_missing_prices(db: AsyncSession, ep: Endpoint, models: list[Model]) -> None:
    """Vendors like OpenAI don't report prices: take OpenRouter's list price for the same model, and re-price this
    model's earlier usage that was recorded as $0."""
    prefix = pricing.vendor_prefix(ep.kind, ep.base_url)
    if prefix is None:
        return
    if any(not m.context_length for m in models):
        contexts = await pricing.openrouter_contexts()
        for m in models:
            ctx = None if m.context_length else pricing.lookup(contexts, prefix, m.model_name)
            if ctx:
                m.context_length = ctx
    missing = [m for m in models if not pricing.has_price(m)]
    if not missing:
        return
    prices = await pricing.openrouter_prices()
    for m in missing:
        hit = pricing.lookup(prices, prefix, m.model_name)
        if not hit:
            continue
        m.input_price, m.output_price = hit
        await db.flush()
        await db.execute(
            update(Usage)
            .where(Usage.model_id == m.id, Usage.cost == 0, (Usage.input_tokens + Usage.output_tokens) > 0)
            .values(cost=(Usage.input_tokens * hit[0] + Usage.output_tokens * hit[1]) / 1_000_000)
        )
        log.info("price for %s/%s from OpenRouter: $%s / $%s per 1M", ep.name, m.model_name, hit[0], hit[1])


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
        for m in models:  # the demo models take every setting
            m.context_length, m.supports_effort = m.context_length or 128_000, True
        return
    if ep.kind == "agent_cli":
        exe = CLI_NAMES.get(ep.driver or "", ep.driver or "?")
        if ep.driver in await online_clis(db):
            ep.status, ep.status_detail = "ok", ""
            _set(models, True, "")
            if ep.driver == "cursor":
                await import_cli_models(db, ep, models)
        elif (await db.execute(select(SshConnection.id).limit(1))).first():
            ep.status, ep.status_detail = "ok", f"{exe} not seen on a connected runner; may still work on an SSH host"
            _set(models, True, ep.status_detail)
        else:
            ep.status = "error"
            ep.status_detail = (f"{exe} was not found on any connected runner. Install it on the PC that runs the "
                                f"MultiChat app (or `multichat runner`), not on the server; it is picked up within 2 "
                                f"minutes (or Runner → Detect again).")
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
    offered = {item["id"] for item in catalog}
    patterns = import_patterns(ep, models)
    if not patterns and offered and not any(m.model_name in offered for m in models):
        # none of the models listed in config.yaml exists (any more, e.g. renamed by the vendor): offer what it has
        log.info("endpoint %s: none of the listed models is offered; importing its model list", ep.name)
        patterns = True
    if patterns:
        models = await import_catalog(db, ep, catalog, models, patterns)
    else:
        await import_catalog(db, ep, catalog, models, [m.model_name for m in models])  # facts for listed models
    for m in models:
        if offered and m.model_name not in offered:
            m.available, m.availability_detail = False, f"not offered by {ep.name}"
        else:
            m.available, m.availability_detail = True, ""
    await fill_missing_prices(db, ep, models)
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
