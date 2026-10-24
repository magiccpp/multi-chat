from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import memory
from app.db import get_db

router = APIRouter(prefix="/api/memory", tags=["memory"])


class MemoryIn(BaseModel):
    text: str


class MemoryOut(BaseModel):
    text: str
    project_files: list[str] = list(memory.PROJECT_FILES)


@router.get("", response_model=MemoryOut)
async def get_memory(db: AsyncSession = Depends(get_db)) -> MemoryOut:
    """The user's global memory; every model gets it in its instructions."""
    return MemoryOut(text=await memory.load(db))


@router.put("", response_model=MemoryOut)
async def put_memory(body: MemoryIn, db: AsyncSession = Depends(get_db)) -> MemoryOut:
    return MemoryOut(text=await memory.save(db, body.text))


@router.post("/append", response_model=MemoryOut)
async def append_memory(body: MemoryIn, db: AsyncSession = Depends(get_db)) -> MemoryOut:
    """Add one line ("# remember this" in the clients)."""
    return MemoryOut(text=await memory.append(db, body.text))
