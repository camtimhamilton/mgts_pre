"""Настройки приложения. Всё, что меняется на финале, — здесь, а не в коде."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_view: str = "hr"  # hr | exec
    team_name: str = "ПУЛЬС"
    demo_mode: bool = True
    analysis_mode: Literal["ml", "demo"] = "ml"
    ml_api_url: str = "http://127.0.0.1:8001"
    ml_api_timeout: float = 10.0
    ml_program_id: str = "synthetic_dashboard"
    ml_mapping_version: str = "dashboard-demo-1"
    ml_knowledge_version: str = "0.1.0"

    llm_provider: str = "deepseek"  # deepseek | gigachat
    llm_mode: str = "fake"  # legacy demo only; real ML never runs inside backend
    llm_api_key: str = ""
    llm_base_url: str = ""
    model_name: str = ""
    llm_timeout: float = 20.0
    llm_retries: int = 2

    ingest_key: str = ""

    db_path: str = "./data/app.db"
    program_start: str = "2026-02-01"

    @property
    def data_source(self) -> str:
        """fixture — пока данных нет, db — как только появились снапшоты."""
        return "fixture" if self.demo_mode else "db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
