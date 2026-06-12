"""
Обновляет данные ниш в PostgreSQL из MPStats /subjects/select.
- Исправляет orders = sales (правильное поле продаж, не orders_count)
- Обновляет revenue, avg_price, buyout_pct, turnover, sellers_with_sales
- Устанавливает data_updated_at = NOW()
- Делает снимок в таблицу niche_history (для накопления истории)

Использует батчевую загрузку через временную таблицу — один SQL-запрос
вместо 7550 отдельных UPDATE (критично для Railway с network latency).

Запуск: venv311/bin/python3 update_niches.py
Рекомендуется запускать раз в 2 недели.
"""
import os
import sys
import requests
import psycopg2
import psycopg2.extras
from datetime import date, datetime
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('MPSTATS_TOKEN')
DB = os.getenv('DATABASE_URL')

if not TOKEN or not DB:
    print('ОШИБКА: MPSTATS_TOKEN или DATABASE_URL не заданы в .env')
    sys.exit(1)

HEADERS = {'X-Mpstats-TOKEN': TOKEN, 'Content-Type': 'application/json'}
TODAY = date.today()
NOW = datetime.utcnow()


def fetch_all_niches():
    print('Запрашиваем MPStats /subjects/select (все ниши)...')
    r = requests.get(
        'https://mpstats.io/api/wb/get/subjects/select',
        headers=HEADERS,
        params={'fbs': 1},
        json={'startRow': 0, 'endRow': 9999, 'filterModel': {}, 'sortModel': []},
        timeout=60,
    )
    if r.status_code != 200:
        print(f'ОШИБКА HTTP {r.status_code}: {r.text[:300]}')
        sys.exit(1)
    data = r.json()
    print(f'Получено ниш: {len(data)}')
    return data


def main():
    data = fetch_all_niches()

    # Подготавливаем строки для батчевой вставки
    rows = []
    for row in data:
        name = row.get('name', '')
        if not name:
            continue
        rows.append((
            name,
            float(row.get('revenue', 0) or 0),
            int(row.get('sales', 0) or 0),           # реальные продажи
            float(row.get('final_price_median', 0) or 0),
            float(row.get('purchase', 0) or 0) / 100,
            float(row.get('turnover_days', 0) or 0),
            int(row.get('suppliers_with_sells', 0) or 0),
            float(row.get('revenue_purchase', 0) or 0),  # выручка после выкупа
        ))

    print(f'Подготовлено строк: {len(rows)}')
    print(f'Загружаем в БД одним батчем...')

    conn = psycopg2.connect(DB)
    cur = conn.cursor()

    # 1. Создаём временную таблицу
    cur.execute("""
        CREATE TEMP TABLE _niche_updates (
            name TEXT,
            revenue NUMERIC,
            sales INTEGER,
            avg_price NUMERIC,
            buyout_pct NUMERIC,
            turnover NUMERIC,
            sellers_with_sales INTEGER,
            revenue_with_buyout NUMERIC
        ) ON COMMIT DROP
    """)

    # 2. Батчевая вставка во временную таблицу (~одна секунда)
    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO _niche_updates VALUES %s",
        rows,
        page_size=1000,
    )
    print(f'  Временная таблица заполнена: {len(rows)} строк')

    # 3. Один UPDATE из temp → niches
    cur.execute("""
        UPDATE niches n
        SET
            revenue             = u.revenue,
            revenue_with_buyout = u.revenue_with_buyout,
            orders              = u.sales,
            avg_price           = u.avg_price,
            buyout_pct          = u.buyout_pct,
            turnover            = u.turnover,
            sellers_with_sales  = u.sellers_with_sales,
            data_updated_at     = %s
        FROM _niche_updates u
        WHERE n.name = u.name
    """, (NOW,))
    updated_niches = cur.rowcount
    print(f'  niches обновлено: {updated_niches}')

    # 4. Снимок в niche_history — один INSERT ... ON CONFLICT
    cur.execute("""
        INSERT INTO niche_history
            (niche_name, snapshot_date, revenue, sales, orders, avg_price, buyout_pct, turnover, sellers_with_sales)
        SELECT
            u.name, %s, u.revenue, u.sales, u.sales, u.avg_price, u.buyout_pct, u.turnover, u.sellers_with_sales
        FROM _niche_updates u
        JOIN niches n ON n.name = u.name
        ON CONFLICT (niche_name, snapshot_date) DO UPDATE SET
            revenue            = EXCLUDED.revenue,
            sales              = EXCLUDED.sales,
            orders             = EXCLUDED.orders,
            avg_price          = EXCLUDED.avg_price,
            buyout_pct         = EXCLUDED.buyout_pct,
            turnover           = EXCLUDED.turnover,
            sellers_with_sales = EXCLUDED.sellers_with_sales
    """, (TODAY,))
    history_rows = cur.rowcount
    print(f'  niche_history строк: {history_rows}')

    conn.commit()
    cur.close()
    conn.close()

    print(f'\n✅ Готово!')
    print(f'   niches обновлено:      {updated_niches}')
    print(f'   niche_history записей: {history_rows}')
    print(f'   data_updated_at =      {NOW.strftime("%Y-%m-%d %H:%M UTC")}')

    # Быстрая проверка по Вытяжкам
    conn2 = psycopg2.connect(DB)
    cur2 = conn2.cursor()
    cur2.execute("""
        SELECT name, orders, revenue, revenue_with_buyout, avg_price, data_updated_at
        FROM niches
        WHERE name IN ('Вытяжки кухонные', 'Кроссовки', 'Платья')
        ORDER BY revenue DESC
    """)
    print(f'\n   {"Ниша":<25} {"orders":>8} {"revenue, млн":>14} {"с выкупом, млн":>16} {"выкуп %":>8}')
    print(f'   {"-"*75}')
    for row in cur2.fetchall():
        name, orders, revenue, rwb, ap, upd = row
        rev = float(revenue or 0) / 1e6
        rwb_v = float(rwb or 0) / 1e6
        pct = rwb_v / rev * 100 if rev else 0
        print(f'   {name:<25} {int(orders or 0):>8,} {rev:>14,.1f} {rwb_v:>16,.1f} {pct:>7.0f}%')
    cur2.close()
    conn2.close()


if __name__ == '__main__':
    main()
