"""web_fetch: download a page and return readable text, with an SSRF guard."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura

MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5


class FetchError(Exception):
    pass


async def _check_host(url: str, allow_private: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError("only http(s) URLs can be fetched")
    if allow_private:
        return
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(f"cannot resolve {parsed.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise FetchError(f"{parsed.hostname} resolves to a private address; blocked by the server's web policy")


def html_to_text(html: str, url: str) -> str:
    text = trafilatura.extract(html, url=url, output_format="markdown", include_links=True, include_tables=True)
    if text:
        return text
    # Fallback: crude tag strip.
    import re

    text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


async def web_fetch(url: str, *, allow_private: bool = False, max_chars: int = 30_000) -> str:
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, headers={"User-Agent": "MultiChat/0.1"}) as client:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            await _check_host(current, allow_private)
            async with client.stream("GET", current) as resp:
                if resp.is_redirect and "location" in resp.headers:
                    current = urljoin(current, resp.headers["location"])
                    continue
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        break
                ctype = resp.headers.get("content-type", "")
                text = bytes(body).decode(resp.encoding or "utf-8", errors="replace")
                header = f"URL: {current}\nStatus: {resp.status_code}\n\n"
                if "html" in ctype:
                    text = html_to_text(text, current)
                elif not (ctype.startswith("text/") or "json" in ctype or "xml" in ctype or not ctype):
                    return header + f"(binary content: {ctype}, {len(body)} bytes)"
                if len(text) > max_chars:
                    text = text[:max_chars] + f"\n… (truncated, {len(text)} chars total)"
                return header + text
        raise FetchError("too many redirects")
