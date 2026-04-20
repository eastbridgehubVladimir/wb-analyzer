"""
Логгер решений платформы.
Записывает каждую рекомендацию в таблицу product_decisions.
"""
import logging
import psycopg2
from datetime import datetime
from core.config import settings

logger = logging.getLogger(__name__)


def log_decision(
    category: str,
    score: int,
    verdict: str,
    monthly_revenue: float = 0,
    avg_orders_per_day: float = 0,
    active_sellers: int = 0,
    competition_level: str = "",
    median_price: float = 0,
    ai_analysis: str = "",
) -> bool:
    """
    Записывает решение платформы в PostgreSQL.
    Возвращает True если успешно, False если ошибка.
    """
    try:
        conn = psycopg2.connect(settings.database_url.replace("+asyncpg", ""))
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO product_decisions (
                category, score, verdict,
                monthly_revenue, avg_orders_per_day,
                active_sellers, competition_level,
                median_price, ai_analysis
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            category, score, verdict,
            monthly_revenue, avg_orders_per_day,
            active_sellers, competition_level,
            median_price, ai_analysis,
        ))

        conn.commit()
        cursor.close()
        conn.close()

        logger.info("Решение записано: %s → %s (score=%d)", category, verdict, score)
        return True

    except Exception as exc:
        logger.error("Ошибка записи решения: %s", exc)
        return False