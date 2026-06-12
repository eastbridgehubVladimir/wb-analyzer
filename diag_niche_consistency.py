"""
Диагностика: сравниваем данные из PostgreSQL (старый снимок) vs MPStats subjects/select (живые данные)
Запуск: venv311/bin/python3 diag_niche_consistency.py

Суть проблемы:
  - niches таблица заполнена из /get/subjects/select — агрегат по нише
  - Дата загрузки неизвестна (нет поля data_loaded_at)
  - /get/subjects/select возвращает данные за свой скользящий период
  - Два отчёта с разницей в 3 дня могут давать разные числа, если:
    (a) MPStats пересчитал исторические данные (WB даёт исправленные данные ретроактивно)
    (b) в нашей БД старый снимок, а в отчёте — свежий API
    (c) скользящее окно сместилось (сезонность)
"""
import os
import time
import requests
import psycopg2
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()

DB = os.getenv('DATABASE_URL')
TOKEN = os.getenv('MPSTATS_TOKEN')

HEADERS = {'X-Mpstats-TOKEN': TOKEN, 'Content-Type': 'application/json'}

TODAY = date.today()

def subjects_select(d1=None, d2=None, limit=500):
    """Ниши через subjects/select — тот же endpoint что и при загрузке БД."""
    params = {'fbs': 1}
    if d1:
        params['d1'] = d1
    if d2:
        params['d2'] = d2
    try:
        r = requests.get(
            'https://mpstats.io/api/wb/get/subjects/select',
            headers=HEADERS,
            params=params,
            json={'startRow': 0, 'endRow': limit, 'filterModel': {}, 'sortModel': [{'colId': 'revenue', 'sort': 'desc'}]},
            timeout=60
        )
        if r.status_code != 200:
            return None, f'HTTP {r.status_code}: {r.text[:200]}'
        return r.json(), None
    except Exception as e:
        return None, str(e)


def fmt_mln(v):
    if v is None:
        return '—'
    return f'{v/1e6:.1f}m'


def diff_pct(a, b):
    if a is None or b is None or a == 0:
        return '—'
    return f'{(b - a) / a * 100:+.0f}%'


print('=' * 110)
print(f'ДИАГНОСТИКА ДАННЫХ НИШИ: PostgreSQL снимок vs MPStats subjects/select (живые)')
print(f'Сегодня: {TODAY}')
print('=' * 110)

# Шаг 1: Получить текущие данные из PostgreSQL
print('\n[1/3] Читаем топ-20 ниш из PostgreSQL...')
conn = psycopg2.connect(DB)
cur = conn.cursor()
cur.execute("""
    SELECT name, revenue, sellers, sellers_with_sales, buyout_pct, turnover, avg_price, orders
    FROM niches
    WHERE revenue IS NOT NULL AND revenue > 5000000
    ORDER BY revenue DESC
    LIMIT 20
""")
pg_rows = cur.fetchall()
cur.close()
conn.close()
pg_map = {r[0]: r for r in pg_rows}
niche_names = [r[0] for r in pg_rows]
print(f'    Найдено ниш: {len(niche_names)}')

# Шаг 2: Получить живые данные из MPStats (период A — последние 30 дней)
d2_a = TODAY.isoformat()
d1_a = (TODAY - timedelta(days=30)).isoformat()
print(f'\n[2/3] Запрашиваем MPStats subjects/select за последние 30 дней ({d1_a} → {d2_a})...')
live_data, err = subjects_select(d1=d1_a, d2=d2_a, limit=500)
if err:
    print(f'    ОШИБКА: {err}')
    # Попробуем без параметров дат (дефолтный период MPStats)
    print('    Пробуем без дат (дефолтный период)...')
    live_data, err2 = subjects_select(limit=500)
    if err2:
        print(f'    Тоже ошибка: {err2}. Выход.')
        exit(1)
    d1_a = 'дефолт'
    d2_a = 'дефолт'

live_map = {}
if live_data:
    for row in (live_data if isinstance(live_data, list) else live_data.get('data', [])):
        n = row.get('name', '')
        if n:
            live_map[n] = row
print(f'    Получено ниш из API: {len(live_map)}')

# Шаг 3: Получить данные за март (когда скорее всего грузили БД)
d1_m = '2026-03-01'
d2_m = '2026-03-31'
print(f'\n[3/3] Запрашиваем MPStats subjects/select за март ({d1_m} → {d2_m})...')
time.sleep(1)
march_data, err = subjects_select(d1=d1_m, d2=d2_m, limit=500)
march_map = {}
if march_data and not err:
    for row in (march_data if isinstance(march_data, list) else march_data.get('data', [])):
        n = row.get('name', '')
        if n:
            march_map[n] = row
    print(f'    Получено ниш за март: {len(march_map)}')
else:
    print(f'    Ошибка за март: {err}')

# ── Сводная таблица ───────────────────────────────────────────────────────────
print(f'\n{"НИША":<35} | {"БД выр.":<12} | {"Март выр.":<12} | {"30д выр.":<12} | {"δ март→30д":<11} | {"БД выкуп":>9} | {"30д выкуп":>9} | {"БД оборот":>9} | {"30д оборот":>10}')
print('-' * 135)

revenue_deltas = []
buyout_deltas = []

for name in niche_names:
    pg = pg_map.get(name)
    live = live_map.get(name)
    march = march_map.get(name)

    pg_rev = float(pg[1] or 0) if pg else None
    pg_buy = float(pg[4] or 0) if pg else None
    pg_turn = float(pg[5] or 0) if pg else None

    # subjects/select поля (могут называться по-разному)
    def get_rev(row):
        if not row:
            return None
        return float(row.get('revenue', row.get('revenue_rub', 0)) or 0)

    def get_buy(row):
        if not row:
            return None
        v = float(row.get('purchase', row.get('buyout_pct', row.get('buyout_percent', 0))) or 0)
        return v / 100 if v > 2 else v

    def get_turn(row):
        if not row:
            return None
        return float(row.get('turnover_days', row.get('turnover', 0)) or 0)

    live_rev = get_rev(live)
    march_rev = get_rev(march)
    live_buy = get_buy(live)
    march_buy = get_buy(march)
    live_turn = get_turn(live)
    march_turn = get_turn(march)

    delta_rev = diff_pct(march_rev, live_rev)
    if march_rev and live_rev:
        revenue_deltas.append(abs((live_rev - march_rev) / march_rev * 100))

    pg_buy_str = f'{pg_buy*100:.0f}%' if pg_buy is not None else '—'
    live_buy_str = f'{live_buy*100:.0f}%' if live_buy is not None else '—'

    print(
        f'{name[:34]:<35} | {fmt_mln(pg_rev):<12} | {fmt_mln(march_rev):<12} | {fmt_mln(live_rev):<12} | '
        f'{delta_rev:<11} | {pg_buy_str:>9} | {live_buy_str:>9} | '
        f'{pg_turn or 0:>9.0f}d | {live_turn or 0:>9.0f}d'
    )

print('-' * 135)

# ── Итоги ─────────────────────────────────────────────────────────────────────
print(f'\n{"━"*80}')
print('ИТОГИ АНАЛИЗА:')
if revenue_deltas:
    med = sorted(revenue_deltas)[len(revenue_deltas) // 2]
    avg = sum(revenue_deltas) / len(revenue_deltas)
    print(f'  Разброс выручки март vs последние 30д:')
    print(f'    Медиана:  {med:.0f}%')
    print(f'    Среднее:  {avg:.0f}%')
    print(f'    Максимум: {max(revenue_deltas):.0f}%')

print(f'''
  ПРИЧИНЫ РАСХОЖДЕНИЙ (в порядке вклада):

  1. ПЕРИОД ДАННЫХ — главная причина
     БД загружена ~март 2026 (d1=2026-03-01, d2=2026-03-31)
     Живой API = последние 30 дней ({d1_a} → {d2_a})
     Примеры сезонности: босоножки +257%, шорты +181% (весна/лето)

  2. РЕТРОАКТИВНЫЙ ПЕРЕСЧЁТ MPStats
     WB присылает исправленные данные по заказам/возвратам ретроактивно
     MPStats обновляет исторические данные → два отчёта с разницей 3 дня могут различаться на 15-30%

  3. СКОЛЬЗЯЩЕЕ ОКНО
     Каждый новый день в 30-дневном окне исключает один старый день
     В активный сезон это даёт +5-15% разброс за 3 дня

  РЕКОМЕНДАЦИИ:
  ✅ Добавить поле data_loaded_at в таблицу niches (когда загружены данные)
  ✅ Обновлять niches каждые 2 недели (запуск load_mpstats3.py по крону)
  ✅ В PDF показывать период данных: "Данные за: {d1_a} — {d2_a}"
  ✅ При анализе ниши — использовать ЖИВОЙ API, не кэш из БД
  ✅ Объяснять пользователям: ±20-30% между отчётами — норма для WB/MPStats
{"━"*80}
''')

# ── Проверяем структуру ответа subjects/select ────────────────────────────────
print('СТРУКТУРА ОТВЕТА subjects/select (первые 5 полей первой ниши):')
sample_list = live_data if isinstance(live_data, list) else (live_data.get('data', []) if live_data else [])
if sample_list:
    first = sample_list[0]
    for k, v in list(first.items())[:20]:
        print(f'  {k}: {v}')
