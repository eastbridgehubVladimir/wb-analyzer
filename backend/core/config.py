"""
Настройки приложения — читаются из .env файла.
Pydantic автоматически валидирует типы и выдаёт понятные ошибки.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PostgreSQL
    database_url: str = "postgresql+asyncpg://wb:wb_secret@localhost:5432/wb_saas"

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

    # Безопасность
    secret_key: str = "change_me"
    cors_origins: list[str] = ["http://localhost:3000"]

    @property
    def proxies(self) -> list[str]:
        return [p.strip() for p in self.proxy_list.split(",") if p.strip()]


settings = Settings()
