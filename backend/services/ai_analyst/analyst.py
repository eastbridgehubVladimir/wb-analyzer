"""
AI-аналитик ниши.

analyze_niche(metrics_summary) → NicheInsights

Задача: интерпретировать метрики ниши на естественном языке.
- НЕ считает метрики (это делает metrics_engine)
- НЕ принимает решение входить/не входить (это делает decision_engine)
- Только: объяснение, инсайты, гипотезы для тестирования
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import anthropic

from core.config import settings
from schemas.analysis import MetricsSummary

logger = logging.getLogger(__name__)

_MODEL = "claude-opus-4-6"

_SYSTEM_PROMPT = """\
Ты аналитик товарных ниш для маркетплейса Wildberries.
Тебе дают агрегированные метрики ниши — уже посчитанные цифры.
Твоя задача — интерпретировать их: объяснить что происходит в нише,
выделить нетривиальные паттерны и предложить конкретные гипотезы для проверки.

Правила:
- НЕ пересчитывай метрики и не выводи формулы
- НЕ выноси финальный вердикт «заходить / не заходить» — это не твоя задача
- Говори конкретно: если видишь высокую концентрацию — скажи что это значит для новичка
- Гипотезы должны быть проверяемыми (что именно сделать, чтобы проверить)
- Отвечай на русском языке
- Будь лаконичен: 3-5 инсайтов, 2-4 гипотезы, 2-3 предложения итогового анализа
"""


@dataclass
class NicheInsights:
    """Результат AI-анализа метрик ниши."""
    insights: list[str]      # ключевые наблюдения по метрикам
    hypotheses: list[str]    # что стоит протестировать
    analysis: str            # краткое связное резюме
    raw_response: str = field(repr=False, default="")  # полный текст ответа модели


def _format_metrics(m: MetricsSummary) -> str:
    return f"""\
Метрики ниши:
- Оценочная выручка ниши: {m.monthly_revenue_estimate:,.0f} ₽/мес
- Среднее заказов в день: {m.avg_orders_per_day:.1f}
- Активных продавцов: {m.active_sellers}
- Уровень конкуренции: {m.competition_level}
- Медианная цена: {m.median_price:,.0f} ₽
- IQR цен (разброс p25–p75): {m.price_iqr:,.0f} ₽
- Доля топ-20% SKU в выручке: {m.top_20pct_revenue_share * 100:.1f}%
- Доля топ-10 SKU в заказах: {m.top_10_revenue_share * 100:.1f}%
"""


def _parse_response(text: str) -> tuple[list[str], list[str], str]:
    """
    Разбирает структурированный ответ модели на три секции.
    Ожидаемый формат (задаётся в user-промпте):

    ИНСАЙТЫ:
    - ...

    ГИПОТЕЗЫ:
    - ...

    АНАЛИЗ:
    ...
    """
    sections: dict[str, list[str]] = {"ИНСАЙТЫ": [], "ГИПОТЕЗЫ": [], "АНАЛИЗ": []}
    current: str | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.rstrip(":").upper()
        if upper in sections:
            current = upper
            continue
        if current:
            sections[current].append(stripped.lstrip("- "))

    insights = sections["ИНСАЙТЫ"] or [text]
    hypotheses = sections["ГИПОТЕЗЫ"] or []
    analysis = " ".join(sections["АНАЛИЗ"]) or text

    return insights, hypotheses, analysis

def _demo_insights(m: MetricsSummary) -> NicheInsights:
    """Реалистичный демо-анализ на основе реальных метрик — без API-ключа."""

    insights = []
    hypotheses = []

    # Анализ конкуренции
    if m.active_sellers > 100:
        insights.append(
            f"Ниша насыщена: {m.active_sellers} активных продавцов. "
            "Топ-20% SKU забирают "
            f"{m.top_20pct_revenue_share * 100:.0f}% выручки — вход без УТП рискован."
        )
    elif m.active_sellers > 30:
        insights.append(
            f"Умеренная конкуренция ({m.active_sellers} продавцов). "
            "Есть место для нового игрока с правильным позиционированием."
        )
    else:
        insights.append(
            f"Низкая конкуренция: всего {m.active_sellers} продавцов. "
            "Ниша либо растущая, либо с низким спросом — нужно проверить динамику."
        )

    # Анализ выручки и спроса
    if m.avg_orders_per_day > 100:
        insights.append(
            f"Высокий спрос: {m.avg_orders_per_day:.0f} заказов/день по нише. "
            f"Оценочная выручка {m.monthly_revenue_estimate:,.0f} ₽/мес говорит о зрелом рынке."
        )
    elif m.avg_orders_per_day > 20:
        insights.append(
            f"Стабильный спрос: {m.avg_orders_per_day:.0f} заказов/день. "
            "Достаточно для тестовой поставки."
        )
    else:
        insights.append(
            f"Низкий спрос: {m.avg_orders_per_day:.0f} заказов/день. "
            "Рекомендуется начать с минимальной партии для проверки."
        )

    # Анализ цен
    price_spread_pct = (m.price_iqr / m.median_price * 100) if m.median_price else 0
    if price_spread_pct > 50:
        insights.append(
            f"Широкий ценовой разброс (IQR {m.price_iqr:,.0f} ₽ при медиане {m.median_price:,.0f} ₽). "
            "Рынок сегментирован — есть место для премиум и эконом позиций."
        )
    else:
        insights.append(
            f"Цены сконцентрированы вокруг {m.median_price:,.0f} ₽. "
            "Ценовая война вероятна — дифференциация важнее цены."
        )

    # Анализ концентрации
    if m.top_10_revenue_share > 0.6:
        insights.append(
            f"Высокая концентрация: топ-10 SKU дают {m.top_10_revenue_share * 100:.0f}% заказов. "
            "Новичку сложно конкурировать с лидерами напрямую."
        )
        hypotheses.append(
            "Найти подкатегорию или размерный ряд где лидеры слабо представлены — "
            "проверить через поиск по long-tail запросам."
        )

    # Гипотезы
    hypotheses.append(
        f"Протестировать вход в ценовом сегменте {m.median_price * 0.85:,.0f}–"
        f"{m.median_price * 1.1:,.0f} ₽ с минимальной партией 30–50 единиц."
    )
    hypotheses.append(
        "Проверить сезонность ниши: сравнить заказы за последние 30 и 90 дней — "
        "если рост более 20%, ниша на подъёме."
    )

    # Итоговый анализ
    if m.competition_level == "LOW":
        analysis = (
            f"Ниша с низкой конкуренцией и стабильным спросом ({m.avg_orders_per_day:.0f} заказов/день). "
            "Благоприятный момент для входа до насыщения рынка. "
            "Рекомендуется тестовая поставка с фокусом на отзывы и SEO карточки."
        )
    elif m.competition_level == "HIGH":
        analysis = (
            f"Высококонкурентная ниша с {m.active_sellers} продавцами. "
            "Вход возможен только через чёткое УТП: уникальная упаковка, "
            "расширенная комплектация или работа с нишевым спросом."
        )
    else:
        analysis = (
            f"Сбалансированная ниша: {m.active_sellers} продавцов, "
            f"{m.avg_orders_per_day:.0f} заказов/день, медианная цена {m.median_price:,.0f} ₽. "
            "Вход оправдан при наличии конкурентной цены и быстрой логистики."
        )

    return NicheInsights(
        insights=insights,
        hypotheses=hypotheses,
        analysis=analysis,
    )
async def analyze_niche(metrics_summary: MetricsSummary) -> NicheInsights:
    """
    Отправляет метрики ниши в Claude и возвращает текстовые инсайты.

    Raises:
        anthropic.APIError: при проблемах с API (сеть, ключ, лимиты)
    """
    if not settings.anthropic_api_key:
        logger.warning("ANTHROPIC_API_KEY не задан — возвращаем демо-анализ")
        return _demo_insights(metrics_summary)

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    user_message = f"""\
{_format_metrics(metrics_summary)}

Дай анализ в следующем формате (строго):

ИНСАЙТЫ:
- <наблюдение 1>
- <наблюдение 2>
- ...

ГИПОТЕЗЫ:
- <что протестировать 1>
- <что протестировать 2>
- ...

АНАЛИЗ:
<2-3 предложения итогового резюме>
"""

    logger.info("Запрос к Claude для анализа ниши (sellers=%d)", metrics_summary.active_sellers)

    try:
        message = await client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = message.content[0].text
        insights, hypotheses, analysis = _parse_response(raw)

        logger.info("Получен AI-анализ: %d инсайтов, %d гипотез", len(insights), len(hypotheses))

        return NicheInsights(
            insights=insights,
            hypotheses=hypotheses,
            analysis=analysis,
            raw_response=raw,
        )
    except Exception as exc:
        logger.warning("Ошибка API (%s) — возвращаем демо-анализ", exc)
        return _demo_insights(metrics_summary)