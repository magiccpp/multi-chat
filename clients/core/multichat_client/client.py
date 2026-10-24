"""Async HTTP client for the MultiChat server, shared by the desktop app and the TUI."""

from __future__ import annotations

import base64
import json
import mimetypes
from collections.abc import AsyncIterator
from typing import Any

import httpx


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


async def iter_sse(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    data: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line == "" and data:
            yield json.loads("\n".join(data))
            data = []
    if data:
        yield json.loads("\n".join(data))


class MultiChatClient:
    def __init__(self, url: str, token: str, *, timeout: float = 30) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.http = httpx.AsyncClient(
            base_url=self.url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(timeout, read=None),
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def __aenter__(self) -> MultiChatClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self.http.request(method, path, **kw)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise ApiError(resp.status_code, str(detail))
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # --- server / config ---
    async def health(self) -> dict:
        return await self._req("GET", "/api/health")

    async def settings(self) -> dict:
        return await self._req("GET", "/api/admin/settings")

    async def reload_config(self) -> dict:
        return await self._req("POST", "/api/admin/reload-config")

    # --- endpoints & models ---
    async def presets(self) -> list[dict]:
        return await self._req("GET", "/api/endpoints/presets")

    async def endpoints(self) -> list[dict]:
        return await self._req("GET", "/api/endpoints")

    async def create_endpoint(self, **body: Any) -> dict:
        return await self._req("POST", "/api/endpoints", json=body)

    async def update_endpoint(self, endpoint_id: int, **body: Any) -> dict:
        return await self._req("PATCH", f"/api/endpoints/{endpoint_id}", json=body)

    async def delete_endpoint(self, endpoint_id: int) -> None:
        await self._req("DELETE", f"/api/endpoints/{endpoint_id}")

    async def test_endpoint(self, endpoint_id: int) -> dict:
        return await self._req("POST", f"/api/endpoints/{endpoint_id}/test")

    async def check_endpoints(self, endpoint_id: int | None = None) -> Any:
        """Re-check keys and vendor model lists now (one endpoint, or all → list of statuses)."""
        path = f"/api/endpoints/{endpoint_id}/check" if endpoint_id is not None else "/api/endpoints/check"
        return await self._req("POST", path, timeout=120)

    async def available_models(self, endpoint_id: int) -> list[dict]:
        return await self._req("GET", f"/api/endpoints/{endpoint_id}/available-models")

    async def import_models(self, endpoint_id: int, ids: list[str]) -> list[dict]:
        return await self._req("POST", f"/api/endpoints/{endpoint_id}/import-models", json=ids)

    async def models(self, enabled_only: bool = False) -> list[dict]:
        return await self._req("GET", "/api/models", params={"enabled_only": enabled_only})

    async def create_model(self, **body: Any) -> dict:
        return await self._req("POST", "/api/models", json=body)

    async def update_model(self, model_id: int, **body: Any) -> dict:
        return await self._req("PATCH", f"/api/models/{model_id}", json=body)

    async def delete_model(self, model_id: int) -> None:
        await self._req("DELETE", f"/api/models/{model_id}")

    # --- ssh & runners ---
    async def ssh_connections(self) -> list[dict]:
        return await self._req("GET", "/api/ssh")

    async def create_ssh(self, **body: Any) -> dict:
        return await self._req("POST", "/api/ssh", json=body)

    async def scan_host_key(self, conn_id: int) -> dict:
        return await self._req("POST", f"/api/ssh/{conn_id}/scan-host-key")

    async def trust_host_key(self, conn_id: int, fingerprint: str) -> dict:
        return await self._req("POST", f"/api/ssh/{conn_id}/trust-host-key", json={"fingerprint": fingerprint})

    async def test_ssh(self, conn_id: int) -> dict:
        return await self._req("POST", f"/api/ssh/{conn_id}/test")

    async def browse_ssh(self, conn_id: int, path: str = "") -> dict:
        return await self._req("GET", f"/api/ssh/{conn_id}/browse", params={"path": path})

    async def runners(self) -> list[dict]:
        return await self._req("GET", "/api/runners")

    async def browse_runner(self, runner_id: int, path: str = "") -> dict:
        return await self._req("GET", f"/api/runners/{runner_id}/browse", params={"path": path})

    # --- sessions ---
    async def sessions(self, limit: int = 50) -> list[dict]:
        return await self._req("GET", "/api/sessions", params={"limit": limit})

    async def create_session(
        self,
        title: str = "New session",
        model_ids: list[int] | None = None,
        context_mode: str = "isolated",
        workspace: dict | None = None,
        reuse_empty: bool = False,
        model_settings: dict[int, dict] | None = None,
    ) -> dict:
        """`reuse_empty` returns the latest session instead when it has no messages yet. `model_settings`:
        {model_id: {"context_window": tokens, "effort": "low"|...|"max"}}."""
        body = {"title": title, "model_ids": model_ids or [], "context_mode": context_mode, "reuse_empty": reuse_empty}
        if model_settings:
            body["model_settings"] = {str(k): v for k, v in model_settings.items()}
        if workspace:
            body["workspace"] = workspace
        return await self._req("POST", "/api/sessions", json=body)

    async def session(self, session_id: int) -> dict:
        return await self._req("GET", f"/api/sessions/{session_id}")

    async def update_session(self, session_id: int, **body: Any) -> dict:
        return await self._req("PATCH", f"/api/sessions/{session_id}", json=body)

    async def delete_session(self, session_id: int) -> None:
        await self._req("DELETE", f"/api/sessions/{session_id}")

    async def set_session_models(self, session_id: int, model_ids: list[int]) -> dict:
        return await self._req("PUT", f"/api/sessions/{session_id}/models", json={"model_ids": model_ids})

    async def update_model_settings(self, session_id: int, model_id: int, **fields: Any) -> dict:
        """effort (any time; None = default) and context_window (only before the session's first message)."""
        return await self._req("PATCH", f"/api/sessions/{session_id}/models/{model_id}", json=fields)

    async def set_workspace(self, session_id: int, kind: str, path: str | None = None, **ids: Any) -> dict:
        return await self._req("PATCH", f"/api/sessions/{session_id}/workspace", json={"kind": kind, "path": path, **ids})

    async def messages(self, session_id: int) -> list[dict]:
        return await self._req("GET", f"/api/sessions/{session_id}/messages")

    async def usage(self, session_id: int) -> list[dict]:
        return await self._req("GET", f"/api/sessions/{session_id}/usage")

    async def upload_attachment(self, session_id: int, filename: str, data: bytes, mime: str | None = None) -> dict:
        """Upload a file or image; pass the returned id to `chat(attachment_ids=...)`."""
        body = {
            "filename": filename,
            "mime": mime or mimetypes.guess_type(filename)[0],
            "data_base64": base64.b64encode(data).decode(),
        }
        return await self._req("POST", f"/api/sessions/{session_id}/attachments", json=body, timeout=120)

    async def attachment(self, attachment_id: int) -> bytes:
        resp = await self.http.get(f"/api/attachments/{attachment_id}")
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, resp.text)
        return resp.content

    async def chat(
        self,
        session_id: int,
        content: str,
        recipients: list[int] | None = None,
        attachment_ids: list[int] | None = None,
    ) -> AsyncIterator[dict]:
        """Start a turn; yields its events until `turn_end`."""
        body = {"content": content, "recipients": recipients, "attachment_ids": attachment_ids or []}
        async with self.http.stream("POST", f"/api/sessions/{session_id}/chat", json=body) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                try:
                    detail = resp.json().get("detail", resp.text)
                except ValueError:
                    detail = resp.text
                raise ApiError(resp.status_code, str(detail))
            async for event in iter_sse(resp):
                yield event

    async def events(self, session_id: int) -> AsyncIterator[dict]:
        """Follow a session forever (replays a running turn first)."""
        async with self.http.stream("GET", f"/api/sessions/{session_id}/events") as resp:
            if resp.status_code >= 400:
                raise ApiError(resp.status_code, (await resp.aread()).decode())
            async for event in iter_sse(resp):
                yield event

    async def stop(self, session_id: int) -> dict:
        return await self._req("POST", f"/api/sessions/{session_id}/stop")

    async def approve(self, session_id: int, call_id: str, approve: bool, note: str | None = None) -> dict:
        return await self._req(
            "POST", f"/api/sessions/{session_id}/approvals/{call_id}", json={"approve": approve, "note": note}
        )

    async def pick_builder(self, session_id: int, model_id: int, switch_to_build: bool = True) -> dict:
        return await self._req(
            "POST", f"/api/sessions/{session_id}/pick-builder", json={"model_id": model_id, "switch_to_build": switch_to_build}
        )

    async def changes(self, session_id: int, turn_id: str | None = None) -> list[dict]:
        params = {"turn_id": turn_id} if turn_id else {}
        return await self._req("GET", f"/api/sessions/{session_id}/changes", params=params)

    async def revert(self, session_id: int, turn_id: str | None = None) -> dict:
        return await self._req("POST", f"/api/sessions/{session_id}/revert", json={"turn_id": turn_id})

    async def tree(self, session_id: int, path: str = ".") -> dict:
        return await self._req("GET", f"/api/sessions/{session_id}/workspace/tree", params={"path": path})

    async def file(self, session_id: int, path: str) -> dict:
        return await self._req("GET", f"/api/sessions/{session_id}/workspace/file", params={"path": path})

    async def context_usage(self, session_id: int) -> list[dict]:
        """Per model: estimated tokens used, context window, percent, compactions."""
        return await self._req("GET", f"/api/sessions/{session_id}/context")

    async def compact(self, session_id: int, model_ids: list[int] | None = None) -> list[dict]:
        """Summarise each model's older history now (like Claude Code's /compact)."""
        return await self._req("POST", f"/api/sessions/{session_id}/compact", json={"model_ids": model_ids}, timeout=600)

    # --- memory (like CLAUDE.md) ---
    async def memory(self) -> dict:
        return await self._req("GET", "/api/memory")

    async def set_memory(self, text: str) -> dict:
        return await self._req("PUT", "/api/memory", json={"text": text})

    async def remember(self, text: str) -> dict:
        """Add one line to the global memory ("# ..." in the apps)."""
        return await self._req("POST", "/api/memory/append", json={"text": text})

    # --- votes & stats ---
    async def vote(self, ref: int | str, value: int, session_id: int | None = None) -> dict:
        """`ref` is a message id or "#n" (per-session index, needs session_id)."""
        ref_s = str(ref).replace("#", "%23")
        params = {"session_id": session_id} if session_id is not None else {}
        return await self._req("PUT", f"/api/messages/{ref_s}/vote", params=params, json={"value": value})

    async def stats(self, date_from: str | None = None, date_to: str | None = None) -> dict:
        params = {k: v for k, v in {"date_from": date_from, "date_to": date_to}.items() if v}
        return await self._req("GET", "/api/stats", params=params)
