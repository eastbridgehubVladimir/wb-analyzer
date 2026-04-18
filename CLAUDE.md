# WB SaaS — Production Architecture

AI-платформа для продавцов Wildberries: аналитика, ценообразование, рекомендации по выбору товаров.

---

## Стек

| Компонент | Технология | Назначение |
|-----------|------------|------------|
| API | FastAPI + uvicorn | HTTP-сервер, Swagger UI |
| ORM | SQLAlchemy 2.0 async | работа с PostgreSQL |
| Migrations | Alembic | версионирование схемы |
| Task queue | ARQ (Redis-backed) | фоновые задачи, cron |
| Scraper | Playwright + tenacity | парсинг WB без официального API |
| Infra | Docker Compose | локальный запуск всего стека |

---

## Слои архитектуры

```
┌─────────────────────────────────────────────────────┐
│  APPLICATION LAYER  — FastAPI (api/v1/)             │
├─────────────────────────────────────────────────────┤
│  INTELLIGENCE LAYER — AI, рекомендации              │
├─────────────────────────────────────────────────────┤
│  DECISION ENGINE    — логика выбора товаров         │  ← отсутствует
├─────────────────────────────────────────────────────┤
│  PROCESSING LAYER   — агрегации, метрики, цены      │
├─────────────────────────────────────────────────────┤
│  DATA LAYER         — парсинг, хранение сырых данных│
└─────────────────────────────────────────────────────┘
```

---

## DATA LAYER

**Статус: реализован частично**

### Источники данных

| Источник | Реализация | Статус |
|----------|------------|--------|
| Playwright-парсер страниц товаров | `services/scraper/wb_scraper.py` | ✅ реализован |
| Playwright-парсер поисковой выдачи | `wb_scraper.scrape_search()` | ✅ реализован |
| Ротация прокси | `services/scraper/proxy_rotator.py` | ✅ реализован |
| Официальный WB Suppliers API | ключ в `core/config.py` (wb_api_key) | ❌ клиент не написан |

### Фоновые задачи (ARQ + Redis)

`workers/main.py` — два воркера:
- `task_scrape_product(wb_sku)` — парсит один товар
- `task_scrape_search(keyword, pages)` — собирает SKU из поиска и ставит их в очередь

Cron: каждый час запускает `task_scrape_search("смартфон")`.

**Не реализовано:** сохранение результатов парсинга в БД (в `task_scrape_product` данные логируются, но не пишутся в PostgreSQL/ClickHouse).

---

## Хранилища данных

### PostgreSQL — оперативные данные

`infra/postgres/init.sql`, модели: `models/pg/product.py`

| Таблица | Что хранит |
|---------|------------|
| `products` | Каталог товаров (wb_sku, название, бренд, категория, атрибуты, изображения) |
| `price_history` | История цен: цена, цена по карте WB, скидка — каждая запись = снимок в момент парсинга |
| `stock_levels` | Остатки по складам WB |
| `competitors` | Пары «наш товар → товар конкурента» + коэффициент схожести |
| `recommendations` | Результаты AI-анализа (тип, тело в JSONB, уверенность, статус применения) |
| `scrape_jobs` | Очередь задач парсера с трекингом статуса и ошибок |

**Принцип:** PostgreSQL хранит сущности (что у нас есть) и операционные состояния (что происходит прямо сейчас).

### ClickHouse — аналитические данные

`infra/clickhouse/init.sql`, база `wb_analytics`

| Таблица | Что хранит | Engine |
|---------|------------|--------|
| `product_events` | Сырые события: просмотр, добавление в корзину, заказ, возврат | MergeTree, TTL 2 года |
| `competitor_prices` | Снимки цен конкурентов (каждые N часов) | MergeTree |
| `search_queries` | Поисковые запросы: ключевое слово, позиция нашего товара, CTR | MergeTree |
| `daily_product_metrics` | Агрегированные дневные метрики (views, orders, revenue, conversion) | SummingMergeTree |

Materialized View `mv_daily_metrics` автоматически агрегирует `product_events` → `daily_product_metrics`.

**Принцип:** ClickHouse хранит immutable-события (что случилось) и готовые агрегаты для дашбордов.

**Не реализовано:** `models/ch/` — пустой пакет, Python-моделей для ClickHouse нет.

---

## PROCESSING LAYER

**Статус: реализован частично**

### Аналитика спроса

`services/analytics/demand_analyzer.py` → `DemandAnalyzer`

- `get_daily_metrics(wb_sku, days)` — читает `daily_product_metrics` из ClickHouse, возвращает views/orders/revenue/conversion за N дней
- `get_trending_skus(category, limit)` — топ товаров по заказам за 7 дней в категории

### Оптимизация цен

`services/pricing/price_optimizer.py` → `PriceOptimizer`

Правило: целевая цена = `max(median_competitors * 0.97, cost_price * 1.15)`.
Читает квантили цен конкурентов из `wb_analytics.competitor_prices`.

**Не реализовано:** ML-модели прогнозирования спроса, сезонности, эластичности цены.

---

## INTELLIGENCE LAYER

**Статус: не реализован**

`services/recommendations/` — пустой пакет.

Планируемые компоненты:
- LLM-анализ листинга (заголовок, описание, фото)
- Предсказание конверсии
- Генерация текстовых рекомендаций
- Оценка потенциала ниши

В requirements.txt нет AI-библиотек (anthropic, openai и др.) — слой не начат.

Место хранения результатов готово: таблица `recommendations` в PostgreSQL (type, body JSONB, confidence, status).

---

## DECISION ENGINE

**Статус: не реализован, слой отсутствует**

Должен отвечать на вопрос: **какие товары стоит добавить в ассортимент?**

Планируемое место: `services/decision_engine/`

Ответственность:
- Скоринг ниш по совокупности сигналов (спрос, конкуренция, маржа, тренд)
- Ранжирование кандидатов для входа в нишу
- Пороговые правила (минимальный объём рынка, максимальная конкуренция)
- Выходной артефакт → запись в `recommendations` с `type='product'`

Входные данные для движка:
- `daily_product_metrics` (объём ниши)
- `competitor_prices` (уровень цен и плотность конкуренции)
- `search_queries` (частота и тренд спроса)
- `competitors` (схожесть с существующим ассортиментом)

---

## APPLICATION LAYER

**Статус: реализован**

`api/v1/` — три роутера, все под префиксом `/api/v1`:

| Роутер | Эндпоинты | Хранилище |
|--------|-----------|-----------|
| `products.py` | `GET /products/`, `GET /products/{id}`, `POST /products/` | PostgreSQL |
| `analytics.py` | `GET /analytics/demand/{wb_sku}`, `GET /analytics/trending` | ClickHouse |
| `pricing.py` | `GET /pricing/recommend/{wb_sku}` | ClickHouse |

Связующая инфраструктура:
- `core/database.py` — пулы соединений PostgreSQL, ClickHouse, Redis
- `core/deps.py` — FastAPI dependency injection
- `core/config.py` — настройки через pydantic-settings / .env
- Redis — кэш + транспорт для ARQ очереди

**Не реализовано:** аутентификация/авторизация, rate limiting, эндпоинты для recommendations и decision_engine.

---

## Поток данных (pipeline)

```
WB (страницы) ──► wb_scraper ──► [workers/ARQ] ──► PostgreSQL
                                                         │
                                              price_history, stock_levels
                                                         │
                                                    ClickHouse
                                              product_events (сырые)
                                                         │
                                              mv_daily_metrics (MV)
                                                         │
                                        ┌────────────────┴────────────────┐
                                        ▼                                 ▼
                               demand_analyzer                    price_optimizer
                               (trending, metrics)            (competitor quantiles)
                                        │                                 │
                                        └────────────────┬────────────────┘
                                                         ▼
                                                  [INTELLIGENCE]      ← не реализован
                                                  [DECISION ENGINE]   ← не реализован
                                                         │
                                              recommendations (PG)
                                                         │
                                                    FastAPI API
```

---

## Что реализовано vs что нет

### ✅ Реализовано
- Полная схема PostgreSQL (6 таблиц + индексы + триггеры)
- Полная схема ClickHouse (4 таблицы + materialized view)
- Playwright-парсер товаров и поиска с прокси-ротацией
- ARQ worker с cron-расписанием
- FastAPI: products CRUD, analytics, pricing
- DemandAnalyzer: метрики из ClickHouse
- PriceOptimizer: rule-based рекомендация цены
- Docker Compose с health checks (postgres, clickhouse, redis, api, worker)
- Pydantic-схемы для всех публичных типов

### ❌ Не реализовано
- Сохранение результатов парсинга в БД (воркер парсит, но не пишет)
- WB Suppliers API клиент (есть только ключ в конфиге)
- Intelligence Layer: LLM-интеграция, рекомендации по листингу
- Decision Engine: скоринг ниш, выбор товаров
- Python-модели для ClickHouse (`models/ch/` пуст)
- Alembic-миграции (`migrations/` пуст, только `__init__.py`)
- Аутентификация API
- Эндпоинты для recommendations
