import secrets

from fastapi import Header, HTTPException, Query, WebSocket, status

from app.config import get_settings


def _valid(token: str | None) -> bool:
    expected = get_settings().multichat_api_token
    return bool(token) and bool(expected) and secrets.compare_digest(token, expected)


def _bearer(value: str | None) -> str | None:
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return None


async def require_token(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None, include_in_schema=False),
) -> None:
    if not _valid(_bearer(authorization) or token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API token")


def websocket_authorized(ws: WebSocket) -> bool:
    return _valid(_bearer(ws.headers.get("authorization")) or ws.query_params.get("token"))
