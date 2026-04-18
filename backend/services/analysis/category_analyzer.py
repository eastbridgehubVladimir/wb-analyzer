"""
Оркестратор анализа категории.

Шаги:
  1. Scrape: поиск SKU по ключевому слову → парсинг каждого товара
  2. Metrics: вычисление 4 метрик прямо из спарсенных данных (inline)
  3. Decision: evaluate_product_opportunity → score + verdict
  4. Persist: запись в ClickHouse и PostgreSQL (фоновый эффект)

Ограничение по concurrency: одновременно открыто не более MAX_CONCURRENT
Playwright-браузеров (каждый браузер — отдельный процесс).
"""
import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from services.analysis.ch_writer import save_prices_to_clickhouse, save_products_to_pg
from services.analysis.inline_metrics import (
    build_competition_report,
    build_price_distribution,
    build_revenue_estimate,
    build_sales_velocity,
)
from services.decision_engine import ProductOpportunityInput, evaluate_product_opportunity
from services.decision_engine.base import OpportunityResult
from services.scraper.wb_scraper import ScrapedProduct, wb_scraper
from services.ai_analyst.analyst import analyze_niche, NicheInsights
from schemas.analysis import MetricsSummary
from services.analysis.decision_logger import log_decision
logger = logging.getLogger(__name__)

MAX_CONCURRENT = 3  # одновременных Playwright-браузеров


async def _scrape_all(skus: list[int]) -> list[ScrapedProduct]:
    """Параллельный парсинг с ограничением concurrency."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def _one(sku: int) -> ScrapedProduct | None:
        async with semaphore:
            try:
                return await wb_scraper.scrape_product(sku)
            except Exception as exc:
                logger.warning("SKU %s — ошибка парсинга: %s", sku, exc)
                return None

    results = await asyncio.gather(*(_one(sku) for sku in skus))
    return [r for r in results if r is not None]


async def analyze_category(
    category: str,
    session: AsyncSession,
    max_products: int = 10,
    scrape_pages: int = 1,
) -> tuple[list[ScrapedProduct], OpportunityResult]:
    """
    Полный цикл анализа ниши.

    Возвращает:
      - список спарсенных товаров
      - OpportunityResult с score, verdict, dimensions
    """
    # ── 1. Поиск SKU ────────────────────────────────────
    logger.info("Поиск товаров в категории '%s', страниц: %d", category, scrape_pages)
    skus = await wb_scraper.scrape_search(category, pages=scrape_pages)
    skus = skus[:max_products]
    logger.info("Найдено %d SKU, парсим...", len(skus))

    if not skus:
        logger.warning("Товаров не найдено для '%s'", category)
        return [], _empty_result()

    # ── 2. Парсинг товаров ───────────────────────────────
    products = await _scrape_all(skus)
    logger.info("Успешно спарсено: %d товаров", len(products))

    if not products:
        return [], _empty_result()

    # ── 3. Метрики из спарсенных данных ─────────────────
    revenue    = build_revenue_estimate(products, category)
    velocity   = build_sales_velocity(products)
    competition = build_competition_report(products, category)
    prices     = build_price_distribution(products)

    # ── 4. Decision engine ───────────────────────────────
    result = evaluate_product_opportunity(ProductOpportunityInput(
        revenue=revenue,
        competition=competition,
        velocity=velocity,
        prices=prices,
    ))

    # ── 4б. AI-анализ ────────────────────────────────────
    try:
        metrics_summary = MetricsSummary(
            monthly_revenue_estimate=revenue.total_revenue,
            avg_orders_per_day=velocity.avg_orders_per_day,
            active_sellers=competition.active_sellers,
            competition_level=competition.level,
            median_price=prices.median_price,
            price_iqr=prices.iqr,
            top_20pct_revenue_share=competition.top_20pct_share,
            top_10_revenue_share=competition.top_10_share,
        )
        ai_insights = await analyze_niche(metrics_summary)
        result.ai_insights = ai_insights
        logger.info("AI-анализ добавлен: %d инсайтов", len(ai_insights.insights))
    except Exception as exc:
        logger.warning("AI-анализ не удался (не критично): %s", exc)
    # ── 4в. Логирование решения ──────────────────────────
    try:
        ai_text = ""
        if result.ai_insights:
            ai_text = result.ai_insights.analysis
        log_decision(
            category=category,
            score=result.score,
            verdict=result.verdict.value,
            monthly_revenue=revenue.total_revenue,
            avg_orders_per_day=velocity.avg_orders_per_day,
            active_sellers=competition.active_sellers,
            competition_level=competition.level,
            median_price=prices.median_price,
            ai_analysis=ai_text,
        )
    except Exception as exc:
        logger.warning("Логирование решения не удалось (не критично): %s", exc)
    # ── 5. Персистенция (не блокирует ответ — ошибки логируем) ──
    try:
        await save_products_to_pg(products, session)
    except Exception as exc:
        logger.error("PG write error: %s", exc)

    try:
        our_sku = products[0].wb_sku
        save_prices_to_clickhouse(our_sku, products)
    except Exception as exc:
        logger.error("ClickHouse write error: %s", exc)

    return products, result


def _empty_result() -> OpportunityResult:
    from services.decision_engine.base import DimensionScore, OpportunityResult, Verdict
    return OpportunityResult(
        score=0,
        verdict=Verdict.SKIP,
        summary="Товары не найдены или не удалось спарсить.",
        dimensions=[],
    )
