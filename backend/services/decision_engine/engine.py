"""
evaluate_product_opportunity — rule-based оценка товарной ниши.

Каждое из четырёх измерений даёт до 25 очков.
Итоговый score = сумма (0–100).

Вердикт:
  BUY   score >= 70  — ниша перспективна, заходить
  TEST  score 40–69  — неоднозначно, пробовать малой партией
  SKIP  score < 40   — ниша не привлекательна

──────────────────────────────────────────────────────────
Scoring rules (каждое правило документировано прямо в коде)
──────────────────────────────────────────────────────────
"""
from services.decision_engine.base import (
    DimensionScore,
    OpportunityResult,
    ProductOpportunityInput,
    Verdict,
)
from services.metrics_engine.base import (
    CompetitionLevel,
    CompetitionReport,
    PriceDistribution,
    RevenueEstimate,
    SalesVelocity,
)

# ── Пороги вердикта ──────────────────────────────────────
_THRESHOLD_BUY  = 70
_THRESHOLD_TEST = 40


def _verdict(score: int) -> Verdict:
    if score >= _THRESHOLD_BUY:
        return Verdict.BUY
    if score >= _THRESHOLD_TEST:
        return Verdict.TEST
    return Verdict.SKIP


# ── 1. Объём рынка (revenue_estimate) ───────────────────
# Чем больше ежемесячная выручка ниши — тем лучше.
# Маленький рынок не стоит усилий даже без конкурентов.
#
#   monthly_estimate, ₽    очки
#   ≥ 5 000 000            25   (крупная ниша)
#   ≥ 1 000 000            20   (средняя)
#   ≥ 300 000              13   (небольшая)
#   ≥ 100 000               7   (микро)
#   < 100 000               2   (нет рынка)

def _score_revenue(r: RevenueEstimate) -> DimensionScore:
    m = r.monthly_estimate

    if m >= 5_000_000:
        pts, reason = 25, f"Крупная ниша: ~{m/1e6:.1f} млн ₽/мес"
    elif m >= 1_000_000:
        pts, reason = 20, f"Средняя ниша: ~{m/1e6:.1f} млн ₽/мес"
    elif m >= 300_000:
        pts, reason = 13, f"Небольшая ниша: ~{m/1000:.0f} тыс ₽/мес"
    elif m >= 100_000:
        pts, reason = 7,  f"Микро-ниша: ~{m/1000:.0f} тыс ₽/мес"
    else:
        pts, reason = 2,  f"Рынок слишком мал: ~{m:.0f} ₽/мес"

    # Штраф за сильную концентрацию: если 20% игроков забирают >80%,
    # новичку остаётся немного.
    if r.top_20pct_share > 0.8 and pts > 2:
        pts = max(2, pts - 7)
        reason += f" (⚠ концентрация: топ-20% берут {r.top_20pct_share*100:.0f}% выручки)"

    return DimensionScore("revenue", pts, 25, reason)


# ── 2. Конкуренция (competition_level) ──────────────────
# Меньше конкурентов = проще зайти и получить позиции.
# Дополнительно смотрим на концентрацию: если топ-10 берут >70%
# заказов, рынок уже поделён и новичку места нет.
#
#   CompetitionLevel   базовые очки
#   LOW                25
#   MEDIUM             18
#   HIGH                8
#   SATURATED           2

_COMPETITION_BASE = {
    CompetitionLevel.LOW:       25,
    CompetitionLevel.MEDIUM:    18,
    CompetitionLevel.HIGH:       8,
    CompetitionLevel.SATURATED:  2,
}

def _score_competition(c: CompetitionReport) -> DimensionScore:
    pts = _COMPETITION_BASE[c.level]
    reason = (
        f"{c.active_sellers} активных продавцов "
        f"(уровень: {c.level.value})"
    )

    if c.top_10_revenue_share > 0.70 and pts > 2:
        pts = max(2, pts - 8)
        reason += f", топ-10 держат {c.top_10_revenue_share*100:.0f}% заказов — рынок закрыт"
    elif c.avg_rating < 4.0 and c.avg_reviews > 50:
        pts = min(25, pts + 3)
        reason += " (конкуренты слабые по качеству — есть окно)"

    return DimensionScore("competition", pts, 25, reason)


# ── 3. Скорость продаж (sales_velocity) ─────────────────
# Высокая скорость = ликвидный товар = быстрый оборот.
# Для новичка важно не застрять с залежалым стоком.
#
#   avg_orders_per_day   очки
#   ≥ 200                25   (очень быстро)
#   ≥ 50                 20
#   ≥ 20                 14
#   ≥ 5                   8
#   < 5                   3   (товар почти не продаётся)

def _score_velocity(v: SalesVelocity) -> DimensionScore:
    apd = v.avg_orders_per_day

    if apd >= 200:
        pts, reason = 25, f"Высокий спрос: {apd:.1f} заказов/день"
    elif apd >= 50:
        pts, reason = 20, f"Хороший спрос: {apd:.1f} заказов/день"
    elif apd >= 20:
        pts, reason = 14, f"Умеренный спрос: {apd:.1f} заказов/день"
    elif apd >= 5:
        pts, reason = 8,  f"Слабый спрос: {apd:.1f} заказов/день"
    else:
        pts, reason = 3,  f"Почти нет продаж: {apd:.1f} заказов/день"

    # Бонус: если пиковые продажи сильно выше среднего — сезонность,
    # можно поймать волну.
    if v.peak_orders_per_day > apd * 3 and apd >= 5:
        pts = min(25, pts + 3)
        reason += f" (пик до {v.peak_orders_per_day:.0f}/день — есть сезонность)"

    return DimensionScore("velocity", pts, 25, reason)


# ── 4. Распределение цен (price_distribution) ───────────
# Смотрим на медианную цену (маржинальность) и ширину ценового
# диапазона (IQR/median) — пространство для позиционирования.
#
# Медианная цена:
#   ≥ 2000 ₽   высокий чек, хорошая маржа
#   ≥ 800 ₽    нормальный сегмент
#   ≥ 300 ₽    низкий чек, маржа под давлением
#   < 300 ₽    commodities, сложно конкурировать
#
# IQR / median (относительный разброс цен):
#   0.15–0.60  здоровый разброс — есть место для позиционирования
#   < 0.15     рынок монолитный, ценовая война
#   > 0.60     хаос, покупатель не понимает цену

def _score_prices(p: PriceDistribution) -> DimensionScore:
    if p.sample_size == 0:
        return DimensionScore("prices", 0, 25, "Нет данных о ценах конкурентов")

    # Оценка по медиане
    if p.median >= 2000:
        pts, reason = 20, f"Высокий чек: медиана {p.median:.0f} ₽"
    elif p.median >= 800:
        pts, reason = 16, f"Средний чек: медиана {p.median:.0f} ₽"
    elif p.median >= 300:
        pts, reason = 10, f"Низкий чек: медиана {p.median:.0f} ₽"
    else:
        pts, reason = 4,  f"Очень низкий чек: медиана {p.median:.0f} ₽"

    # Корректировка по ширине диапазона
    relative_iqr = p.iqr / p.median if p.median > 0 else 0
    if 0.15 <= relative_iqr <= 0.60:
        pts = min(25, pts + 5)
        reason += f", здоровый разброс цен (IQR {p.iqr:.0f} ₽)"
    elif relative_iqr < 0.15:
        pts = max(0, pts - 3)
        reason += f", цены зажаты — ценовая война (IQR {p.iqr:.0f} ₽)"
    else:
        reason += f", широкий разброс цен (IQR {p.iqr:.0f} ₽) — рынок нестабилен"

    return DimensionScore("prices", pts, 25, reason)


# ── Сборка итогового результата ──────────────────────────

def _build_summary(score: int, verdict: Verdict, dims: list[DimensionScore]) -> str:
    weakest = min(dims, key=lambda d: d.score / d.max_score)
    strongest = max(dims, key=lambda d: d.score / d.max_score)

    if verdict == Verdict.BUY:
        return (
            f"Ниша привлекательна (score {score}). "
            f"Сильная сторона: {strongest.reason}."
        )
    if verdict == Verdict.TEST:
        return (
            f"Неоднозначная ниша (score {score}). "
            f"Слабое место: {weakest.reason}. Рекомендуется тестовая партия."
        )
    return (
        f"Ниша не рекомендована (score {score}). "
        f"Основная проблема: {weakest.reason}."
    )


# ── Публичная функция ────────────────────────────────────

def evaluate_product_opportunity(data: ProductOpportunityInput) -> OpportunityResult:
    """
    Оценивает привлекательность товарной ниши.

    Принимает: ProductOpportunityInput (4 метрики из metrics_engine)
    Возвращает: OpportunityResult (score 0–100, verdict, объяснение)
    """
    dims = [
        _score_revenue(data.revenue),
        _score_competition(data.competition),
        _score_velocity(data.velocity),
        _score_prices(data.prices),
    ]

    score = sum(d.score for d in dims)
    verdict = _verdict(score)
    summary = _build_summary(score, verdict, dims)

    return OpportunityResult(score=score, verdict=verdict, summary=summary, dimensions=dims)
