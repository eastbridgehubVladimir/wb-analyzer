"""
Клиент для MPStats API.
Получает данные о нишах WB в реальном времени.
"""
import requests
import logging
from core.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://mpstats.io/api"

def get_headers():
    return {
        "X-Mpstats-TOKEN": settings.mpstats_token,
        "Content-Type": "application/json"
    }

def get_niches(start_row=0, end_row=100, sort_by="revenue"):
    """
    Получает список ниш WB с метриками.
    """
    try:
        response = requests.post(
            f"{BASE_URL}/wb/get/category",
            headers=get_headers(),
            json={
                "startRow": start_row,
                "endRow": end_row,
                "filterModel": {},
                "sortModel": [{"colId": sort_by, "sort": "desc"}]
            },
            timeout=30
        )
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 401:
            logger.error("MPStats: ошибка авторизации — проверь токен")
            return None
        elif response.status_code == 429:
            logger.warning("MPStats: превышен лимит запросов")
            return None
        else:
            logger.error("MPStats: ошибка %d", response.status_code)
            return None
    except Exception as e:
        logger.error("MPStats: ошибка соединения: %s", e)
        return None

def get_niche_details(path, d1, d2):
    """
    Получает детальные данные по конкретной нише включая динамику.
    path — путь категории например 'Одежда/Платья'
    d1, d2 — период например '2026-03-01', '2026-03-31'
    """
    try:
        response = requests.post(
            f"{BASE_URL}/wb/get/category",
            params={"d1": d1, "d2": d2, "path": path},
            headers=get_headers(),
            json={
                "startRow": 0,
                "endRow": 100,
                "filterModel": {},
                "sortModel": [{"colId": "revenue", "sort": "desc"}]
            },
            timeout=30
        )
        if response.status_code == 200:
            return response.json()
        else:
            logger.error("MPStats details: ошибка %d", response.status_code)
            return None
    except Exception as e:
        logger.error("MPStats details: %s", e)
        return None