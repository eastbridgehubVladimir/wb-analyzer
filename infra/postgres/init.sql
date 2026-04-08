-- ============================================================
-- СХЕМА PostgreSQL — оперативные данные WB-SaaS
-- ============================================================

-- Расширения
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- для поиска по тексту

-- ============================================================
-- ТОВАРЫ
-- ============================================================

CREATE TABLE products (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    wb_sku      BIGINT UNIQUE NOT NULL,          -- артикул WB
    seller_sku  TEXT,                             -- артикул продавца
    name        TEXT NOT NULL,
    brand       TEXT,
    category    TEXT,
    subcategory TEXT,
    description TEXT,
    images      JSONB DEFAULT '[]',               -- массив URL изображений
    attributes  JSONB DEFAULT '{}',               -- характеристики товара
    is_active   BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_products_wb_sku    ON products(wb_sku);
CREATE INDEX idx_products_category  ON products(category);
CREATE INDEX idx_products_name_trgm ON products USING GIN (name gin_trgm_ops);

-- ============================================================
-- ЦЕНЫ (история изменений)
-- ============================================================

CREATE TABLE price_history (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    product_id      UUID REFERENCES products(id) ON DELETE CASCADE,
    wb_sku          BIGINT NOT NULL,
    price           NUMERIC(12,2) NOT NULL,       -- текущая цена
    price_with_card NUMERIC(12,2),                -- цена по карте WB
    old_price       NUMERIC(12,2),                -- зачёркнутая цена
    discount_pct    SMALLINT DEFAULT 0,
    recorded_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_price_history_sku  ON price_history(wb_sku);
CREATE INDEX idx_price_history_time ON price_history(recorded_at DESC);

-- ============================================================
-- ОСТАТКИ И СКЛАДЫ
-- ============================================================

CREATE TABLE stock_levels (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    product_id  UUID REFERENCES products(id) ON DELETE CASCADE,
    wb_sku      BIGINT NOT NULL,
    warehouse   TEXT NOT NULL,                    -- название склада WB
    quantity    INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_stock_sku_warehouse ON stock_levels(wb_sku, warehouse);

-- ============================================================
-- КОНКУРЕНТЫ
-- ============================================================

CREATE TABLE competitors (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    wb_sku      BIGINT NOT NULL,                  -- наш товар
    rival_sku   BIGINT NOT NULL,                  -- товар конкурента
    rival_name  TEXT,
    rival_brand TEXT,
    similarity  NUMERIC(4,2),                     -- схожесть 0..1
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_competitors_sku ON competitors(wb_sku);

-- ============================================================
-- РЕКОМЕНДАЦИИ (результаты AI анализа)
-- ============================================================

CREATE TABLE recommendations (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    type            TEXT NOT NULL,                -- 'pricing' | 'listing' | 'product' | 'ad'
    product_id      UUID REFERENCES products(id),
    title           TEXT NOT NULL,
    body            JSONB NOT NULL,               -- структурированная рекомендация
    confidence      NUMERIC(4,2),                 -- уверенность модели 0..1
    status          TEXT DEFAULT 'pending',       -- pending | applied | dismissed
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    applied_at      TIMESTAMPTZ
);

CREATE INDEX idx_recommendations_type   ON recommendations(type);
CREATE INDEX idx_recommendations_status ON recommendations(status);

-- ============================================================
-- ЗАДАЧИ ПАРСЕРА (очередь)
-- ============================================================

CREATE TABLE scrape_jobs (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_type    TEXT NOT NULL,                    -- 'product' | 'search' | 'category'
    payload     JSONB NOT NULL,
    status      TEXT DEFAULT 'pending',           -- pending | running | done | failed
    attempts    SMALLINT DEFAULT 0,
    error       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE INDEX idx_scrape_jobs_status ON scrape_jobs(status, created_at);

-- ============================================================
-- Автообновление updated_at
-- ============================================================

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_products_updated_at
    BEFORE UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
