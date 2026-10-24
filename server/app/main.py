import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI

from app import availability
from app import db as dbmod
from app.api import attachments, endpoints, memory, models, runners, sessions, ssh_connections, stats, votes, workspace
from app.auth import require_token
from app.config import get_settings
from app.config_sync import get_setting, load_config, sync_config

log = logging.getLogger("multichat")


async def reload_config() -> dict:
    settings = get_settings()
    config = load_config(settings.config_file)
    async with dbmod.sessionmaker()() as db:
        counts = await sync_config(db, config)
    log.info("config synced from %s: %s", settings.config_file, counts)
    return counts


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    if not get_settings().multichat_api_token:
        raise RuntimeError("MULTICHAT_API_TOKEN must be set")
    await reload_config()
    async with dbmod.sessionmaker()() as db:
        minutes = float((await get_setting(db, "defaults")).get("availability_interval_minutes", 30))
    checker = asyncio.create_task(availability.periodic(minutes * 60))
    yield
    checker.cancel()
    await dbmod.dispose()


admin = APIRouter(prefix="/api/admin", tags=["admin"])


@admin.post("/reload-config")
async def admin_reload() -> dict:
    counts = await reload_config()
    asyncio.create_task(availability.check_all())
    return counts


@admin.get("/settings")
async def admin_settings(db=Depends(dbmod.get_db)) -> dict:  # noqa: ANN001
    search = await get_setting(db, "web_search")
    return {
        "web_search": {"provider": search.get("provider"), "configured": bool(search.get("provider"))},
        "web": await get_setting(db, "web"),
        "defaults": await get_setting(db, "defaults"),
    }


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = FastAPI(title="MultiChat server", version="0.1.0", lifespan=lifespan)

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "version": app.version}

    protected = [Depends(require_token)]
    for r in (attachments.router, endpoints.router, memory.router, models.router, sessions.router, ssh_connections.router, stats.router, votes.router, workspace.router, admin):
        app.include_router(r, dependencies=protected)
    app.include_router(runners.router)  # per-route auth (the WebSocket checks its own token)
    app.include_router(runners.ws_router)
    return app


app = create_app()
