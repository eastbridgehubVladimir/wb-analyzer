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
import time
import requests
import psycopg2
import psycopg2.extras
from datetime import date, datetime
from dotenv import load_dotenv

_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_DIR, '.env'))

TOKEN = os.getenv('MPSTATS_TOKEN')
DB = os.getenv('DATABASE_URL')

if not TOKEN or not DB:
    print('ОШИБКА: MPSTATS_TOKEN или DATABASE_URL не заданы в .env')
    sys.exit(1)


def get_niches_to_update(limit: int = 100) -> list[str]:
    """
    Умный порядок обновления:
    1. Ниши которые запрашивали пользователи за 14 дней (по данным telegram_leads)
    2. Ниши без data_updated_at или старше 30 дней — дополняем до limit
    """
    conn = psycopg2.connect(DB)
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT n.name
        FROM niches n
        JOIN telegram_leads tl ON LOWER(tl.niche_searched) ILIKE LOWER(n.name)
        WHERE tl.created_at > NOW() - INTERVAL '14 days'
        ORDER BY n.name
        LIMIT %s
    """, (limit,))
    priority = [row[0] for row in cur.fetchall()]

    remaining = limit - len(priority)
    if remaining > 0:
        exclude = tuple(priority) if priority else ('__none__',)
        cur.execute("""
            SELECT name FROM niches
            WHERE name NOT IN %s
            ORDER BY data_updated_at ASC NULLS FIRST
            LIMIT %s
        """, (exclude, remaining))
        priority.extend(row[0] for row in cur.fetchall())

    conn.close()
    print(f'Умный крон: будет обновлено {len(priority)} ниш (из них из telegram_leads: {len(priority)})')
    return priority

HEADERS = {'X-Mpstats-TOKEN': TOKEN, 'Content-Type': 'application/json'}
TODAY = date.today()
NOW = datetime.utcnow()

# MPStats отдаёт не больше 500 записей за один вызов /subjects/select
# (документированный лимит) — endRow:9999 в один запрос физически не может
# вернуть больше ~500 строк. Идём постранично, с паузой между страницами,
# чтобы не словить блокировку от анти-DDoS (QRATOR) при частых запросах.
BATCH_SIZE = 500
BATCH_PAUSE_SECONDS = 2.5


def fetch_all_niches():
    """
    Постранично тянет весь каталог /subjects/select по BATCH_SIZE записей.
    При HTTP 429 (квота MPStats на сегодня исчерпана) останавливается и
    возвращает то, что успела собрать до этого момента — частичный прогресс
    не выбрасывается, main() запишет в БД столько ниш, сколько реально пришло.
    """
    print('Запрашиваем MPStats /subjects/select (постранично, по 500 записей)...')
    all_rows = []
    start_row = 0
    batch_num = 0

    while True:
        batch_num += 1
        end_row = start_row + BATCH_SIZE - 1
        r = requests.get(
            'https://mpstats.io/api/wb/get/subjects/select',
            headers=HEADERS,
            params={'fbs': 1},
            json={'startRow': start_row, 'endRow': end_row, 'filterModel': {}, 'sortModel': []},
            timeout=60,
        )

        if r.status_code == 429:
            print(f'ОСТАНОВКА: MPStats вернул 429 (квота на сегодня исчерпана) на пачке {batch_num} '
                  f'(startRow={start_row}). Успели получить {len(all_rows)} ниш — пишем в БД только их, '
                  f'дальше в этом запуске не пытаемся.')
            break

        if r.status_code != 200:
            print(f'ОШИБКА HTTP {r.status_code}: {r.text[:300]}')
            sys.exit(1)

        page = r.json()
        all_rows.extend(page)
        print(f'  пачка {batch_num}: startRow={start_row}, endRow={end_row} -> '
              f'получено {len(page)} записей (всего собрано {len(all_rows)})')

        if len(page) < BATCH_SIZE:
            print('Конец списка — последняя пачка короче 500 записей.')
            break

        start_row += BATCH_SIZE
        time.sleep(BATCH_PAUSE_SECONDS)

    print(f'Итого получено ниш: {len(all_rows)} (выполнено пачек: {batch_num})')
    return all_rows


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

    if not rows:
        print('Нет ни одной строки для записи (первая же пачка упала на 429 или вернула пусто) — выходим без изменений в БД.')
        return

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
