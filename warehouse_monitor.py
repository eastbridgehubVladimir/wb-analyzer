"""
warehouse_monitor.py — Агент v1: мониторинг статуса складов WB.

Раз в 4 часа (Railway cron) ищет свежие новости об атаках/закрытии складов WB
и конкурентов (Ozon, Яндекс Маркет, СДЭК, Почта России) через Firecrawl,
передаёт найденные тексты в Claude API для извлечения структурированных
данных, обновляет таблицу warehouse_status (только для WB — по конкурентам
своей таблицы пока нет, только лог + уведомление), логирует каждый запуск
в warehouse_monitor_log и уведомляет TELEGRAM_ADMIN_ID.

Источники и уровни доверия (обновлено 31.07.2026 после ложного срабатывания
по YouTube-шортсу и агрегатору dw.com — см. SOURCE_TIERS в коде):
  1 — Минобороны Украины (defence.ua, t.me/DefenceU)
  2 — украинские новостные агентства (suspilne.media, pravda.com.ua,
      radiosvoboda.org, unian.net, hromadske.ua)
  3 — официальный Telegram-канал WB
  4 — российские источники (kommersant.ru, rbc.ru) — с задержкой, для подтверждения
Правило подтверждения (см. _confidence): уровень 1/2 подтверждает факт сам
по себе; уровень 3/4 требует минимум двух разных источников; иначе находка
помечается 'uncertain' и в БД НЕ пишется — только логируется для ручной
проверки (см. предыдущий инцидент с Коледино/Екатеринбургом).

Это агент v1 — минимальная рабочая версия одного из шести агентов
запланированной агентской сети (см. MASTER_PLAN / контекст сессии 31.07.2026).

Переменные окружения: DATABASE_URL, ANTHROPIC_API_KEY, FIRECRAWL_API_KEY,
TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID.
Без FIRECRAWL_API_KEY скрипт не падает, но и не находит новых данных —
см. предупреждение в логах.

Запуск вручную: python3 warehouse_monitor.py
Railway cron:   каждые 4 часа, команда: python3 warehouse_monitor.py

Сервис trustworthy-presence на Railway дважды падал с ModuleNotFoundError,
несмотря на requirements-monitor.txt и отдельный railway-monitor.json —
build-конфиг второго сервиса из общего репозитория почему-то не подхватывался.
Поэтому скрипт сам проверяет и при необходимости ставит зависимости при
старте (см. _ensure_deps ниже) — это не зависит от того, как Railway
собрал образ, и гарантированно работает при любом build-конфиге.
"""
import os
import sys
import subprocess


def _ensure_deps():
    """Подстраховка на случай, если build-шаг Railway не поставил зависимости
    (ровно это происходило со службой trustworthy-presence уже несколько раз).
    Если пакет уже есть — pip install не запускается, лишнего времени на прогон
    не тратится.

    psycopg2 ставится ОТДЕЛЬНО, через --only-binary=:all: — без этого флага pip
    в runtime-окружении Railway (там нет pg_config) при отсутствии подходящего
    wheel пытается собрать psycopg2-binary из исходников и падает с
    "Failed to build psycopg2-binary from source". С флагом pip либо возьмёт
    готовый бинарный wheel, либо сразу и honestly скажет, что подходящего
    wheel нет — не будет пытаться компилировать."""
    pkgs = []
    try:
        import requests  # noqa
    except ImportError:
        pkgs.append('requests==2.31.0')
    try:
        import anthropic  # noqa
    except ImportError:
        pkgs.append('anthropic>=0.40.0')
    try:
        import dotenv  # noqa
    except ImportError:
        pkgs.append('python-dotenv==1.0.1')
    if pkgs:
        print(f'[warehouse_monitor] Отсутствуют зависимости, ставлю: {pkgs}')
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q'] + pkgs)
        import importlib as _il
        _il.invalidate_caches()
        print('[warehouse_monitor] Установка завершена')

    try:
        import psycopg2  # noqa
    except ImportError:
        print('[warehouse_monitor] psycopg2 отсутствует, ставлю psycopg2-binary (только бинарный wheel, без компиляции)')
        subprocess.check_call([
            sys.executable, '-m', 'pip', 'install', '-q',
            '--only-binary=:all:',
            'psycopg2-binary==2.9.12',
        ])
        import importlib as _il
        _il.invalidate_caches()
        print('[warehouse_monitor] psycopg2-binary установлен')


_ensure_deps()

import json
import re
import time
from datetime import datetime

import requests
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL       = os.getenv('DATABASE_URL')
ANTHROPIC_API_KEY  = os.getenv('ANTHROPIC_API_KEY')
FIRECRAWL_API_KEY  = os.getenv('FIRECRAWL_API_KEY')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_ADMIN_ID  = os.getenv('TELEGRAM_ADMIN_ID')

# Источники мониторинга и уровни доверия (переработано 10.08.2026: российские
# источники — Коммерсант, РБК — либо замалчивают атаки на склады WB, либо
# публикуют с задержкой; украинские источники публикуют быстро и точно, но
# раньше не были основными. Теперь именно украинские источники — приоритет,
# российские — вспомогательное подтверждение, недостаточное само по себе).
#
# Уровень 1 — публикация сама по себе достаточна для 'confirmed':
#   defence.ua (Минобороны Украины), mil.gov.ua (Генштаб ВСУ),
#   nv.ua (NV), suspilne.media (Суспільне), t.me/DefenceU
# Уровень 2 — 'confirmed' только если совпадают 2+ РАЗНЫХ источника уровня 2:
#   pravda.com.ua, unian.net, radiosvoboda.org, hromadske.ua,
#   novayagazeta.eu, vedomosti.ru (осторожно — тоже может замалчивать)
# Уровень 3 — только как дополнительное подтверждение, само по себе даёт
#   не более 'uncertain': rbc.ru, kommersant.ru, официальный Telegram-канал WB
# Уровень 4 — не участвует в решениях, только логируется: всё остальное
#   (неверифицированные Telegram-каналы, YouTube, соцсети)
#
# Правила — см. _confidence().
SOURCE_TIERS = {
    'defence.ua':       1,
    'mil.gov.ua':       1,
    'nv.ua':             1,
    'suspilne.media':   1,
    'pravda.com.ua':    2,
    'unian.net':        2,
    'radiosvoboda.org': 2,
    'hromadske.ua':     2,
    'novayagazeta.eu':  2,
    'vedomosti.ru':     2,
    'nexta.tv':         2,
    'news.zerkalo.io':  2,
    'rbc.ru':           3,
    'kommersant.ru':    3,
    'kp.ru':            3,
    'aif.ru':           3,
    'tass.ru':          3,
}

# Telegram (t.me) — отдельно от SOURCE_TIERS: обычный веб-поиск Firecrawl плохо
# индексирует посты конкретных каналов, поэтому t.me/DefenceU (уровень 1) и
# официальный канал WB (уровень 3) нельзя надёжно различить по URL через
# веб-поиск. Best-effort в _classify_source_tier(): путь содержит "defenceu" →
# уровень 1; любой другой t.me-путь по умолчанию считаем неверифицированным
# Telegram-каналом — уровень 4 (раньше по умолчанию считался офиц. каналом WB,
# уровень 3, но это было недоказанное допущение, а не факт — новая схема
# уровней явно требует не путать неверифицированные каналы с официальным).
# Для по-настоящему надёжного покрытия Telegram в будущем нужен Telegram Bot
# API (чтение постов конкретных каналов напрямую), а не веб-поиск.
_TELEGRAM_DOMAIN = 't.me'

_ALL_SOURCE_DOMAINS = list(SOURCE_TIERS.keys()) + [_TELEGRAM_DOMAIN]

# v1/search REST API не принимает includeDomains как поле JSON (только query:
# 'sources' и 'includeDomains' — фичи MCP-обёртки, не голого REST) — фильтр по
# домену делаем через оператор site: прямо в строке запроса.
_DOMAIN_FILTER = ' OR '.join(f'site:{d}' for d in _ALL_SOURCE_DOMAINS)

# Склады конкурентов — тоже влияют на рынок и важны пользователям WBAnalyzer.
# Инфраструктуры для хранения их статуса в БД пока нет (не было схемы в задаче) —
# находки только логируются и уходят уведомлением админу, в warehouse_status
# не пишутся.
COMPETITOR_KEYWORDS = ['Ozon', 'Яндекс Маркет', 'СДЭК', 'Почта России']

# Запросы разделены по приоритету источников (10.08.2026): украинские источники
# публикуют быстро и точно, российские — с задержкой или замалчивают. ЗАПРОС 1
# (украинский) обрабатывается первым в run(); если он уже даёт 'confirmed',
# ЗАПРОС 2 (российский) в этом прогоне не выполняется — экономия лимитов
# Firecrawl/Claude, т.к. российские источники (уровень 3) всё равно не могут
# сами по себе подтвердить находку.
_DOMAIN_FILTER_UA = ' OR '.join(f'site:{d}' for d in [
    'defence.ua', 'mil.gov.ua', 'nv.ua', 'suspilne.media',
    'pravda.com.ua', 'unian.net', 'radiosvoboda.org',
    'novayagazeta.eu', 'hromadske.ua',
    'nexta.tv', 'news.zerkalo.io',
])
_DOMAIN_FILTER_RU = ' OR '.join(f'site:{d}' for d in [
    'rbc.ru', 'kommersant.ru', 'vedomosti.ru',
    'kp.ru', 'aif.ru', 'tass.ru',
])

SEARCH_QUERY_UA = f"Wildberries склад атака удар пожар ({_DOMAIN_FILTER_UA})"
SEARCH_QUERY_RU = f"Wildberries склад атака пожар горит ({_DOMAIN_FILTER_RU})"

# Отдельный запрос для складов конкурентов — по всем источникам сразу
# (не разделяется на UA/RU приоритет, т.к. это не основной сценарий подтверждения).
COMPETITOR_SEARCH_QUERIES = [
    f"{' '.join(COMPETITOR_KEYWORDS)} склад атака пожар БПЛА ({_DOMAIN_FILTER})",
]

# ── RSS-источники (обновлено 12.08.2026) ────────────────────────────────────
# Было на rsshub.app (публичный демо-инстанс) — 12.08.2026 диагностика
# показала, что rsshub.app отдаёт 403 с Cloudflare JS-челленджем на ВСЕ
# запрошенные адреса (nexta_tv, zerkalo_io, suspilne), обычный requests.get()
# её пройти не может. Заменено на tg.i-c-a.su — при диагностике отдал 200 и
# валидный RSS с реальным контентом обоих каналов, но с заметным rate-limit
# (два запроса подряд без паузы — один из них получил 429/завис). Отсюда
# time.sleep() между вызовами ниже в run().
#
# Suspilne через RSS убран: его rsshub.app-адрес тоже был за Cloudflare,
# альтернативы под suspilne на tg.i-c-a.su не проверяли, а suspilne.media
# и так уже уровень 1 в SOURCE_TIERS и покрывается обычным Firecrawl-поиском
# (SEARCH_QUERY_UA) — без RSS-дублирования ничего не теряем.
#
# Тир для каждого фида задан здесь напрямую, а не через _classify_source_tier()
# — та классифицирует по домену источника (nv.ua, rbc.ru и т.п.), а не знает
# про tg.i-c-a.su-зеркала чужих каналов.
#
# NEXTA и Зеркало — уровень 2 (как украинские агентства: pravda.com.ua,
# unian.net), а НЕ уровень 1. Решение 12.08.2026: это уважаемые, но
# Telegram-первые каналы, не государственные/официальные источники и не
# редакции с фактчекингом уровня nv.ua/suspilne.media — а система уже дважды
# обжигалась на избыточном доверии к недостаточно проверенным источникам
# (ложный Коледино/Екатеринбург 31.07, фантомная запись "Россия (14 складов)"
# 10.08 — см. историю выше и is_valid_warehouse_name).
RSS_SOURCES = [
    ('https://tg.i-c-a.su/rss/nexta_tv', 2, 'NEXTA (Telegram)'),
    ('https://tg.i-c-a.su/rss/zerkalo_io', 2, 'Зеркало (Telegram)'),
]
RSS_KEYWORDS = ['Wildberries', 'WB', 'склад', 'warehouse', 'логистик']


def fetch_rss_source(url: str, keywords: list) -> list:
    """
    Получает RSS и возвращает тексты записей которые содержат хотя бы одно
    ключевое слово.
    """
    try:
        r = requests.get(url, timeout=15,
            headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200:
            print(f'[RSS] Ошибка {url}: HTTP {r.status_code}')
            return []
        # Простой парсинг XML без библиотек
        items = re.findall(
            r'<item>(.*?)</item>', r.text, re.DOTALL)
        results = []
        for item in items:
            # Извлечь title и description
            title = re.search(
                r'<title>(.*?)</title>', item)
            desc = re.search(
                r'<description>(.*?)</description>',
                item)
            text = ''
            if title: text += title.group(1) + ' '
            if desc: text += desc.group(1)
            # Проверить ключевые слова
            text_lower = text.lower()
            if any(kw.lower() in text_lower
                   for kw in keywords):
                # Убрать HTML теги
                clean = re.sub(r'<[^>]+>', '', text)
                results.append(clean[:500])
        return results
    except Exception as e:
        print(f'[RSS] Ошибка {url}: {e}')
        return []

VALID_STATUSES = {'active', 'limited', 'closed', 'attacked'}

# Фильтр свежести: складам за 13 дней уже досталось, старые статьи (нашлась даже
# статья 2022 года про пожар на складе Ozon) только шумят и путают агрегацию
# уровней доверия. 'qdr:w' — Google-style time-based search, "за последнюю неделю".
FRESHNESS_TBS = 'qdr:w'
FRESHNESS_MAX_DAYS = 7


def _is_recent_enough(metadata: dict, max_days: int = FRESHNESS_MAX_DAYS) -> bool:
    """Вторая линия защиты поверх tbs: если в метаданных есть ОДНА однозначная
    дата публикации/изменения — проверяем её. Если дат нет или их несколько
    (списком — как на агрегаторных страницах) — не отбрасываем результат,
    доверяем tbs: лучше пропустить погранично старую статью, чем потерять
    свежую из-за неоднозначных метаданных."""
    for key in ('publishedTime', 'article:published_time', 'modifiedTime', 'article:modified_time'):
        val = metadata.get(key)
        if isinstance(val, str) and val:
            try:
                dt = datetime.fromisoformat(val.replace('Z', '+00:00'))
                if dt.tzinfo:
                    dt = dt.replace(tzinfo=None)
                return (datetime.now() - dt).days <= max_days
            except (ValueError, TypeError):
                continue
    return True


def _firecrawl_search(query: str, limit: int = 10) -> list:
    """Ищет новости через Firecrawl REST API. Возвращает [{title, url, text}].
    limit=5 пропускал релевантные статьи ниже топ-5 (unian.net про Владимирскую
    область оказывалась 2-й при limit=10, но не входила в выдачу при limit=5) —
    увеличено 03.08.2026."""
    if not FIRECRAWL_API_KEY:
        print('[warehouse_monitor] FIRECRAWL_API_KEY не задан — поиск пропущен')
        return []
    try:
        resp = requests.post(
            'https://api.firecrawl.dev/v1/search',
            headers={'Authorization': f'Bearer {FIRECRAWL_API_KEY}'},
            json={
                'query': query,
                'limit': limit,
                'tbs': FRESHNESS_TBS,
                'scrapeOptions': {'formats': ['markdown'], 'onlyMainContent': True},
            },
            timeout=30,
        )
        resp.raise_for_status()
        # v1/search возвращает {"success": true, "data": [...]} — плоский список,
        # без вложенных ключей news/web (в отличие от параметра sources у MCP-обёртки,
        # который REST API вообще не принимает — 400 Unrecognized key "sources").
        results = resp.json().get('data') or []
        # YouTube явно исключаем: 31.07.2026 ложное срабатывание по YouTube-
        # шортсу (Коледино/Екатеринбург) — см. историю в SOURCE_TIERS выше.
        results = [r for r in results
                   if 'youtube.com' not in r.get('url', '')
                   and 'youtu.be' not in r.get('url', '')]
        out = []
        skipped_old = 0
        for r in results:
            if not _is_recent_enough(r.get('metadata') or {}):
                skipped_old += 1
                continue
            # title+description всегда ставим первыми: для "коротких" форматов
            # некоторых сайтов (напр. rbc.ru/rbcfreenews) onlyMainContent иногда
            # вытаскивает не текст статьи, а ленту других заголовков сайта —
            # markdown при этом непустой, но бесполезный, а нужный факт был
            # только в description. Из-за этого 03.08.2026 агент не заметил
            # атаку на склад во Владимирской области, хотя рабочая ссылка на
            # rbc.ru была в выдаче с самого начала.
            title = r.get('title', '')
            description = r.get('description', '')
            markdown = r.get('markdown', '')
            combined = f"{title}. {description}\n\n{markdown}".strip()
            out.append({
                'title': title,
                'url': r.get('url', ''),
                'text': combined[:4000],
            })
        if skipped_old:
            print(f'[warehouse_monitor]   отфильтровано как устаревшее (>{FRESHNESS_MAX_DAYS} дн.): {skipped_old}')
        return out
    except Exception as e:
        print(f'[warehouse_monitor] Firecrawl search error ({query!r}): {e}')
        return []


def _classify_source_tier(url: str) -> int:
    """Уровень доверия источника (1-4) по домену. 0 — домен не в списке доверенных
    (не должен был пройти site:-фильтр поиска, но проверяем ещё раз — вторая линия защиты)."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    if host.startswith('www.'):
        host = host[4:]
    if host == _TELEGRAM_DOMAIN or host.endswith('.' + _TELEGRAM_DOMAIN):
        return 1 if 'defenceu' in urlparse(url).path.lower() else 4
    for domain, tier in SOURCE_TIERS.items():
        if host == domain or host.endswith('.' + domain):
            return tier
    return 0


def _confidence(tiers: list, source_urls: list) -> str:
    """confirmed / uncertain / ignored — правила подтверждения по уровням источников
    (переработано 10.08.2026 — цель: уведомления только когда атака ПОДТВЕРЖДЕНА,
    без "возможно"/"требует проверки").
    confirmed: хотя бы один источник уровня 1, ИЛИ 2+ РАЗНЫХ источника уровня 2.
    uncertain: один источник уровня 2 или уровня 3 — логируем, НЕ уведомляем.
    ignored:   только источники уровня 4 — не участвует в решениях вообще."""
    if any(t == 1 for t in tiers):
        return 'confirmed'
    tier2_sources = {u for t, u in zip(tiers, source_urls) if t == 2}
    if len(tier2_sources) >= 2:
        return 'confirmed'
    tier23_sources = {u for t, u in zip(tiers, source_urls) if t in (2, 3)}
    if len(tier23_sources) >= 1:
        return 'uncertain'
    return 'ignored'


def _ensure_schema(conn):
    """Идемпотентно добавляет notified_warehouses в warehouse_monitor_log, если
    колонки ещё нет — самомиграция вместо отдельного .py-скрипта (задача явно
    просила менять только warehouse_monitor.py). Безопасно вызывать на каждом
    запуске: ADD COLUMN IF NOT EXISTS ничего не делает, если колонка уже есть."""
    cur = conn.cursor()
    cur.execute("ALTER TABLE warehouse_monitor_log ADD COLUMN IF NOT EXISTS notified_warehouses TEXT")
    conn.commit()
    cur.close()


def _get_recently_notified(conn, hours: int = 24) -> set:
    """Названия складов, о которых уже уведомляли за последние `hours` часов —
    чтобы не слать одно и то же уведомление каждые 4 часа из одной и той же
    старой статьи (реальная жалоба: агент дублировал уведомления)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT notified_warehouses FROM warehouse_monitor_log
        WHERE run_at > NOW() - INTERVAL '24 hours' AND notified_warehouses IS NOT NULL
    """)
    names = set()
    for (raw,) in cur.fetchall():
        try:
            names.update(json.loads(raw))
        except (TypeError, ValueError):
            continue
    cur.close()
    return names


def _get_known_warehouse_names(conn) -> list:
    """Список канонических названий складов из warehouse_status."""
    cur = conn.cursor()
    cur.execute("SELECT name FROM warehouse_status ORDER BY name")
    names = [r[0] for r in cur.fetchall()]
    cur.close()
    return names


def is_valid_warehouse_name(name: str) -> bool:
    """Отсеивает агрегированные/мусорные "названия складов" вроде "Россия
    (14 складов)" — 10.08.2026 такая фраза из статьи про совокупные потери
    WB по всей стране прошла как новый склад с этим текстом вместо города,
    попала в БД и на публичный лендинг (см. диагностику того же дня)."""
    if not name or len(name) < 3 or len(name) > 30:
        return False
    # Более 2 цифр подряд — почти наверняка не название города (номер,
    # статистика и т.п.), а не часть прошедшего проверку регэкспа ниже.
    if re.search(r'\d{3,}', name):
        return False
    # Отклонять агрегированные фразы
    aggregators = [
        'складов', 'объектов', 'регионов', 'территорий',
        'всего', 'итого', 'россия', 'ukraine', 'russia',
        'беларусь', 'казахстан'
    ]
    name_lower = name.lower()
    if any(a in name_lower for a in aggregators):
        return False
    # Отклонять если есть число + слово (типа "14 складов" или "(14 складов)")
    if re.search(r'\d+\s+\w+', name):
        return False
    return True


def _resolve_warehouse_name(candidate: str, known_names: list):
    """Сопоставляет предложенное Claude название с уже известным складом по
    регистронезависимому вхождению в обе стороны ('Koledino' / 'Wildberries
    Екатеринбург' → 'Коледино' / 'Екатеринбург' — реальный баг, найденный
    31.07.2026). Если совпадений нет — считаем это НОВЫМ складом и возвращаем
    имя как есть: раньше находки с именем не из списка просто отбрасывались,
    из-за чего 03.08.2026 агент не заметил атаку на склад во Владимирской
    области — его вообще не было в known_names, и система structurally не
    могла его обнаружить.

    Но "новым складом" не должна становиться первая попавшаяся строка —
    10.08.2026 так в БД попала "Россия (14 складов)" (см. диагностику).
    Кандидат, не совпавший ни с одним известным именем, дополнительно
    проходит is_valid_warehouse_name(); если не проходит — возвращается
    None, и вызывающий код обязан отбросить находку целиком, а не считать
    её ни известным, ни новым складом."""
    c = candidate.strip()
    cl = c.lower()
    for name in known_names:
        nl = name.lower()
        if cl == nl or nl in cl or cl in nl:
            return name
    if not is_valid_warehouse_name(c):
        print(f"[warehouse_monitor] Отклонён невалидный кандидат в новые склады: {c!r}")
        return None
    return c


def _extract_with_claude(text: str, known_names: list, source_url: str = '') -> dict:
    """Просит Claude извлечь структурированные данные об атаке/закрытии склада WB
    или конкурента. Достоверность источника (уровни 1-4) проверяется по URL в
    Python (_classify_source_tier) — единичный вызов Claude на один текст не может
    надёжно оценить "2+ независимых источника сообщают одно и то же", это решается
    агрегацией в run(). Здесь Claude отвечает только за извлечение фактов из текста
    и за отсев вторичных пересказов внутри самого текста (см. инструкцию ниже)."""
    if not ANTHROPIC_API_KEY:
        print('[warehouse_monitor] ANTHROPIC_API_KEY не задан — извлечение пропущено')
        return {'found': False}
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    names_list = ', '.join(known_names)
    competitors_list = ', '.join(COMPETITOR_KEYWORDS)
    prompt = (
        "Найди в тексте упоминания складов Wildberries, пострадавших от атак/пожаров/аварий "
        "(в том числе НОВЫХ складов, которых нет в списке ниже — это нормально, WB регулярно "
        "теряет новые склады, список известных — не исчерпывающий).\n\n"
        f"Уже известные склады Wildberries: {names_list}.\n"
        "Если склад из текста совпадает с одним из известных (тот же город/регион, просто "
        "иначе написан, например с приставкой 'Wildberries' или на латинице) — используй ЕГО "
        "название СТРОГО как в списке выше. Если это НОВЫЙ склад, которого в списке нет — "
        "верни его коротким названием ближайшего крупного города или региона (например "
        "'Владимир', а не название посёлка вроде 'Хрястово'), не пропускай его только из-за "
        "того, что его нет в списке.\n\n"
        f"Также отслеживай склады конкурентов: {competitors_list}.\n\n"
        f"ИСТОЧНИК ТЕКСТА: {source_url or 'неизвестен'}\n"
        "Если статья сама указывает, что пересказывает непроверенный вторичный источник "
        "(например, анонимный Telegram-канал без первоисточника, слухи, неподтверждённые "
        'данные) — не доверяй такому фрагменту и верни {"found": false}.\n\n'
        "ВАЖНО: если текст сам не уверен, что пострадал именно склад WB, а не соседний/другой "
        "объект (например: 'что именно горит — пока неизвестно', 'возможно горит склад "
        "соседней компании рядом с Wildberries', 'по предварительным данным') — это НЕ "
        'достаточное подтверждение для склада WB, верни {"found": false} для этого склада, '
        "даже если WB упоминается в тексте по соседству.\n\n"
        "Если найден склад WB (известный или новый) — верни JSON ОДНИМ объектом (не массивом): "
        '{"company": "WB", "warehouse": "название города/региона", '
        '"status": "attacked/closed/limited", "date": "дата", "summary": "краткое описание"}.\n'
        "Если найден склад конкурента — верни: "
        '{"company": "Ozon|Яндекс Маркет|СДЭК|Почта России", "warehouse": "название/город склада", '
        '"status": "attacked/closed/limited", "date": "дата", "summary": "краткое описание"}.\n'
        "Если ни склада WB, ни склада конкурента не упомянуто — верни "
        '{"found": false}.\n\n'
        f"ТЕКСТ:\n{text}\n\nONLY JSON, без пояснений."
    )
    try:
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = msg.content[0].text.strip().replace('```json', '').replace('```', '').strip()
        result = json.loads(raw)
        if isinstance(result, list):
            result = result[0] if result else {}
        if not isinstance(result, dict) or not result or result.get('found') is False:
            return {'found': False}
        if not str(result.get('warehouse', '')).strip():
            return {'found': False}
        # Сопоставление с известными складами (фаззи-матч на случай другого написания)
        # или регистрация нового склада — делается централизованно в run() через
        # _resolve_warehouse_name(), чтобы результат был согласован при группировке
        # находок из разных источников по одному и тому же складу.
        result['found'] = True
        result['raw_response'] = raw
        return result
    except Exception as e:
        print(f'[warehouse_monitor] Claude extract error: {e}')
        return {'found': False}


def _update_warehouse_status(conn, finding: dict) -> bool:
    """Обновляет запись в warehouse_status, либо создаёт новую, если склад
    обнаружен впервые (имя уже нормализовано через _resolve_warehouse_name() —
    см. run() — так что дубль под другим написанием того же склада исключён).
    Возвращает True, если в БД что-то реально изменилось."""
    name = str(finding.get('warehouse', '')).strip()
    status = str(finding.get('status', '')).strip().lower()
    if not name or status not in VALID_STATUSES:
        return False
    note = finding.get('summary', '')
    source_url = finding.get('source_url', '')
    cur = conn.cursor()
    cur.execute("SELECT status FROM warehouse_status WHERE name = %s", (name,))
    row = cur.fetchone()
    if row:
        if row[0] == status:
            cur.close()
            return False  # статус не изменился — обновлять нечего
        cur.execute("""
            UPDATE warehouse_status
            SET status = %s, status_note = %s, updated_at = NOW(), source_url = %s,
                attacked_at = CASE WHEN %s = 'attacked' THEN NOW() ELSE attacked_at END
            WHERE name = %s
        """, (status, note, source_url, status, name))
    else:
        # Новый склад — раньше эта ветка была убрана, чтобы не плодить дубли по
        # неточному имени (см. инцидент с 'Koledino'/'Wildberries Екатеринбург'
        # 31.07.2026). Теперь имя уже прогнано через фаззи-сопоставление в run(),
        # так что добавление новой строки безопасно, а без этой ветки агент
        # структурно не может обнаружить впервые атакованный склад — именно это
        # произошло 03.08.2026 со складом во Владимирской области.
        cur.execute("""
            INSERT INTO warehouse_status (name, status, status_note, source_url, attacked_at)
            VALUES (%s, %s, %s, %s, CASE WHEN %s = 'attacked' THEN NOW() ELSE NULL END)
        """, (name, status, note, source_url, status))
    conn.commit()
    cur.close()
    return True


# Кнопка под каждым уведомлением — ссылка на бота WBAnalyzer.
_OPEN_APP_BUTTON = {
    "inline_keyboard": [[
        {"text": "📊 Открыть WBAnalyzer", "url": "https://t.me/Wbanalyzer_user_bot"}
    ]]
}

_STATUS_VERB = {'attacked': 'атакован', 'limited': 'ограничен', 'closed': 'закрыт'}


def _notify_admin(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_ID:
        print('[warehouse_monitor] TELEGRAM_BOT_TOKEN/TELEGRAM_ADMIN_ID не заданы — уведомление пропущено')
        return
    try:
        resp = requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
            json={
                'chat_id': TELEGRAM_ADMIN_ID,
                'text': text,
                'parse_mode': 'HTML',
                'reply_markup': _OPEN_APP_BUTTON,
            },
            timeout=10,
        )
        # Раньше ответ Telegram не проверялся вообще — если chat_id неверный или
        # бот не может писать в этот чат, Telegram вернёт 200 с {"ok": false, ...}
        # (или 4xx), а скрипт молча считал уведомление отправленным.
        data = {}
        try:
            data = resp.json()
        except ValueError:
            pass
        if not resp.ok or data.get('ok') is False:
            print(f'[warehouse_monitor] Telegram notify FAILED: HTTP {resp.status_code}, ответ: {data or resp.text[:300]}')
        else:
            print('[warehouse_monitor] Telegram-уведомление отправлено')
    except Exception as e:
        print(f'[warehouse_monitor] Telegram notify error: {e}')


# Человекочитаемые названия источников — для текста "Статус: ПОДТВЕРЖДЕНО (...)"
# в уведомлении (см. ПРАВКА 4).
_SOURCE_LABELS = {
    'defence.ua':       'Минобороны Украины',
    'mil.gov.ua':       'Генштаб ВСУ',
    'nv.ua':             'NV',
    'suspilne.media':   'Суспільне',
    'pravda.com.ua':    'Украинская правда',
    'unian.net':        'УНІАН',
    'radiosvoboda.org': 'Радио Свобода',
    'hromadske.ua':     'Громадське',
    'novayagazeta.eu':  'Новая газета Европа',
    'vedomosti.ru':     'Ведомости',
    'rbc.ru':           'РБК',
    'kommersant.ru':    'Коммерсант',
}


def _source_label(url: str) -> str:
    """Человекочитаемое имя источника по URL — для текста уведомления."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    if host.startswith('www.'):
        host = host[4:]
    if host == _TELEGRAM_DOMAIN or host.endswith('.' + _TELEGRAM_DOMAIN):
        return 'Минобороны Украины (Telegram)' if 'defenceu' in urlparse(url).path.lower() else 'Telegram'
    for domain, label in _SOURCE_LABELS.items():
        if host == domain or host.endswith('.' + domain):
            return label
    return host or url


def _search_and_extract(query: str, known_names: list) -> list:
    """Один поисковый запрос → список находок с проставленными source_url/_tier.
    known_names мутируется на месте (append), чтобы новые склады, найденные в
    одном запросе, участвовали в группировке находок из следующего запроса."""
    found = []
    print(f'[warehouse_monitor] Поиск: {query!r}')
    results = _firecrawl_search(query)
    print(f'[warehouse_monitor]   источников найдено: {len(results)}')
    for r in results:
        if not r['text']:
            continue
        finding = _extract_with_claude(r['text'], known_names, r['url'])
        if not finding.get('found'):
            continue
        if str(finding.get('company', '')).strip().upper() == 'WB':
            resolved = _resolve_warehouse_name(str(finding.get('warehouse', '')).strip(), known_names)
            if resolved is None:
                # Не совпал ни с одним известным складом и не прошёл
                # is_valid_warehouse_name() — агрегированная/мусорная
                # находка, отбрасываем её целиком (см. _resolve_warehouse_name).
                continue
            if resolved not in known_names:
                print(f"[warehouse_monitor]   новый склад, не встречавшийся ранее: {resolved!r}")
                known_names.append(resolved)  # чтобы находки в этом же прогоне тоже сгруппировались
            finding['warehouse'] = resolved
        finding['source_url'] = r['url']
        finding['_tier'] = _classify_source_tier(r['url'])
        found.append(finding)
        print(f"[warehouse_monitor]   упоминание: {finding.get('company')}/{finding.get('warehouse')} "
              f"→ {finding.get('status')} (уровень источника {finding['_tier']})")
    return found


def _process_rss_source(feed_url: str, tier: int, label: str, keywords: list, known_names: list) -> list:
    """RSS-аналог _search_and_extract(): те же тексты проходят через тот же
    Claude-экстрактор и то же разрешение имени склада (is_valid_warehouse_name
    внутри _resolve_warehouse_name), только tier источника задан заранее
    (из RSS_SOURCES) вместо вычисления через _classify_source_tier(), которая
    не умеет классифицировать rsshub.app-адреса."""
    found = []
    texts = fetch_rss_source(feed_url, keywords)
    if texts:
        print(f'[RSS] {feed_url}: {len(texts)} записей')
    for text in texts:
        finding = _extract_with_claude(text, known_names, feed_url)
        if not finding.get('found'):
            continue
        if str(finding.get('company', '')).strip().upper() == 'WB':
            resolved = _resolve_warehouse_name(str(finding.get('warehouse', '')).strip(), known_names)
            if resolved is None:
                continue
            if resolved not in known_names:
                print(f"[warehouse_monitor]   новый склад, не встречавшийся ранее: {resolved!r}")
                known_names.append(resolved)
            finding['warehouse'] = resolved
        finding['source_url'] = feed_url
        finding['_tier'] = tier
        found.append(finding)
        print(f"[warehouse_monitor]   упоминание ({label}): {finding.get('company')}/{finding.get('warehouse')} "
              f"→ {finding.get('status')} (уровень источника {tier})")
    return found


def run():
    print(f'[warehouse_monitor] Старт: {datetime.now().isoformat()}')
    if not DATABASE_URL:
        print('[warehouse_monitor] DATABASE_URL не найден — выход')
        return

    conn = psycopg2.connect(DATABASE_URL)
    _ensure_schema(conn)
    known_names = _get_known_warehouse_names(conn)
    raw_findings = []  # каждая находка + '_tier' (уровень доверия источника)

    # RSS-источники (tg.i-c-a.su) — перед основным Firecrawl-поиском, см. RSS_SOURCES.
    # Пауза между запросами: диагностика 12.08.2026 показала rate-limit на
    # tg.i-c-a.su при двух запросах подряд без задержки.
    for i, (feed_url, tier, label) in enumerate(RSS_SOURCES):
        if i > 0:
            time.sleep(3)
        raw_findings.extend(_process_rss_source(feed_url, tier, label, RSS_KEYWORDS, known_names))

    # ЗАПРОС 1 — украинские источники (приоритетный, публикуют быстро и точно).
    raw_findings.extend(_search_and_extract(SEARCH_QUERY_UA, known_names))

    wb_ua_only = [f for f in raw_findings if str(f.get('company', '')).strip().upper() == 'WB']
    groups_ua = {}
    for f in wb_ua_only:
        groups_ua.setdefault((f['warehouse'], f['status']), []).append(f)
    ua_already_confirmed = any(
        _confidence([x['_tier'] for x in g], [x['source_url'] for x in g]) == 'confirmed'
        for g in groups_ua.values()
    )

    # ЗАПРОС 2 — российские источники (вторичный, только доп. подтверждение —
    # уровень 3 сам по себе никогда не даёт 'confirmed'). Пропускаем, если из
    # украинских источников уже есть подтверждение — экономия лимитов.
    if ua_already_confirmed:
        print('[warehouse_monitor] Подтверждение уже есть из украинских источников — '
              'пропускаем запрос по российским источникам (экономия лимитов)')
    else:
        raw_findings.extend(_search_and_extract(SEARCH_QUERY_RU, known_names))

    for query in COMPETITOR_SEARCH_QUERIES:
        raw_findings.extend(_search_and_extract(query, known_names))

    wb_findings = [f for f in raw_findings if str(f.get('company', '')).strip().upper() == 'WB']
    competitor_findings = [f for f in raw_findings if str(f.get('company', '')).strip().upper() != 'WB']

    # Группируем WB-находки по (склад, статус) — правило "confirmed/uncertain/ignored"
    # требует знать ВСЕ источники, сообщившие об одном и том же, а не рассматривать
    # каждую находку изолированно.
    groups = {}
    for f in wb_findings:
        groups.setdefault((f['warehouse'], f['status']), []).append(f)

    updated = []
    uncertain = []
    for (wh, status), group in groups.items():
        tiers = [f['_tier'] for f in group]
        urls = [f['source_url'] for f in group]
        conf = _confidence(tiers, urls)
        best = dict(group[0])
        best['confidence'] = conf
        best['source_urls'] = urls
        if conf == 'ignored':
            print(f"[warehouse_monitor] {wh} → {status}: только источники уровня 4 — "
                  f"игнорируем (не БД, не уведомление)")
            continue
        if conf == 'uncertain':
            print(f"[warehouse_monitor] {wh} → {status}: неуверенность (источники: {urls}) "
                  f"— БД не трогаем, только лог для ручной проверки, уведомление НЕ отправляется")
            uncertain.append(best)
            continue
        # conf == 'confirmed'
        if _update_warehouse_status(conn, best):
            updated.append(best)

    # Дедуп уведомлений: не слать одно и то же за последние 24 часа, кроме
    # confirmed — подтверждённая атака отправляется всегда, даже если склад уже
    # упоминался, иначе агент замолчит про реально новую смену статуса того же
    # склада. uncertain НИКОГДА не уведомляется (см. ПРАВКА 2/4) — только лог.
    recently_notified = _get_recently_notified(conn)
    to_notify_updated = [
        f for f in updated
        if f.get('confidence') == 'confirmed' or f.get('warehouse') not in recently_notified
    ]
    notified_names = [f.get('warehouse') for f in to_notify_updated]

    # Один сводный лог на запуск: found=True, если было хоть одно распознанное упоминание
    # (включая конкурентов, confirmed, uncertain и ignored-находки — все они попадают в
    # raw_response для ручной проверки, независимо от того, уведомляли мы или нет).
    all_for_log = wb_findings + competitor_findings
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO warehouse_monitor_log
            (found, warehouse, status, summary, source_url, raw_response, notified_warehouses)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (
        bool(all_for_log),
        all_for_log[0].get('warehouse') if all_for_log else None,
        all_for_log[0].get('status') if all_for_log else None,
        '; '.join(f"{f.get('company')}/{f.get('warehouse')}→{f.get('status')}" for f in all_for_log) or None,
        all_for_log[0].get('source_url') if all_for_log else None,
        json.dumps(all_for_log, ensure_ascii=False, default=str) if all_for_log else None,
        json.dumps(notified_names, ensure_ascii=False) if notified_names else None,
    ))
    conn.commit()
    cur.close()
    conn.close()

    if to_notify_updated:
        lines = []
        for f in to_notify_updated:
            verb = _STATUS_VERB.get(f.get('status'), f.get('status'))
            urls = f.get('source_urls') or [f.get('source_url')]
            labels = sorted({_source_label(u) for u in urls if u})
            lines.append(f"- {f.get('warehouse')} — {verb} {f.get('date', '—')}")
            if len(urls) > 1:
                lines.append(f"  Источники: {', '.join(u for u in urls if u)}")
            else:
                lines.append(f"  Источник: {urls[0]}")
            lines.append(f"  Статус: ПОДТВЕРЖДЕНО ({' / '.join(labels)})")
        _notify_admin("🔴 Подтверждённая атака на склад WB\n\n" + "\n".join(lines))
        print(f'[warehouse_monitor] Обновлено складов: {len(updated)} (уведомлено: {len(to_notify_updated)})')
    elif updated:
        print(f'[warehouse_monitor] Обновлено складов: {len(updated)} '
              f'(уведомление подавлено — уже сообщали за последние 24ч)')

    if uncertain:
        print(f'[warehouse_monitor] Находок с низкой уверенностью (uncertain): {len(uncertain)} '
              f'— залогированы для ручной проверки, уведомление НЕ отправлялось')

    if competitor_findings:
        lines = [f"• {f.get('company')}: {f.get('warehouse')} → {f.get('status')} ({f.get('source_url')})"
                  for f in competitor_findings]
        print('[warehouse_monitor] Упоминания складов конкурентов:\n' + '\n'.join(lines))
        _notify_admin("ℹ️ warehouse_monitor: упоминания складов конкурентов (информационно, в БД не пишем)\n\n"
                      + '\n'.join(lines))

    if not (to_notify_updated or competitor_findings):
        print('[warehouse_monitor] Тишина — нет подтверждённых новых атак')

    print(f'[warehouse_monitor] Готово: {datetime.now().isoformat()}')


if __name__ == '__main__':
    run()
