"""
Типы входа и выхода decision_engine.
"""
from dataclasses import dataclass, field
from enum import Enum

from services.metrics_engine.base import (
    CompetitionReport,
    PriceDistribution,
    RevenueEstimate,
    SalesVelocity,
)


class Verdict(str, Enum):
    BUY  = "BUY"   # score >= 70: входить в нишу
    TEST = "TEST"  # score 40–69: тестовая закупка
    SKIP = "SKIP"  # score < 40:  не заходить


@dataclass
class ProductOpportunityInput:
    """Входные данные для оценки ниши."""
    revenue:     RevenueEstimate
    competition: CompetitionReport
    velocity:    SalesVelocity
    prices:      PriceDistribution


@dataclass
class DimensionScore:
    """Оценка одного измерения с объяснением."""
    name:      str
    score:     int   # фактическое количество очков
    max_score: int   # максимум за это измерение
    reason:    str   # почему поставили именно столько


@dataclass
class OpportunityResult:
    """Результат оценки товарной ниши."""
    score:      int            # итого 0–100
    verdict:    Verdict
    summary:    str            # одна фраза с главным выводом
    dimensions: list[DimensionScore] = field(default_factory=list)
    ai_insights: object = field(default=None)  # NicheInsights или None