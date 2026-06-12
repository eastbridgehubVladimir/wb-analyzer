"""
Настройки приложения — читаются из .env файла.
Pydantic автоматически валидирует типы и выдаёт понятные ошибки.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PostgreSQL — async driver для FastAPI, sync для скриптов
    async_database_url: str = "postgresql+asyncpg://wb:wb_secret@localhost:5432/wb_saas"

    @property
    def database_url(self) -> str:
        """Возвращает async URL (для FastAPI). Если задан ASYNC_DATABASE_URL — использует его."""
        return self.async_database_url

    # ClickHouse
    clickhouse_host: str = "localhost"
    clickhouse_port: int = 9000
    clickhouse_db: str = "wb_analytics"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # WB API
    wb_api_key: str = ""
    wb_api_base_url: str = "https://suppliers-api.wildberries.ru"

    # Прокси (через запятую)
    proxy_list: str = ""

    # AI
    anthropic_api_key: str = ""
    # MPStats
    mpstats_token: str = ""
    # Безопасность
    secret_key: str = "change_me"
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:8001",
        "http://127.0.0.1:8002",
        "http://127.0.0.1:8003",
        "http://127.0.0.1:8004",
    ]

    @property
    def proxies(self) -> list[str]:
        return [p.strip() for p in self.proxy_list.split(",") if p.strip()]


settings = Settings()
