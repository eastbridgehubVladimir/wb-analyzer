"""
Фоновые задачи на ARQ (async Redis Queue).
Запускаются отдельным процессом: `arq workers.main.WorkerSettings`
"""
import logging

from arq import cron
from arq.connections import RedisSettings

from core.config import settings
from services.scraper.wb_scraper import wb_scraper

logger = logging.getLogger(__name__)


async def task_scrape_product(ctx, wb_sku: int):
    """Спарсить данные одного товара и сохранить в БД."""
    logger.info("Парсим товар SKU=%s", wb_sku)
    product = await wb_scraper.scrape_product(wb_sku)
    if product:
        logger.info("Готово: %s — %s руб.", product.name, product.price)
    return product


async def task_scrape_search(ctx, keyword: str, pages: int = 3):
    """Собрать SKU из поиска и поставить в очередь парсинга."""
    logger.info("Поиск по ключевому слову: %s", keyword)
    skus = await wb_scraper.scrape_search(keyword, pages)
    logger.info("Найдено %d товаров", len(skus))
    for sku in skus:
        await ctx["redis"].enqueue_job("task_scrape_product", sku)
    return skus


class WorkerSettings:
    functions = [task_scrape_product, task_scrape_search]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 5           # одновременно выполняемых задач
    job_timeout = 120      # таймаут одной задачи в секундах

    # Автозадачи по расписанию
    cron_jobs = [
        # Каждый час обновлять топ-товары
        cron(task_scrape_search, minute=0, kwargs={"keyword": "смартфон"}),
    ]
