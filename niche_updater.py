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
#
# Локальный таймер (_mpstats_backoff_until) — быстрая проверка без похода в БД,
# действует только внутри ЭТОГО процесса. Но app.py, telegram_bot.py и cron —
# три разных процесса на Railway с разной памятью, поэтому локального таймера
# недостаточно: если квота кончилась через один процесс, другие об этом не
# узнают и продолжат долбить MPStats. Поэтому факт "квота исчерпана СЕГОДНЯ"
# дополнительно пишется в Postgres (mpstats_quota_status) и проверяется там
# всеми процессами — это и есть общий флаг.

_mpstats_backoff_until: float = 0


def _ensure_quota_table(conn) -> None:
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mpstats_quota_status (
            quota_date DATE PRIMARY KEY,
            exhausted_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()


def _quota_exhausted_today_in_db() -> bool:
    try:
        conn = psycopg2.connect(_DB)
        _ensure_quota_table(conn)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM mpstats_quota_status WHERE quota_date = CURRENT_DATE")
        exhausted = cur.fetchone() is not None
        cur.close()
        conn.close()
        return exhausted
    except Exception as e:
        logger.error(f'[MPStats] проверка общего флага квоты не удалась: {e}')
        return False  # сбой самой проверки не должен блокировать работу


def _mark_quota_exhausted_in_db() -> None:
    try:
        conn = psycopg2.connect(_DB)
        _ensure_quota_table(conn)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO mpstats_quota_status (quota_date) VALUES (CURRENT_DATE) "
            "ON CONFLICT (quota_date) DO NOTHING"
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f'[MPStats] запись общего флага квоты не удалась: {e}')


def is_mpstats_available() -> bool:
    if time.time() <= _mpstats_backoff_until:
        return False
    return not _quota_exhausted_today_in_db()

def set_mpstats_backoff(seconds: int = 3600) -> None:
    global _mpstats_backoff_until
    _mpstats_backoff_until = time.time() + seconds
    logger.warning(f'[MPStats] Circuit breaker активирован на {seconds}с (локально + общий флаг на сегодня в Postgres)')
    _mark_quota_exhausted_in_db()


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

# MPStats отдаёт не больше 500 записей за вызов (тот же документированный
# лимит, что и в update_niches.py). На точечный поиск одной ниши это не
# должно требовать всех 7533 строк — сначала пробуем серверный текстовый
# фильтр по имени (ag-grid-стиль filterModel, как описано в документации
# MPStats). Это НЕ проверено живым запросом на момент написания — MPStats
# был недоступен для теста (квота+блокировка QRATOR в этот день), поэтому
# результат фильтра проверяется, а не считается надёжным по умолчанию:
# если совпадения нет — запасной вариант ниже, постраничный перебор.
BATCH_SIZE = 500
BATCH_PAUSE_SECONDS = 2.5
# Защита от того, чтобы один поиск одной ниши не съел квоту на полный
# перебор всего каталога, если серверный фильтр не сработал.
MAX_FALLBACK_PAGES = 5


def _match_row(data: list, niche_name: str):
    niche_lower = niche_name.lower()
    for row in data:
        if row.get('name', '').lower() == niche_lower:
            return row
    for row in data:
        name = row.get('name', '')
        if niche_lower in name.lower() or name.lower() in niche_lower:
            return row
    return None


def _request_subjects(start_row: int, end_row: int, filter_model: dict | None = None):
    return requests.get(
        'https://mpstats.io/api/wb/get/subjects/select',
        headers=_HEADERS,
        params={'fbs': 1},
        json={'startRow': start_row, 'endRow': end_row, 'filterModel': filter_model or {}, 'sortModel': []},
        timeout=60,
    )


def _fetch_and_update_niche(niche_name: str) -> bool:
    """
    Находит нужную нишу у MPStats и обновляет её в БД. Возвращает True при успехе.

    1) Точечный запрос с текстовым фильтром по имени (1 вызов, <=500 записей).
    2) Если фильтр не дал совпадения — постраничный перебор каталога
       (<=500/запрос, пауза между страницами), но не больше MAX_FALLBACK_PAGES
       страниц, чтобы один поиск не выжигал квоту на весь день.
    На HTTP 429 на любом шаге — останавливаемся сразу и помечаем общую
    квоту как исчерпанную на сегодня (см. set_mpstats_backoff).
    """
    if not _TOK:
        logger.warning("MPSTATS_TOKEN не задан, обновление пропущено")
        return False

    match = None

    try:
        r = _request_subjects(0, BATCH_SIZE - 1, {
            'name': {'filterType': 'text', 'type': 'contains', 'filter': niche_name}
        })
        if r.status_code == 429:
            logger.error("MPStats 429 Too Many Requests (точечный фильтр по имени)")
            set_mpstats_backoff(3600)
            return False
        if r.status_code == 200:
            match = _match_row(r.json(), niche_name)
        else:
            logger.warning(f"MPStats HTTP {r.status_code} на фильтре по имени, пробуем перебор")
    except Exception as e:
        logger.error(f"MPStats filtered request error: {e}")

    if match is None:
        start_row = 0
        for page_num in range(1, MAX_FALLBACK_PAGES + 1):
            try:
                r = _request_subjects(start_row, start_row + BATCH_SIZE - 1)
            except Exception as e:
                logger.error(f"MPStats request error (страница {page_num}): {e}")
                return False

            if r.status_code == 429:
                logger.error(f"MPStats 429 Too Many Requests (перебор, страница {page_num}, startRow={start_row})")
                set_mpstats_backoff(3600)
                return False
            if r.status_code != 200:
                logger.error(f"MPStats HTTP {r.status_code} (перебор, страница {page_num})")
                return False

            page = r.json()
            match = _match_row(page, niche_name)
            if match is not None:
                break
            if len(page) < BATCH_SIZE:
                break  # конец каталога, ниши там нет

            start_row += BATCH_SIZE
            if page_num < MAX_FALLBACK_PAGES:
                time.sleep(BATCH_PAUSE_SECONDS)
        else:
            logger.warning(f"Ниша '{niche_name}' не найдена за {MAX_FALLBACK_PAGES} страниц перебора (лимит защиты от перерасхода)")

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
