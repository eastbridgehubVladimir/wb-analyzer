from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.deps import pg_session
from models.pg.product import Product
from schemas.product import ProductCreate, ProductOut

router = APIRouter(prefix="/products", tags=["products"])


@router.get("/", response_model=list[ProductOut])
async def list_products(
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(pg_session),
):
    """Список всех товаров с фильтром по категории."""
    q = select(Product).where(Product.is_active == True).limit(limit).offset(offset)
    if category:
        q = q.where(Product.category == category)
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/{product_id}", response_model=ProductOut)
async def get_product(product_id: UUID, db: AsyncSession = Depends(pg_session)):
    product = await db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")
    return product


@router.post("/", response_model=ProductOut, status_code=201)
async def create_product(payload: ProductCreate, db: AsyncSession = Depends(pg_session)):
    product = Product(**payload.model_dump())
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return product
