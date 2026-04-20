"""
Ротация прокси: случайный выбор из списка.
Если прокси "умер" — помечается как нерабочий на N минут.
"""
import random
import time
from dataclasses import dataclass, field

from core.config import settings


@dataclass
class ProxyEntry:
    url: str
    failed_until: float = 0.0  # unix timestamp, до которого прокси не использовать

    @property
    def is_available(self) -> bool:
        return time.time() > self.failed_until


class ProxyRotator:
    def __init__(self, cooldown_seconds: int = 300):
        self._cooldown = cooldown_seconds
        self._proxies: list[ProxyEntry] = [
            ProxyEntry(url=url) for url in settings.proxies
        ]

    def get(self) -> str | None:
        """Вернуть случайный рабочий прокси или None (без прокси)."""
        available = [p for p in self._proxies if p.is_available]
        if not available:
            return None
        return random.choice(available).url

    def mark_failed(self, proxy_url: str):
        """Отключить прокси на время cooldown."""
        for p in self._proxies:
            if p.url == proxy_url:
                p.failed_until = time.time() + self._cooldown
                break


proxy_rotator = ProxyRotator()
