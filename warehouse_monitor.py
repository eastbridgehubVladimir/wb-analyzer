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
"""
import os
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

# Запросы для мониторинга. Второй специально сужен на РБК/Коммерсант —
# источники с наиболее оперативными и достоверными новостями об атаках.
SEARCH_QUERIES = [
    "Wildberries склад атака пожар БПЛА",
    "Wildberries склад",
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
                'sources': [{'type': 'news'}],
                'scrapeOptions': {'formats': ['markdown'], 'onlyMainContent': True},
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get('data', {})
        results = data.get('news') or data.get('web') or []
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


def _extract_with_claude(text: str) -> dict:
    """Просит Claude извлечь структурированные данные об атаке/закрытии склада."""
    if not ANTHROPIC_API_KEY:
        print('[warehouse_monitor] ANTHROPIC_API_KEY не задан — извлечение пропущено')
        return {'found': False}
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = (
        "Найди в тексте упоминания конкретных складов WB (Wildberries). "
        "Если склад атакован/закрыт/эвакуирован — верни JSON: "
        '{"warehouse": "название", "status": "attacked/closed/limited", '
        '"date": "дата", "summary": "краткое описание"}. '
        'Если ничего не найдено — верни {"found": false}.\n\n'
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
        if not result or result.get('found') is False:
            return {'found': False}
        result['found'] = True
        result['raw_response'] = raw
        return result
    except Exception as e:
        print(f'[warehouse_monitor] Claude extract error: {e}')
        return {'found': False}


def _update_warehouse_status(conn, finding: dict) -> bool:
    """Обновляет/создаёт запись в warehouse_status. Возвращает True, если статус изменился."""
    name = str(finding.get('warehouse', '')).strip()
    status = str(finding.get('status', '')).strip().lower()
    if not name or status not in VALID_STATUSES:
        return False
    note = finding.get('summary', '')
    source_url = finding.get('source_url', '')
    cur = conn.cursor()
    cur.execute("SELECT status FROM warehouse_status WHERE name = %s", (name,))
    row = cur.fetchone()
    if row and row[0] == status:
        cur.close()
        return False  # статус не изменился — обновлять нечего
    if row:
        cur.execute("""
            UPDATE warehouse_status
            SET status = %s, status_note = %s, updated_at = NOW(), source_url = %s,
                attacked_at = CASE WHEN %s = 'attacked' THEN NOW() ELSE attacked_at END
            WHERE name = %s
        """, (status, note, source_url, status, name))
    else:
        cur.execute("""
            INSERT INTO warehouse_status (name, status, status_note, source_url, attacked_at)
            VALUES (%s, %s, %s, %s, CASE WHEN %s = 'attacked' THEN NOW() ELSE NULL END)
        """, (name, status, note, source_url, status))
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
    all_findings = []
    updated = []

    for query in SEARCH_QUERIES:
        print(f'[warehouse_monitor] Поиск: {query!r}')
        results = _firecrawl_search(query)
        print(f'[warehouse_monitor]   источников найдено: {len(results)}')
        for r in results:
            if not r['text']:
                continue
            finding = _extract_with_claude(r['text'])
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
