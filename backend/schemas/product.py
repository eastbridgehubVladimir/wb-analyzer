from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ProductBase(BaseModel):
    wb_sku: int
    name: str
    brand: str | None = None
    category: str | None = None


class ProductCreate(ProductBase):
    seller_sku: str | None = None
    description: str | None = None
    attributes: dict = Field(default_factory=dict)


class ProductOut(ProductBase):
    id: UUID
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class PriceHistoryOut(BaseModel):
    wb_sku: int
    price: float
    price_with_card: float | None
    discount_pct: int
    recorded_at: datetime

    model_config = {"from_attributes": True}


class RecommendationOut(BaseModel):
    id: UUID
    type: str
    title: str
    body: dict
    confidence: float | None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
