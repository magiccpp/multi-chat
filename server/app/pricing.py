"""Model prices: cost from per-1M-token prices, and list prices from OpenRouter's public catalog for vendors whose
APIs don't report prices (OpenAI, Anthropic, DeepSeek, Google, xAI, Mistral, GitHub Models)."""

from __future__ import annotations

import logging
import re
import time

import httpx

from app.tables import Model

log = logging.getLogger("multichat.pricing")

CATALOG_URL = "https://openrouter.ai/api/v1/models"  # public, no key needed
CACHE_SECONDS = 6 * 3600
_cache: tuple[float, dict[str, tuple[float, float]], dict[str, int]] | None = None

# (substring of the endpoint's base URL or kind, OpenRouter id prefix)
VENDORS = [
    ("api.openai.com", "openai/"),
    ("anthropic", "anthropic/"),
    ("deepseek.com", "deepseek/"),
    ("generativelanguage.googleapis.com", "google/"),
    ("api.x.ai", "x-ai/"),
    ("mistral.ai", "mistralai/"),
    ("models.github.ai", ""),  # ids are already "openai/gpt-4.1"
    ("models.inference.ai.azure.com", "openai/"),
]


def cost_for(model: Model, input_tokens: int, output_tokens: int) -> float:
    """USD cost from the model's per-1M-token prices (configured on the backend)."""
    return (input_tokens * (model.input_price or 0) + output_tokens * (model.output_price or 0)) / 1_000_000


def has_price(model: Model) -> bool:
    return bool((model.input_price or 0) > 0 or (model.output_price or 0) > 0)


def _norm(model_id: str) -> str:
    """'anthropic/claude-sonnet-4-5-20250929' and 'anthropic/claude-sonnet-4.5' compare equal."""
    s = model_id.lower()
    s = re.sub(r"[-_@](\d{8}|\d{4}-\d{2}-\d{2}|latest)$", "", s)
    return re.sub(r"[^a-z0-9/]", "", s)


def vendor_prefix(kind: str, base_url: str | None) -> str | None:
    where = f"{kind} {base_url or ''}".lower()
    if "openrouter.ai" in where or kind in ("mock", "agent_cli"):
        return None
    for needle, prefix in VENDORS:
        if needle in where:
            return prefix
    return None


async def _catalog() -> tuple[dict[str, tuple[float, float]], dict[str, int]]:
    """OpenRouter's public model list: normalised id -> (input, output) USD per 1M tokens, and -> context length.
    Cached; empty if unreachable."""
    global _cache
    if _cache and time.monotonic() - _cache[0] < CACHE_SECONDS:
        return _cache[1], _cache[2]
    prices: dict[str, tuple[float, float]] = {}
    contexts: dict[str, int] = {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            data = (await client.get(CATALOG_URL)).raise_for_status().json().get("data", [])
        for item in data:
            if isinstance(item.get("context_length"), int):
                contexts.setdefault(_norm(item["id"]), item["context_length"])
            p = item.get("pricing") or {}
            try:
                inp, out = float(p.get("prompt") or 0) * 1_000_000, float(p.get("completion") or 0) * 1_000_000
            except (TypeError, ValueError):
                continue
            if inp > 0 or out > 0:
                prices.setdefault(_norm(item["id"]), (round(inp, 6), round(out, 6)))
    except (httpx.HTTPError, ValueError) as exc:
        log.info("OpenRouter price list unavailable: %s", exc)
    if prices:
        _cache = (time.monotonic(), prices, contexts)
    return prices, contexts


async def openrouter_prices() -> dict[str, tuple[float, float]]:
    return (await _catalog())[0]


async def openrouter_contexts() -> dict[str, int]:
    return (await _catalog())[1]


def lookup(prices: dict, prefix: str, model_name: str):  # noqa: ANN201 - prices or context lengths
    for candidate in (prefix + model_name, model_name, prefix + model_name.split("/")[-1]):
        hit = prices.get(_norm(candidate))
        if hit:
            return hit
    return None
