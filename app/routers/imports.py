"""Импорт анкет из файла. Заглушка фазы 1 — реализуется отдельной задачей.

Контракт: docs/import-contract.md. Этот файл — единственное место, где живут
эндпоинты импорта; остальной код трогать не нужно.
"""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/import", tags=["import"])


@router.post("/preview")
async def preview() -> None:
    raise HTTPException(status_code=501, detail="импорт ещё не реализован")


@router.post("/commit")
async def commit() -> None:
    raise HTTPException(status_code=501, detail="импорт ещё не реализован")
