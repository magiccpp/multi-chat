"""Cursor's model list (`cursor-agent --list-models`) grouped into families.

In Cursor, effort / thinking and the long context window are separate model slugs (`gpt-5-high`,
`claude-opus-4-8-thinking-high`, `…-1m`). MultiChat shows one model per family and picks the slug from the
session's effort and context window, so they are chosen like any other model's settings.
"""

from __future__ import annotations

import re
from typing import Any

EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_CONTEXT = 200_000  # Cursor's standard window; long-context variants carry their size in the slug

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
LINE = re.compile(r"^\s*(?:[-*•>]\s+)?([a-z0-9][a-z0-9._-]*)(?:\s+(?:[-–—:|]\s*)?(.*?))?\s*$")
MARKERS = re.compile(r"\((?:current|default|selected)\)|\[(?:current|default)\]|[✓✔*]", re.I)
CONTEXT = re.compile(r"^(\d+(?:\.\d+)?)([km])$")
PROSE = {"use", "run", "to", "the", "available", "models", "model", "loading", "no", "you", "pass", "try", "tip",
         "note", "or", "and", "with", "for", "select", "current"}


def parse_list(text: str) -> list[dict[str, str]]:
    """[{id, name}] from the CLI's output, whatever its exact layout (`slug - Name`, `slug  Name`, `* slug (current)`)."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = ANSI.sub("", raw).rstrip()
        m = LINE.match(line)
        if not m:
            continue
        slug, rest = m.group(1).rstrip(".-_"), MARKERS.sub(" ", m.group(2) or "").strip()
        if not slug or slug in PROSE or slug in seen or rest.startswith(("-", "`", "<")) or "<" in rest:
            continue
        seen.add(slug)
        out.append({"id": slug, "name": " ".join(rest.split())})
    return out


def split(slug: str) -> dict[str, Any]:
    """Trailing attribute tokens: at most one effort level, `thinking`, `fast`, a context size like `1m`."""
    parts = slug.split("-")
    v: dict[str, Any] = {"slug": slug, "effort": None, "thinking": False, "fast": False, "context": None}
    while len(parts) > 1:
        tok = parts[-1]
        ctx = CONTEXT.match(tok)
        if tok in EFFORTS and v["effort"] is None:
            v["effort"] = tok
        elif tok == "thinking" and not v["thinking"]:
            v["thinking"] = True
        elif tok == "fast" and not v["fast"]:
            v["fast"] = True
        elif ctx and v["context"] is None:
            v["context"] = int(float(ctx.group(1)) * (1_000 if ctx.group(2) == "k" else 1_000_000))
        else:
            break
        parts.pop()
    v["base"] = "-".join(parts)
    return v


def _plain(v: dict[str, Any]) -> bool:
    return v["effort"] is None and not v["thinking"] and not v["fast"] and v["context"] is None


def families(models: list[dict[str, str]]) -> list[dict[str, Any]]:
    """[{id, name, variants, default_context}] in the CLI's order. A slug whose base has no siblings stays a model of
    its own (so `gpt-5.1-codex-max` alone isn't read as "gpt-5.1-codex at max effort")."""
    names = {m["id"]: m.get("name") or "" for m in models}
    groups: dict[str, list[dict[str, Any]]] = {}
    for m in models:
        v = split(m["id"])
        groups.setdefault(v["base"], []).append(v)
    out = []
    for base, variants in groups.items():
        if len(variants) == 1 and base not in names:
            v = variants[0]
            out.append({"id": v["slug"], "name": names[v["slug"]] or v["slug"], "variants": [], "default_context": None})
            continue
        plain = next((v for v in variants if _plain(v)), None)
        name = names.get(base) or (names[plain["slug"]] if plain else "") or base
        name = re.sub(r"\s*\((?:thinking|high|low|medium|max|fast)[^)]*\)\s*$", "", name, flags=re.I)
        for v in variants:
            v.pop("base", None)
        out.append({"id": base, "name": name, "variants": variants, "default_context": DEFAULT_CONTEXT})
    return out


def _plain_is_medium(variants: list[dict[str, Any]]) -> bool:
    """Next to explicit levels (`gpt-5-low`, `gpt-5-high`), the plain slug is Cursor's default: medium."""
    levels = {v.get("effort") for v in variants}
    return bool(levels - {None}) and "medium" not in levels and any(
        not v.get("effort") and not v.get("thinking") for v in variants)


def effort_levels(cli: dict[str, Any] | None) -> list[str] | None:
    """Effort choices a family offers (None: no choice). A thinking variant without a level counts as "high"."""
    variants = (cli or {}).get("variants") or []
    levels = {v["effort"] for v in variants if v.get("effort")}
    if any(v.get("thinking") and not v.get("effort") for v in variants):
        levels.add("high")
    if _plain_is_medium(variants):
        levels.add("medium")
    return [e for e in EFFORTS if e in levels] or None


def context_choices(cli: dict[str, Any] | None) -> list[int] | None:
    """Context windows a family offers (None: no choice)."""
    cli = cli or {}
    variants = cli.get("variants") or []
    sizes = {v["context"] or cli.get("default_context") or DEFAULT_CONTEXT for v in variants}
    return sorted(sizes) if len(sizes) > 1 else None


def resolve(model_name: str, cli: dict[str, Any] | None, effort: str | None, context_window: int | None) -> str:
    """The slug to run for this family, effort and context window: exact match, else the nearest lower effort, else
    the closest variant; fast variants only when nothing else fits."""
    variants = (cli or {}).get("variants") or []
    if not variants:
        return model_name
    default_ctx = (cli or {}).get("default_context") or DEFAULT_CONTEXT
    pool = [v for v in variants if not v["fast"]] or variants
    want_ctx = None if context_window in (None, default_ctx) else context_window
    pool = [v for v in pool if v["context"] == want_ctx] or [v for v in pool if v["context"] is None] or pool
    if effort is None:
        plain = [v for v in pool if not v["effort"] and not v["thinking"]]
        return (plain or sorted(pool, key=lambda v: EFFORTS.index(v["effort"]) if v["effort"] else -1))[0]["slug"]
    rank = EFFORTS.index(effort) if effort in EFFORTS else 2
    plain_level = 1 if _plain_is_medium(variants) else -1

    def score(v: dict[str, Any]) -> tuple:
        level = EFFORTS.index(v["effort"]) if v["effort"] else (2 if v["thinking"] else plain_level)
        lower = level <= rank
        return (level != rank, not lower, abs(rank - level), not v["thinking"])

    return min(pool, key=score)["slug"]
