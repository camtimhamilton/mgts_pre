"""Точка входа. Один процесс, один порт, один контейнер."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import api
from .db import init_db
from .routers import imports

STATIC = Path(__file__).with_name("static")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Пульс программы", version="0.2", lifespan=lifespan)
app.include_router(api.router)
app.include_router(imports.router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/form", include_in_schema=False)
def form_page() -> FileResponse:
    return FileResponse(STATIC / "form.html")


@app.get("/import", include_in_schema=False)
def import_page() -> FileResponse:
    return FileResponse(STATIC / "import.html")
