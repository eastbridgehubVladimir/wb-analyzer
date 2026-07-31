"""
warehouse_monitor.py — Агент v1: мониторинг статуса складов WB.

Раз в 4 часа (Railway cron) ищет свежие новости об атаках/закрытии складов WB
через Firecrawl, передаёт найденные тексты в Claude API для извлечения
структурированных данных, обновляет таблицу warehouse_status, логирует
каждый запуск в warehouse_monitor_log и уведомляет TELEGRAM_ADMIN_ID
о найденных изменениях.

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

# Надёжные источники — только им доверяем при обновлении статуса склада.
# Добавлено 31.07.2026: агент по YouTube-шортсу и агрегатору dw.com ошибочно
# пометил Коледино и Екатеринбург как 'attacked' — оба склада на деле работают.
TRUSTED_DOMAINS = ['kommersant.ru', 'rbc.ru', 'lenta.ru', 'wildberries.ru', 'wb.ru']

# v1/search REST API не принимает includeDomains как поле JSON (только query:
# 'sources' и 'includeDomains' — фичи MCP-обёртки, не голого REST) — фильтр по
# домену делаем через оператор site: прямо в строке запроса.
_DOMAIN_FILTER = ' OR '.join(f'site:{d}' for d in TRUSTED_DOMAINS)

# Запросы для мониторинга. Второй специально сужен на РБК/Коммерсант —
# источники с наиболее оперативными и достоверными новостями об атаках.
SEARCH_QUERIES = [
    f"Wildberries склад атака пожар БПЛА ({_DOMAIN_FILTER})",
    f"Wildberries склад ({_DOMAIN_FILTER})",
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
    """Просит Claude извлечь структурированные данные об атаке/закрытии склада."""
    if not ANTHROPIC_API_KEY:
        print('[warehouse_monitor] ANTHROPIC_API_KEY не задан — извлечение пропущено')
        return {'found': False}
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    names_list = ', '.join(known_names)
    trusted_list = ', '.join(TRUSTED_DOMAINS)
    prompt = (
        "Найди в тексте упоминания конкретных складов WB (Wildberries). "
        f"Список известных складов (используй ТОЛЬКО эти названия, ровно как написано): {names_list}.\n\n"
        f"ИСТОЧНИК ТЕКСТА: {source_url or 'неизвестен'}\n"
        f"Используй ТОЛЬКО надёжные источники: Коммерсант, РБК, Lenta.ru, официальные каналы WB "
        f"(домены: {trusted_list}). Если источник — YouTube, любая соцсеть (VK, Telegram-каналы "
        "неофициальных лиц, Instagram и т.п.), агрегатор или блог без указания первоисточника — "
        'верни {"found": false}, ДАЖЕ ЕСЛИ текст выглядит релевантным и достоверным на вид.\n\n'
        "Если склад из списка выше атакован/закрыт/эвакуирован и источник надёжный — верни JSON "
        'ОДНИМ объектом (не массивом): {"warehouse": "название ровно из списка выше", '
        '"status": "attacked/closed/limited", "date": "дата", "summary": "краткое описание"}. '
        "Если упомянутого склада нет в списке, источник ненадёжен или ничего не найдено — "
        'верни {"found": false}.\n\n'
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
        if str(result.get('warehouse', '')).strip() not in known_names:
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
    all_findings = []
    updated = []

    for query in SEARCH_QUERIES:
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
            all_findings.append(finding)
            print(f"[warehouse_monitor]   упоминание: {finding.get('warehouse')} → {finding.get('status')}")
            if _update_warehouse_status(conn, finding):
                updated.append(finding)

    # Один сводный лог на запуск: found=True, если было хоть одно распознанное упоминание.
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO warehouse_monitor_log (found, warehouse, status, summary, source_url, raw_response)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (
        bool(all_findings),
        all_findings[0].get('warehouse') if all_findings else None,
        all_findings[0].get('status') if all_findings else None,
        '; '.join(f"{f.get('warehouse')}→{f.get('status')}" for f in all_findings) or None,
        all_findings[0].get('source_url') if all_findings else None,
        json.dumps(all_findings, ensure_ascii=False) if all_findings else None,
    ))
    conn.commit()
    cur.close()
    conn.close()

    if updated:
        lines = [f"• {f.get('warehouse')} → {f.get('status')} ({f.get('date', '—')})" for f in updated]
        _notify_admin(
            "⚠️ warehouse_monitor: обновлён статус складов WB\n\n" + "\n".join(lines)
        )
        print(f'[warehouse_monitor] Обновлено складов: {len(updated)}')
    elif all_findings:
        print(f'[warehouse_monitor] Найдено {len(all_findings)} упоминаний, но статусы не изменились')
    else:
        print('[warehouse_monitor] Упоминаний складов не найдено')

    print(f'[warehouse_monitor] Готово: {datetime.now().isoformat()}')


if __name__ == '__main__':
    run()
