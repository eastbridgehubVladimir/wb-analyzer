-- ============================================================
-- СХЕМА ClickHouse — аналитические данные WB-SaaS
-- ClickHouse хранит миллионы событий и делает агрегации за секунды
-- ============================================================

CREATE DATABASE IF NOT EXISTS wb_analytics;

-- ============================================================
-- СОБЫТИЯ ТОВАРОВ: просмотры, продажи, позиции в поиске
-- Одна строка = одно событие (immutable log)
-- ============================================================

CREATE TABLE IF NOT EXISTS wb_analytics.product_events (
    event_date      Date,
    event_time      DateTime,
    wb_sku          UInt64,
    event_type      LowCardinality(String),   -- 'view' | 'cart' | 'order' | 'return'
    category        LowCardinality(String),
    search_position UInt16 DEFAULT 0,          -- позиция в выдаче
    price           Decimal(12,2),
    quantity        UInt32 DEFAULT 1,
    revenue         Decimal(14,2) DEFAULT 0,
    source          LowCardinality(String)    -- 'organic' | 'ad' | 'promo'
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(event_date)
ORDER BY (event_date, wb_sku, event_type)
TTL event_date + INTERVAL 2 YEAR;

-- ============================================================
-- СНИМКИ ЦЕН КОНКУРЕНТОВ (каждые N часов)
-- ============================================================

CREATE TABLE IF NOT EXISTS wb_analytics.competitor_prices (
    snapshot_date   Date,
    snapshot_time   DateTime,
    our_sku         UInt64,
    rival_sku       UInt64,
    rival_price     Decimal(12,2),
    rival_rating    Float32 DEFAULT 0,
    rival_reviews   UInt32 DEFAULT 0,
    rival_position  UInt16 DEFAULT 0
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(snapshot_date)
ORDER BY (snapshot_date, our_sku, rival_sku);

-- ============================================================
-- ПОИСКОВЫЕ ЗАПРОСЫ: частота и позиция нашего товара
-- ============================================================

CREATE TABLE IF NOT EXISTS wb_analytics.search_queries (
    query_date      Date,
    keyword         String,
    wb_sku          UInt64,
    frequency       UInt32,                   -- сколько раз искали
    position        UInt16,                   -- наша позиция
    ctr             Float32 DEFAULT 0         -- click-through rate
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(query_date)
ORDER BY (query_date, keyword, wb_sku);

-- ============================================================
-- ДНЕВНАЯ АГРЕГАЦИЯ — готовые метрики для дашборда
-- (Materialized View обновляется автоматически при вставке)
-- ============================================================

CREATE TABLE IF NOT EXISTS wb_analytics.daily_product_metrics (
    metric_date     Date,
    wb_sku          UInt64,
    category        LowCardinality(String),
    views           UInt32 DEFAULT 0,
    cart_adds       UInt32 DEFAULT 0,
    orders          UInt32 DEFAULT 0,
    returns         UInt32 DEFAULT 0,
    revenue         Decimal(14,2) DEFAULT 0,
    avg_price       Decimal(12,2) DEFAULT 0,
    conversion_rate Float32 DEFAULT 0         -- orders / views
)
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(metric_date)
ORDER BY (metric_date, wb_sku);

-- Автоматически агрегируем события в daily_product_metrics
CREATE MATERIALIZED VIEW IF NOT EXISTS wb_analytics.mv_daily_metrics
TO wb_analytics.daily_product_metrics AS
SELECT
    toDate(event_time)          AS metric_date,
    wb_sku,
    category,
    countIf(event_type = 'view')  AS views,
    countIf(event_type = 'cart')  AS cart_adds,
    countIf(event_type = 'order') AS orders,
    countIf(event_type = 'return') AS returns,
    sumIf(revenue, event_type = 'order') AS revenue,
    avgIf(price, event_type = 'order')   AS avg_price,
    0 AS conversion_rate
FROM wb_analytics.product_events
GROUP BY metric_date, wb_sku, category;
