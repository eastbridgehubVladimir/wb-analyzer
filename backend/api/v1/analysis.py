"""
POST /api/v1/analysis/category

Полный цикл: парсинг WB → метрики → decision engine → результат.
"""
import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.deps import pg_session
from schemas.analysis import (
    CategoryAnalysisRequest,
    CategoryAnalysisResponse,
    DimensionOut,
    MetricsSummary,
    ScrapedProductOut,
)
from services.analysis.category_analyzer import analyze_category
from services.analysis.inline_metrics import (
    build_competition_report,
    build_price_distribution,
    build_revenue_estimate,
    build_sales_velocity,
)
from services.pdf_export import create_pdf_from_analysis
from core.config import settings
from services.mpstats_client import get_niche_details
from datetime import datetime, timedelta
from sqlalchemy import text
from services.scraper.wb_scraper import ScrapedProduct as ScrapedProductModel
from services.decision_engine.engine import evaluate_product_opportunity
from services.decision_engine.base import ProductOpportunityInput
from services.ai_analyst.analyst import analyze_niche
from services.metrics_engine.base import (
    CompetitionLevel as CL,
    RevenueEstimate, SalesVelocity, CompetitionReport, PriceDistribution,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analysis", tags=["analysis"])


@router.post("/category", response_model=CategoryAnalysisResponse)
async def analyze_category_endpoint(
    payload: CategoryAnalysisRequest,
    db: AsyncSession = Depends(pg_session),
):
    """
    Анализирует товарную нишу Wildberries.

    1. Парсит поисковую выдачу WB по `category`
    2. Парсит каждый найденный товар (цена, рейтинг, отзывы)
    3. Вычисляет 4 метрики ниши
    4. Прогоняет через decision_engine → score 0-100, verdict BUY/TEST/SKIP
    5. Сохраняет данные в PostgreSQL и ClickHouse

    ⚠ Время ответа: 30–120 сек в зависимости от `max_products`.
    Для production вынести в ARQ-воркер и вернуть job_id.
    """
    try:
        products, decision = await analyze_category(
            category=payload.category,
            session=db,
            max_products=payload.max_products,
            scrape_pages=payload.scrape_pages,
        )
    except Exception as exc:
        logger.exception("Category analysis failed")
        raise HTTPException(
            status_code=503,
            detail=f"Не удалось выполнить анализ категории: {exc}",
        )

    # Собираем MetricsSummary из спарсенных данных для ответа
    revenue     = build_revenue_estimate(products, payload.category)
    velocity    = build_sales_velocity(products)
    competition = build_competition_report(products, payload.category)
    prices      = build_price_distribution(products)

    metrics = MetricsSummary(
        monthly_revenue_estimate=revenue.monthly_estimate,
        avg_orders_per_day=velocity.avg_orders_per_day,
        active_sellers=competition.active_sellers,
        competition_level=competition.level.value,
        median_price=prices.median,
        price_iqr=prices.iqr,
        top_20pct_revenue_share=revenue.top_20pct_share,
        top_10_revenue_share=competition.top_10_revenue_share,
    )

    return CategoryAnalysisResponse(
        category=payload.category,
        products_scraped=len(products),
        score=decision.score,
        verdict=decision.verdict.value,
        summary=decision.summary,
        dimensions=[
            DimensionOut(
                name=d.name,
                score=d.score,
                max_score=d.max_score,
                reason=d.reason,
            )
            for d in decision.dimensions
        ],
        metrics=metrics,
        products=[
            ScrapedProductOut(
                wb_sku=p.wb_sku,
                name=p.name,
                brand=p.brand or None,
                price=p.price,
                old_price=p.old_price,
                rating=p.rating,
                reviews_count=p.reviews_count,
                images=p.images,
            )
            for p in products
        ],
    )


@router.post("/category/export-pdf")
async def export_category_pdf(
    payload: CategoryAnalysisRequest,
    db: AsyncSession = Depends(pg_session),
    stub: bool = False,
):
    """
    Экспортирует результаты анализа в PDF.

    1. Выполняет анализ категории (как /category)
    2. Генерирует красивый PDF с метриками, товарами, вердиктом
    3. Возвращает PDF файл для скачивания

    ⚠ Время ответа: 30–120 сек в зависимости от `max_products`.
    """
    from services.decision_engine.base import OpportunityResult, Verdict

    products = []
    decision = None

    # Используем готовые данные из браузера если переданы
    pc = payload.pre_computed or {}

    # Если pre_computed не передан — загружаем из таблицы niches (наша БД с 7127 нишами)
    if not pc and not stub:
        lookup = payload.niche_full or payload.category
        try:
            res = await db.execute(
                text("""
                    SELECT revenue, avg_price, commission, buyout_pct, profit_pct,
                           sellers_with_sales, orders, products, mpstats_path,
                           niche_status, turnover, COALESCE(display_name, name)
                    FROM niches
                    WHERE LOWER(name) = LOWER(:q)
                       OR LOWER(COALESCE(display_name, name)) = LOWER(:q)
                    ORDER BY revenue DESC NULLS LAST LIMIT 1
                """),
                {"q": lookup}
            )
            niche_row = res.fetchone()
            if niche_row and niche_row[0] and float(niche_row[0]) > 0:
                rev     = float(niche_row[0] or 0)
                price   = float(niche_row[1] or 0)
                comm    = float(niche_row[2] or 0)
                buyout  = float(niche_row[3] or 0)
                profit  = float(niche_row[4] or 0)
                sws     = int(niche_row[5] or 0)
                orders  = int(niche_row[6] or 0)
                turnover= float(niche_row[10] or 0)
                if sws > 100: comp_level = 'high'
                elif sws > 20: comp_level = 'medium'
                else: comp_level = 'low'
                top_20 = 0.6 if sws > 5 else 0.9
                pc = {
                    'monthly_revenue_estimate': rev,
                    'annual_revenue': rev * 12,
                    'avg_orders_per_day': round(orders / 30, 1),
                    'orders_per_month': orders,
                    'active_sellers': sws,
                    'sellers_with_sales': sws,
                    'competition_level': comp_level,
                    'median_price': price,
                    'avg_check': price,
                    'price_iqr': round(price * 0.3),
                    'top_20pct_revenue_share': top_20,
                    'top_10_revenue_share': round(top_20 * 0.6, 2),
                    'score': 50,
                    'verdict': 'MAYBE',
                    'summary': lookup,
                    'buyout_pct': buyout,
                    'profit_pct': profit,
                    'commission': comm,
                    'turnover': turnover,
                    'lost_revenue_pct': 0,
                    'lost_revenue': 0,
                    'avg_rating': 0,
                }
                logger.info("Ниша '%s' загружена из БД: rev=%.0f, price=%.0f", lookup, rev, price)
                # Вычисляем реальный score через decision engine
                try:
                    if sws > 200:  _comp = CL.SATURATED
                    elif sws > 50: _comp = CL.HIGH
                    elif sws > 10: _comp = CL.MEDIUM
                    else:          _comp = CL.LOW
                    _iqr = round(price * 0.3)
                    _opp = evaluate_product_opportunity(ProductOpportunityInput(
                        revenue=RevenueEstimate(
                            category=lookup, period_days=30,
                            total_revenue=rev, monthly_estimate=rev,
                            avg_revenue_per_sku=rev / max(sws, 1),
                            top_20pct_share=top_20,
                        ),
                        competition=CompetitionReport(
                            category=lookup, active_sellers=sws,
                            level=_comp, avg_reviews=100.0, avg_rating=4.2,
                            top_10_revenue_share=round(top_20 * 0.6, 2),
                        ),
                        velocity=SalesVelocity(
                            period_days=30,
                            avg_orders_per_day=round(orders / 30, 1),
                            peak_orders_per_day=round(orders / 30, 1) * 1.5,
                            median_orders_per_day=round(orders / 30, 1) * 0.8,
                            total_orders=orders,
                        ),
                        prices=PriceDistribution(
                            sample_size=max(sws, 10),
                            min_price=price * 0.4, max_price=price * 2.0,
                            p25=price * 0.75, median=price, p75=price * 1.25,
                            std_dev=_iqr * 0.6, iqr=_iqr,
                        ),
                    ))
                    pc['score'] = _opp.score
                    pc['verdict'] = _opp.verdict.value
                    pc['summary'] = _opp.summary
                    logger.info("Score для '%s': %d (%s)", lookup, _opp.score, _opp.verdict.value)
                except Exception as _se:
                    logger.warning("Не удалось вычислить score: %s", _se)
        except Exception as exc:
            logger.warning("Не удалось загрузить нишу из БД: %s", exc)

    if pc or stub:
        if stub:
            pc = {
                'monthly_revenue_estimate': 118454,
                'avg_orders_per_day': 0.5,
                'active_sellers': 3,
                'competition_level': 'low',
                'median_price': 1999,
                'price_iqr': 1000,
                'top_20pct_revenue_share': 0.008,
                'top_10_revenue_share': 0.3,
                'score': 48,
                'verdict': 'TEST',
                'summary': payload.category,
            }

        # Строим MetricsSummary из готовых данных
        metrics = MetricsSummary(
            monthly_revenue_estimate=float(pc.get('monthly_revenue_estimate', 0)),
            avg_orders_per_day=float(pc.get('avg_orders_per_day', 0)),
            active_sellers=int(pc.get('active_sellers', 0)),
            competition_level=str(pc.get('competition_level', 'low')),
            median_price=float(pc.get('median_price', 0)),
            price_iqr=float(pc.get('price_iqr', 0)),
            top_20pct_revenue_share=float(pc.get('top_20pct_revenue_share', 0)),
            top_10_revenue_share=float(pc.get('top_10_revenue_share', 0)),
        )
        score_val = int(pc.get('score', 0))
        verdict_str = str(pc.get('verdict', 'TEST')).upper()
        try:
            verdict_enum = Verdict(verdict_str)
        except ValueError:
            verdict_enum = Verdict.TEST
        decision = OpportunityResult(
            score=score_val,
            verdict=verdict_enum,
            summary=str(pc.get('summary', payload.category)),
            dimensions=[],
        )
        # Сохраняем дополнительные поля ниши для PDF
        decision.niche_extra = {
            'annual_revenue': float(pc.get('annual_revenue', 0)),
            'orders_per_month': int(pc.get('orders_per_month', 0)),
            'sellers_with_sales': int(pc.get('sellers_with_sales', 0)),
            'buyout_pct': float(pc.get('buyout_pct', 0)),
            'profit_pct': float(pc.get('profit_pct', 0)),
            'turnover': float(pc.get('turnover', 0)),
            'commission': float(pc.get('commission', 0)),
            'lost_revenue_pct': float(pc.get('lost_revenue_pct', 0)),
            'lost_revenue': float(pc.get('lost_revenue', 0)),
            'avg_check': float(pc.get('avg_check', 0)),
            'avg_rating': float(pc.get('avg_rating', 0)),
            # AI-аналитика с текущей страницы
            'insights': list(pc.get('insights') or []),
            'hypotheses': list(pc.get('hypotheses') or []),
            'analysis': str(pc.get('analysis') or ''),
        }

        # Пробуем получить товары из MPStats для таблицы (не критично)
        if settings.mpstats_token:
            try:
                lookup_name = payload.niche_full or payload.category
                res = await db.execute(
                    text("SELECT mpstats_path FROM niches WHERE (LOWER(name) = LOWER(:q) OR LOWER(name) LIKE LOWER(:ql)) AND mpstats_path IS NOT NULL ORDER BY revenue DESC LIMIT 1"),
                    {"q": lookup_name, "ql": f"%{lookup_name}%"}
                )
                row = res.fetchone()
                mpstats_path = row[0] if (row and row[0]) else lookup_name
                d2 = datetime.utcnow().date()
                d1 = d2 - timedelta(days=30)
                details = get_niche_details(mpstats_path, d1.isoformat(), d2.isoformat())
                if details:
                    rows_data = details.get('data') or details.get('rows') or []
                    for r in rows_data[: payload.max_products or 50]:
                        try:
                            sp = ScrapedProductModel(
                                wb_sku=int(r.get('nm_id') or r.get('nmId') or 0),
                                name=str(r.get('name') or r.get('brand') or ''),
                                brand=str(r.get('brand') or 'N/A'),
                                price=float(r.get('avg_price') or r.get('avg_price_with_discount') or r.get('price') or 0),
                                price_with_card=None,
                                old_price=None,
                                rating=float(r.get('rating') or 0),
                                reviews_count=int(r.get('orders') or r.get('reviews') or 0),
                                images=[],
                                attributes={},
                            )
                            products.append(sp)
                        except Exception:
                            continue
                    logger.info("MPStats товары для таблицы: %d шт.", len(products))
            except Exception as exc:
                logger.warning("MPStats для таблицы товаров не сработал: %s", exc)

    # Если метрики ещё не построены (нет pre_computed) — считаем из товаров
    if not (pc or stub):
        revenue     = build_revenue_estimate(products, payload.category)
        velocity    = build_sales_velocity(products)
        competition = build_competition_report(products, payload.category)
        prices      = build_price_distribution(products)
        metrics = MetricsSummary(
            monthly_revenue_estimate=revenue.monthly_estimate,
            avg_orders_per_day=velocity.avg_orders_per_day,
            active_sellers=competition.active_sellers,
            competition_level=competition.level.value,
            median_price=prices.median,
            price_iqr=prices.iqr,
            top_20pct_revenue_share=revenue.top_20pct_share,
            top_10_revenue_share=competition.top_10_revenue_share,
        )
        if not decision:
            try:
                decision = evaluate_product_opportunity(ProductOpportunityInput(
                    revenue=revenue, competition=competition,
                    velocity=velocity, prices=prices,
                ))
            except Exception:
                decision = OpportunityResult(
                    score=0, verdict=Verdict.TEST,
                    summary="Decision engine temporarily unavailable.", dimensions=[],
                )

    # AI-аналитика: попытка вызвать analyze_niche, но при отсутствии ключей вернём демо-версию
    try:
        ai_insights = await analyze_niche(metrics)
        # Помещаем инсайты в decision, чтобы потом попасть в PDF
        decision.ai_insights = ai_insights
    except Exception:
        # анализ не критичен
        decision.ai_insights = None

    # Собираем данные для PDF
    niche_extra = getattr(decision, 'niche_extra', {}) or {}

    # Берём AI-инсайты: сначала из браузера (уже готовы), потом из analyze_niche
    ai_raw = getattr(decision, 'ai_insights', None)
    insights_list = niche_extra.get('insights') or (ai_raw.insights if ai_raw else [])
    hypotheses_list = niche_extra.get('hypotheses') or (ai_raw.hypotheses if ai_raw else [])
    analysis_text = niche_extra.get('analysis') or (ai_raw.analysis if ai_raw else '')

    agents_data = dict(payload.agents or {})
    charts_data = dict(payload.charts or {})

    analysis_data = {
        'category': payload.category,
        'products_scraped': len(products),
        'score': decision.score,
        'verdict': decision.verdict.value,
        'summary': decision.summary,
        'agents': agents_data,
        'charts': charts_data,
        'dimensions': [
            {
                'name': d.name,
                'score': d.score,
                'max_score': d.max_score,
                'reason': d.reason,
            }
            for d in decision.dimensions
        ],
        'metrics': {
            'monthly_revenue_estimate': metrics.monthly_revenue_estimate,
            'annual_revenue': niche_extra.get('annual_revenue', metrics.monthly_revenue_estimate * 12),
            'avg_orders_per_day': metrics.avg_orders_per_day,
            'orders_per_month': niche_extra.get('orders_per_month', 0),
            'active_sellers': metrics.active_sellers,
            'sellers_with_sales': niche_extra.get('sellers_with_sales', 0),
            'competition_level': metrics.competition_level,
            'median_price': metrics.median_price,
            'price_iqr': metrics.price_iqr,
            'top_20pct_revenue_share': metrics.top_20pct_revenue_share,
            'top_10_revenue_share': metrics.top_10_revenue_share,
            'buyout_pct': niche_extra.get('buyout_pct', 0),
            'profit_pct': niche_extra.get('profit_pct', 0),
            'turnover': niche_extra.get('turnover', 0),
            'commission': niche_extra.get('commission', 0),
            'lost_revenue_pct': niche_extra.get('lost_revenue_pct', 0),
            'lost_revenue': niche_extra.get('lost_revenue', 0),
            'avg_check': niche_extra.get('avg_check', metrics.median_price),
            'avg_rating': niche_extra.get('avg_rating', 0),
        },
        'products': [
            {
                'wb_sku': p.wb_sku,
                'name': p.name,
                'brand': p.brand or 'N/A',
                'price': p.price,
                'old_price': p.old_price,
                'rating': p.rating,
                'reviews_count': p.reviews_count,
                'images': p.images,
            }
            for p in products
        ],
        'ai_insights': {
            'insights': insights_list,
            'hypotheses': hypotheses_list,
            'analysis': analysis_text,
        },
    }

    # Генерируем PDF (учитываем уровень отчёта)
    report_level = payload.report_level if hasattr(payload, 'report_level') else 'standard'
    try:
        pdf_buffer = create_pdf_from_analysis(analysis_data, level=report_level)
    except Exception as exc:
        logger.exception("PDF export failed")
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось сформировать PDF: {exc}",
        )

    # Возвращаем PDF для скачивания
    filename = f"WB-Analysis-{payload.category.replace('/', '-')}-{report_level}.pdf"
    encoded_filename = quote(filename, safe='')
    return StreamingResponse(
        iter([pdf_buffer.getvalue()]),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=\"report.pdf\"; filename*=UTF-8''{encoded_filename}"}
    )
