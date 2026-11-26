"""Точка входа. Один процесс, один порт, один контейнер."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import api
from .db import init_db
from .routers import imports
from .pair_api import router as pair_router
from .pair_store import init_store

STATIC = Path(__file__).with_name("static")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    init_store()
    yield


app = FastAPI(title="ПУЛЬС · дашборд программы", version="2.0.0", lifespan=lifespan)
app.include_router(api.router)
app.include_router(imports.router)
app.include_router(pair_router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "pair.html")


@app.get("/legacy", include_in_schema=False)
def legacy_index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/form", include_in_schema=False)
def form_page() -> FileResponse:
    return FileResponse(STATIC / "form.html")


@app.get("/import", include_in_schema=False)
def import_page() -> FileResponse:
    return FileResponse(STATIC / "import.html")
