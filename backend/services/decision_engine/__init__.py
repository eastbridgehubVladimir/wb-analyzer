from services.decision_engine.base import (
    DimensionScore,
    OpportunityResult,
    ProductOpportunityInput,
    Verdict,
)
from services.decision_engine.engine import evaluate_product_opportunity

__all__ = [
    "evaluate_product_opportunity",
    "ProductOpportunityInput",
    "OpportunityResult",
    "DimensionScore",
    "Verdict",
]
