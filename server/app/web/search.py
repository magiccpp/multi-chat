"""web_search via a provider configured on the backend: Brave, Tavily or SearXNG."""

from __future__ import annotations

from typing import Any

import httpx


class SearchError(Exception):
    pass


async def web_search(config: dict[str, Any], api_key: str | None, query: str, count: int = 8) -> str:
    provider = config.get("provider")
    async with httpx.AsyncClient(timeout=20) as client:
        if provider == "brave":
            resp = await client.get(
                config.get("base_url") or "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": count},
                headers={"X-Subscription-Token": api_key or "", "Accept": "application/json"},
            )
            resp.raise_for_status()
            items = [
                (r.get("title", ""), r.get("url", ""), r.get("description", ""))
                for r in (resp.json().get("web") or {}).get("results", [])
            ]
        elif provider == "tavily":
            resp = await client.post(
                config.get("base_url") or "https://api.tavily.com/search",
                json={"query": query, "max_results": count},
                headers={"Authorization": f"Bearer {api_key or ''}"},
            )
            resp.raise_for_status()
            items = [(r.get("title", ""), r.get("url", ""), r.get("content", "")) for r in resp.json().get("results", [])]
        elif provider == "searxng":
            base = (config.get("base_url") or "").rstrip("/")
            if not base:
                raise SearchError("searxng needs base_url")
            resp = await client.get(f"{base}/search", params={"q": query, "format": "json"})
            resp.raise_for_status()
            items = [
                (r.get("title", ""), r.get("url", ""), r.get("content", "")) for r in resp.json().get("results", [])[:count]
            ]
        else:
            raise SearchError("web search is not configured on the server")
    if not items:
        return f"No results for {query!r}."
    return "\n\n".join(f"{i}. {title}\n   {url}\n   {snippet.strip()}" for i, (title, url, snippet) in enumerate(items, 1))
