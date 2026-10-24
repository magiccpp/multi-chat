"""Sync the declarative backend configuration (config.yaml) into the database.

Example (see config.example.yaml):

    endpoints:
      - name: openai
        kind: openai_compatible
        base_url: https://api.openai.com/v1
        api_key: ${OPENAI_API_KEY}
        models:
          - {name: gpt-5, display_name: GPT-5, color: "#10a37f", input_price: 1.25, output_price: 10}
    ssh_connections: [...]
    web_search: {provider: brave, api_key: ${BRAVE_API_KEY}}
    web: {allow_private: false}
    defaults: {approve_edits: auto, approve_commands: ask, max_steps: 40, command_timeout: 600}
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crypto
from app.tables import Endpoint, Model, Setting, SshConnection

log = logging.getLogger(__name__)
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return expand_env(data)


def _stored(value: str | None) -> str | None:
    """Decrypt a stored secret; one encrypted with a lost/changed key counts as different (config re-encrypts it)."""
    try:
        return crypto.decrypt(value)
    except RuntimeError:
        log.warning("a stored secret could not be decrypted (MULTICHAT_SECRET_KEY changed?); re-encrypting from config")
        return None


async def set_setting(db: AsyncSession, key: str, value: dict[str, Any]) -> None:
    row = await db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


async def get_setting(db: AsyncSession, key: str) -> dict[str, Any]:
    row = await db.get(Setting, key)
    return dict(row.value) if row else {}


OPENROUTER_URL = "https://openrouter.ai/api/v1"


def is_openrouter(base_url: str | None) -> bool:
    return "openrouter.ai" in (base_url or "")


def with_builtin_endpoints(endpoints: list[dict[str, Any]], env: dict[str, str], skip_openrouter: bool = False) -> list[dict[str, Any]]:
    """OpenRouter is always offered when OPENROUTER_API_KEY is set, even if config.yaml doesn't mention it (or was
    copied from an older example). A config entry for OpenRouter without a key gets the one from the environment."""
    key = (env.get("OPENROUTER_API_KEY") or "").strip()
    out = []
    for ep in endpoints:
        if key and is_openrouter(ep.get("base_url")) and not ep.get("api_key"):
            ep = {**ep, "api_key": key}
        out.append(ep)
    if key and not skip_openrouter and not any(is_openrouter(ep.get("base_url")) for ep in out):
        names = {ep.get("name") for ep in out}
        name = "openrouter" if "openrouter" not in names else "openrouter-env"
        out.append({"name": name, "kind": "openai_compatible", "base_url": OPENROUTER_URL, "api_key": key,
                    "auto_models": True, "models": []})
    return out


async def sync_config(db: AsyncSession, config: dict[str, Any]) -> dict[str, int]:
    counts = {"endpoints": 0, "models": 0, "ssh_connections": 0}
    seen_endpoints: set[str] = set()
    # an OpenRouter endpoint added from a client already covers the built-in one
    added_in_app = (await db.execute(
        select(Endpoint.id).where(Endpoint.source == "api", Endpoint.enabled.is_(True), Endpoint.base_url.like("%openrouter.ai%"))
    )).first() is not None
    endpoints = with_builtin_endpoints(list(config.get("endpoints") or []), dict(os.environ), skip_openrouter=added_in_app)

    for ep in endpoints:
        name = ep["name"]
        seen_endpoints.add(name)
        row = (await db.execute(select(Endpoint).where(Endpoint.name == name))).scalar_one_or_none()
        if row is None:
            row = Endpoint(name=name)
            db.add(row)
        row.kind = ep.get("kind", "openai_compatible")
        row.driver = ep.get("driver")
        row.base_url = ep.get("base_url")
        key = ep.get("api_key") or None
        if key is None or _stored(row.api_key_enc) != key:
            row.api_key_enc = crypto.encrypt(key)
        row.source = "config"
        row.enabled = ep.get("enabled", True)
        # OpenRouter imports its whole catalog unless told otherwise; see also availability.import_patterns()
        row.auto_models = ep.get("auto_models", is_openrouter(row.base_url))
        await db.flush()
        counts["endpoints"] += 1

        for m in ep.get("models") or []:
            mname = m["name"] if isinstance(m, dict) else str(m)
            spec = m if isinstance(m, dict) else {}
            model = (
                await db.execute(select(Model).where(Model.endpoint_id == row.id, Model.model_name == mname))
            ).scalar_one_or_none()
            if model is None:
                model = Model(endpoint_id=row.id, model_name=mname)
                db.add(model)
            model.display_name = spec.get("display_name") or mname
            model.color = spec.get("color") or model.color or "#6b7280"
            model.input_price = float(spec.get("input_price", model.input_price or 0) or 0)
            model.output_price = float(spec.get("output_price", model.output_price or 0) or 0)
            model.supports_tools = bool(spec.get("supports_tools", True))
            model.enabled = bool(spec.get("enabled", True))
            model.source = "config"
            counts["models"] += 1

    # Endpoints removed from the file are disabled, not deleted, so history and stats survive.
    for row in (await db.execute(select(Endpoint).where(Endpoint.source == "config"))).scalars():
        if row.name not in seen_endpoints:
            row.enabled = False

    for sc in config.get("ssh_connections") or []:
        row = (await db.execute(select(SshConnection).where(SshConnection.name == sc["name"]))).scalar_one_or_none()
        if row is None:
            row = SshConnection(name=sc["name"])
            db.add(row)
        row.host = sc["host"]
        row.port = int(sc.get("port", 22))
        row.username = sc["username"]
        row.auth = sc.get("auth", "key")
        row.shell = sc.get("shell", "bash")
        key_text = sc.get("private_key")
        if not key_text and sc.get("private_key_file"):
            key_text = Path(sc["private_key_file"]).read_text()
        row.private_key_enc = crypto.encrypt(key_text)
        row.passphrase_enc = crypto.encrypt(sc.get("passphrase"))
        row.password_enc = crypto.encrypt(sc.get("password"))
        if sc.get("host_key"):
            row.host_key = sc["host_key"]
        row.source = "config"
        counts["ssh_connections"] += 1

    search = dict(config.get("web_search") or {})
    if search.get("api_key"):
        search["api_key_enc"] = crypto.encrypt(search.pop("api_key"))
    else:
        search.pop("api_key", None)
    await set_setting(db, "web_search", search)
    await set_setting(db, "web", dict(config.get("web") or {}))
    await set_setting(db, "defaults", dict(config.get("defaults") or {}))
    await db.commit()
    return counts
