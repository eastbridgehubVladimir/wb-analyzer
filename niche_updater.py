"""
On-demand обновление данных ниши из MPStats.

Вызывается фоново когда пользователь запрашивает нишу с данными
старше STALE_DAYS дней. Делает 1 API call к /subjects/select,
обновляет только нужную нишу в БД.

Интеграция:
  - telegram_bot.py: asyncio.create_task(refresh_niche_if_stale(name))
  - app.py:          threading.Thread(target=sync_refresh_niche, ...).start()
"""
import os
import time
import logging
import requests
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta, date
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

logger = logging.getLogger(__name__)

# ── Circuit breaker ───────────────────────────────────────────────────────────

_mpstats_backoff_until: float = 0

def is_mpstats_available() -> bool:
    return time.time() > _mpstats_backoff_until

def set_mpstats_backoff(seconds: int = 3600) -> None:
    global _mpstats_backoff_until
    _mpstats_backoff_until = time.time() + seconds
    logger.warning(f'[MPStats] Circuit breaker активирован на {seconds}с')


STALE_DAYS = 14
_DB  = os.getenv('DATABASE_URL')
_TOK = os.getenv('MPSTATS_TOKEN')
_HEADERS = {'X-Mpstats-TOKEN': _TOK, 'Content-Type': 'application/json'}


# ── Утилиты ───────────────────────────────────────────────────────────────────

def is_niche_stale(data_updated_at) -> bool:
    if data_updated_at is None:
        return True
    if isinstance(data_updated_at, str):
        data_updated_at = datetime.fromisoformat(data_updated_at)
    return datetime.now() - data_updated_at > timedelta(days=STALE_DAYS)


def _get_updated_at(niche_name: str):
    """Возвращает data_updated_at из БД для первой подходящей ниши."""
    conn = psycopg2.connect(_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT data_updated_at FROM niches WHERE LOWER(name) ILIKE LOWER(%s) LIMIT 1",
            (f'%{niche_name}%',)
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _log_refresh(niche_name: str, triggered_by: str, was_stale: bool, success: bool):
    try:
        conn = psycopg2.connect(_DB)
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO niche_refresh_log
               (niche_name, triggered_by, was_stale, success)
               VALUES (%s, %s, %s, %s)""",
            (niche_name, triggered_by, was_stale, success)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"niche_refresh_log write error: {e}")


# ── Ядро обновления ───────────────────────────────────────────────────────────

def _fetch_and_update_niche(niche_name: str) -> bool:
    """
    1 вызов к /subjects/select → находит нужную нишу → обновляет в БД.
    Возвращает True при успехе.
    """
    if not _TOK:
        logger.warning("MPSTATS_TOKEN не задан, обновление пропущено")
        return False

    try:
        r = requests.get(
            'https://mpstats.io/api/wb/get/subjects/select',
            headers=_HEADERS,
            params={'fbs': 1},
            json={'startRow': 0, 'endRow': 9999, 'filterModel': {}, 'sortModel': []},
            timeout=60,
        )
        if r.status_code == 429:
            logger.error("MPStats 429 Too Many Requests")
            set_mpstats_backoff(3600)
            return False
        if r.status_code != 200:
            logger.error(f"MPStats HTTP {r.status_code}")
            return False
    except Exception as e:
        logger.error(f"MPStats request error: {e}")
        return False

    data = r.json()
    niche_lower = niche_name.lower()

    # Ищем точное или частичное совпадение
    match = None
    for row in data:
        name = row.get('name', '')
        if name.lower() == niche_lower:
            match = row
            break
    if match is None:
        for row in data:
            name = row.get('name', '')
            if niche_lower in name.lower() or name.lower() in niche_lower:
                match = row
                break

    if match is None:
        logger.warning(f"Ниша '{niche_name}' не найдена в ответе MPStats")
        return False

    now = datetime.utcnow()
    today = date.today()

    conn = psycopg2.connect(_DB)
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE niches SET
                revenue             = %s,
                revenue_with_buyout = %s,
                orders              = %s,
                avg_price           = %s,
                buyout_pct          = %s,
                turnover            = %s,
                sellers_with_sales  = %s,
                data_updated_at     = %s
            WHERE LOWER(name) ILIKE LOWER(%s)
        """, (
            float(match.get('revenue', 0) or 0),
            float(match.get('revenue_purchase', 0) or 0),
            int(match.get('sales', 0) or 0),
            float(match.get('final_price_median', 0) or 0),
            float(match.get('purchase', 0) or 0) / 100,
            float(match.get('turnover_days', 0) or 0),
            int(match.get('suppliers_with_sells', 0) or 0),
            now,
            f'%{niche_name}%',
        ))

        # Снимок в niche_history
        cur.execute("""
            INSERT INTO niche_history
                (niche_name, snapshot_date, revenue, sales, orders,
                 avg_price, buyout_pct, turnover, sellers_with_sales)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (niche_name, snapshot_date) DO UPDATE SET
                revenue            = EXCLUDED.revenue,
                sales              = EXCLUDED.sales,
                orders             = EXCLUDED.orders,
                avg_price          = EXCLUDED.avg_price,
                buyout_pct         = EXCLUDED.buyout_pct,
                turnover           = EXCLUDED.turnover,
                sellers_with_sales = EXCLUDED.sellers_with_sales
        """, (
            match.get('name'),
            today,
            float(match.get('revenue', 0) or 0),
            int(match.get('sales', 0) or 0),
            int(match.get('sales', 0) or 0),
            float(match.get('final_price_median', 0) or 0),
            float(match.get('purchase', 0) or 0) / 100,
            float(match.get('turnover_days', 0) or 0),
            int(match.get('suppliers_with_sells', 0) or 0),
        ))

        conn.commit()
        logger.info(f"✓ Ниша '{niche_name}' обновлена (data_updated_at={now:%Y-%m-%d})")
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"DB update error for '{niche_name}': {e}")
        return False
    finally:
        conn.close()


# ── Публичный API ─────────────────────────────────────────────────────────────

async def refresh_niche_if_stale(niche_name: str, triggered_by: str = 'on_demand') -> bool:
    """
    Async-обёртка для telegram_bot.py.
    asyncio.create_task(refresh_niche_if_stale(name))
    """
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, sync_refresh_niche, niche_name, triggered_by)


def sync_refresh_niche(niche_name: str, triggered_by: str = 'on_demand') -> bool:
    """
    Sync-версия для threading.Thread в app.py.
    threading.Thread(target=sync_refresh_niche, args=(name,), daemon=True).start()
    """
    if not is_mpstats_available():
        logger.info('[MPStats] Circuit breaker активен — пропускаем обновление')
        return False
    updated_at = _get_updated_at(niche_name)
    stale = is_niche_stale(updated_at)

    if not stale:
        logger.info(f"Ниша '{niche_name}' свежая (updated {updated_at}), пропуск")
        _log_refresh(niche_name, triggered_by, was_stale=False, success=False)
        return False

    logger.info(f"Ниша '{niche_name}' устарела (updated {updated_at}), обновляем...")
    success = _fetch_and_update_niche(niche_name)
    _log_refresh(niche_name, triggered_by, was_stale=True, success=success)
    return success
