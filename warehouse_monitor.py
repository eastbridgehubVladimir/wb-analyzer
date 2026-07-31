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
    (ровно это происходило дважды с сервисом trustworthy-presence). Если пакет
    уже есть — pip install не запускается, лишнего времени на прогон не тратится."""
    pkgs = []
    try:
        import requests  # noqa
    except ImportError:
        pkgs.append('requests==2.31.0')
    try:
        import psycopg2  # noqa
    except ImportError:
        pkgs.append('psycopg2-binary==2.9.9')
    try:
        import anthropic  # noqa
    except ImportError:
        pkgs.append('anthropic==0.25.0')
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


_ensure_deps()

import json
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

# Источники мониторинга и уровни доверия (обновлено 31.07.2026 после ложного
# срабатывания по YouTube-шортсу и dw.com-агрегатору — Коледино и Екатеринбург
# были ошибочно помечены 'attacked', оба склада на деле работали).
#
# Уровень 1 — Минобороны Украины (максимальное доверие)
# Уровень 2 — украинские новостные агентства
# Уровень 3 — официальный Telegram-канал WB
# Уровень 4 — российские источники (с задержкой, для подтверждения)
#
# Правила применения уровней — см. _confidence(): уровень 1/2 сам по себе
# подтверждает факт; уровень 3/4 нужен минимум от двух разных источников;
# иначе находка помечается 'uncertain' и в БД не пишется — только логируется.
SOURCE_TIERS = {
    'defence.ua':       1,
    'suspilne.media':   2,
    'pravda.com.ua':    2,
    'radiosvoboda.org': 2,
    'unian.net':        2,
    'hromadske.ua':     2,
    'kommersant.ru':    4,
    'rbc.ru':           4,
}

# Telegram (t.me) — отдельно от SOURCE_TIERS: обычный веб-поиск Firecrawl плохо
# индексирует посты конкретных каналов, поэтому t.me/DefenceU (уровень 1) и
# официальный канал WB (уровень 3) нельзя надёжно различить по URL через
# веб-поиск. Best-effort в _classify_source_tier(): путь содержит "defenceu" →
# уровень 1, иначе относим t.me-ссылку к уровню 3 (офиц. канал WB). Для
# по-настоящему надёжного покрытия Telegram в будущем нужен Telegram Bot API
# (чтение постов конкретных каналов напрямую), а не веб-поиск.
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

# Запросы для мониторинга складов WB.
SEARCH_QUERIES = [
    f"Wildberries склад атака пожар БПЛА ({_DOMAIN_FILTER})",
    f"Wildberries склад ({_DOMAIN_FILTER})",
]

# Отдельный запрос для складов конкурентов.
COMPETITOR_SEARCH_QUERIES = [
    f"{' '.join(COMPETITOR_KEYWORDS)} склад атака пожар БПЛА ({_DOMAIN_FILTER})",
]

VALID_STATUSES = {'active', 'limited', 'closed', 'attacked'}


def _firecrawl_search(query: str, limit: int = 5) -> list:
    """Ищет новости через Firecrawl REST API. Возвращает [{title, url, text}]."""
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
                'scrapeOptions': {'formats': ['markdown'], 'onlyMainContent': True},
            },
            timeout=30,
        )
        resp.raise_for_status()
        # v1/search возвращает {"success": true, "data": [...]} — плоский список,
        # без вложенных ключей news/web (в отличие от параметра sources у MCP-обёртки,
        # который REST API вообще не принимает — 400 Unrecognized key "sources").
        results = resp.json().get('data') or []
        out = []
        for r in results:
            out.append({
                'title': r.get('title', ''),
                'url': r.get('url', ''),
                'text': (r.get('markdown') or r.get('description') or '')[:4000],
            })
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
        return 1 if 'defenceu' in urlparse(url).path.lower() else 3
    for domain, tier in SOURCE_TIERS.items():
        if host == domain or host.endswith('.' + domain):
            return tier
    return 0


def _confidence(tiers: list, source_urls: list) -> str:
    """confirmed / probable / uncertain — правила подтверждения по уровням источников.
    confirmed: хотя бы один источник уровня 1 или 2.
    probable:  2+ РАЗНЫХ источника уровня 3-4 сообщают одно и то же.
    uncertain: всё остальное — в БД не пишем, только логируем для ручной проверки."""
    if any(t in (1, 2) for t in tiers):
        return 'confirmed'
    tier34_sources = {u for t, u in zip(tiers, source_urls) if t in (3, 4)}
    if len(tier34_sources) >= 2:
        return 'probable'
    return 'uncertain'


def _get_known_warehouse_names(conn) -> list:
    """Список канонических названий складов из warehouse_status — Claude должен
    сопоставлять найденный текст с этим списком, а не придумывать своё название
    (иначе 'Koledino' и 'Wildberries Екатеринбург' создают дубли вместо апдейта
    существующих строк 'Коледино'/'Екатеринбург' — реальный баг, найденный на проверке)."""
    cur = conn.cursor()
    cur.execute("SELECT name FROM warehouse_status ORDER BY name")
    names = [r[0] for r in cur.fetchall()]
    cur.close()
    return names


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
        "Найди в тексте упоминания складов, пострадавших от атак/пожаров/аварий.\n\n"
        f"Известные склады Wildberries (используй ТОЛЬКО эти названия, ровно как написано): "
        f"{names_list}.\n"
        f"Также отслеживай склады конкурентов: {competitors_list}.\n\n"
        f"ИСТОЧНИК ТЕКСТА: {source_url or 'неизвестен'}\n"
        "Если статья сама указывает, что пересказывает непроверенный вторичный источник "
        "(например, анонимный Telegram-канал без первоисточника, слухи, неподтверждённые "
        'данные) — не доверяй такому фрагменту и верни {"found": false}.\n\n'
        "Если найден склад WB из списка — верни JSON ОДНИМ объектом (не массивом): "
        '{"company": "WB", "warehouse": "название ровно из списка WB выше", '
        '"status": "attacked/closed/limited", "date": "дата", "summary": "краткое описание"}.\n'
        "Если найден склад конкурента — верни: "
        '{"company": "Ozon|Яндекс Маркет|СДЭК|Почта России", "warehouse": "название/город склада", '
        '"status": "attacked/closed/limited", "date": "дата", "summary": "краткое описание"}.\n'
        "Если ни склада WB из списка, ни склада конкурента не упомянуто — верни "
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
        company = str(result.get('company', '')).strip()
        if company == 'WB' and str(result.get('warehouse', '')).strip() not in known_names:
            # Claude всё равно не попал в список — не создаём дубль, пропускаем находку.
            print(f"[warehouse_monitor] warehouse {result.get('warehouse')!r} не в известном списке — пропущено")
            return {'found': False}
        result['found'] = True
        result['raw_response'] = raw
        return result
    except Exception as e:
        print(f'[warehouse_monitor] Claude extract error: {e}')
        return {'found': False}


def _update_warehouse_status(conn, finding: dict) -> bool:
    """Обновляет существующую запись в warehouse_status. Возвращает True, если статус изменился.
    Название уже проверено против known_names в _extract_with_claude — новые строки не создаём,
    чтобы не плодить дубли под слегка другим написанием того же склада."""
    name = str(finding.get('warehouse', '')).strip()
    status = str(finding.get('status', '')).strip().lower()
    if not name or status not in VALID_STATUSES:
        return False
    note = finding.get('summary', '')
    source_url = finding.get('source_url', '')
    cur = conn.cursor()
    cur.execute("SELECT status FROM warehouse_status WHERE name = %s", (name,))
    row = cur.fetchone()
    if not row:
        cur.close()
        return False  # защита: имени нет в БД — не создаём новую строку
    if row[0] == status:
        cur.close()
        return False  # статус не изменился — обновлять нечего
    cur.execute("""
        UPDATE warehouse_status
        SET status = %s, status_note = %s, updated_at = NOW(), source_url = %s,
            attacked_at = CASE WHEN %s = 'attacked' THEN NOW() ELSE attacked_at END
        WHERE name = %s
    """, (status, note, source_url, status, name))
    conn.commit()
    cur.close()
    return True


def _notify_admin(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_ID:
        print('[warehouse_monitor] TELEGRAM_BOT_TOKEN/TELEGRAM_ADMIN_ID не заданы — уведомление пропущено')
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_ADMIN_ID, 'text': text, 'parse_mode': 'HTML'},
            timeout=10,
        )
    except Exception as e:
        print(f'[warehouse_monitor] Telegram notify error: {e}')


def run():
    print(f'[warehouse_monitor] Старт: {datetime.now().isoformat()}')
    if not DATABASE_URL:
        print('[warehouse_monitor] DATABASE_URL не найден — выход')
        return

    conn = psycopg2.connect(DATABASE_URL)
    known_names = _get_known_warehouse_names(conn)
    raw_findings = []  # каждая находка + '_tier' (уровень доверия источника)

    for query in SEARCH_QUERIES + COMPETITOR_SEARCH_QUERIES:
        print(f'[warehouse_monitor] Поиск: {query!r}')
        results = _firecrawl_search(query)
        print(f'[warehouse_monitor]   источников найдено: {len(results)}')
        for r in results:
            if not r['text']:
                continue
            finding = _extract_with_claude(r['text'], known_names, r['url'])
            if not finding.get('found'):
                continue
            finding['source_url'] = r['url']
            finding['_tier'] = _classify_source_tier(r['url'])
            raw_findings.append(finding)
            print(f"[warehouse_monitor]   упоминание: {finding.get('company')}/{finding.get('warehouse')} "
                  f"→ {finding.get('status')} (уровень источника {finding['_tier']})")

    wb_findings = [f for f in raw_findings if str(f.get('company', '')).strip().upper() == 'WB']
    competitor_findings = [f for f in raw_findings if str(f.get('company', '')).strip().upper() != 'WB']

    # Группируем WB-находки по (склад, статус) — правило "confirmed/probable/uncertain"
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
        if conf == 'uncertain':
            print(f"[warehouse_monitor] {wh} → {status}: неуверенность (источники: {urls}) "
                  f"— БД не трогаем, только лог для ручной проверки")
            uncertain.append(best)
            continue
        if conf == 'probable':
            best['summary'] = (best.get('summary') or '') + ' (вероятно — 2+ источника уровня 3-4, требует проверки)'
        if _update_warehouse_status(conn, best):
            updated.append(best)

    # Один сводный лог на запуск: found=True, если было хоть одно распознанное упоминание
    # (включая конкурентов и uncertain-находки — они тоже идут в raw_response для ручной проверки).
    all_for_log = wb_findings + competitor_findings
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO warehouse_monitor_log (found, warehouse, status, summary, source_url, raw_response)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (
        bool(all_for_log),
        all_for_log[0].get('warehouse') if all_for_log else None,
        all_for_log[0].get('status') if all_for_log else None,
        '; '.join(f"{f.get('company')}/{f.get('warehouse')}→{f.get('status')}" for f in all_for_log) or None,
        all_for_log[0].get('source_url') if all_for_log else None,
        json.dumps(all_for_log, ensure_ascii=False, default=str) if all_for_log else None,
    ))
    conn.commit()
    cur.close()
    conn.close()

    if updated:
        lines = [f"• {f.get('warehouse')} → {f.get('status')} [{f.get('confidence')}] ({f.get('date', '—')})"
                  for f in updated]
        _notify_admin("⚠️ warehouse_monitor: обновлён статус складов WB\n\n" + "\n".join(lines))
        print(f'[warehouse_monitor] Обновлено складов: {len(updated)}')
    if uncertain:
        print(f'[warehouse_monitor] Находок с низкой уверенностью: {len(uncertain)} — требуют ручной проверки (см. лог)')
    if competitor_findings:
        lines = [f"• {f.get('company')}: {f.get('warehouse')} → {f.get('status')} ({f.get('source_url')})"
                  for f in competitor_findings]
        print('[warehouse_monitor] Упоминания складов конкурентов:\n' + '\n'.join(lines))
        _notify_admin("ℹ️ warehouse_monitor: упоминания складов конкурентов (информационно, в БД не пишем)\n\n"
                      + '\n'.join(lines))
    if not (updated or uncertain or competitor_findings):
        print('[warehouse_monitor] Упоминаний складов не найдено')

    print(f'[warehouse_monitor] Готово: {datetime.now().isoformat()}')


if __name__ == '__main__':
    run()
