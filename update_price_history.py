"""
Накопление истории цен и рейтингов НАШИХ товаров через WB Supplier API.

Источники (не требуют прокси, не блокируются):
  - content-api.wildberries.ru   → список наших nm_ids + названия/бренды/категории
  - statistics-api.wildberries.ru → реальные цены из заказов + скидки

Конкуренты (не реализовано): WB search.wb.ru и card.wb.ru блокируют
серверные запросы (429/404 без браузера). Для конкурентов нужны
либо residential прокси, либо активная подписка MPStats.

Записывает снимки в таблицу price_history (PostgreSQL).

ЗАПУСК ТЕСТА:
    cd ~/wb-saas && venv311/bin/python3 update_price_history.py --days 30

ПРОВЕРКА РЕЗУЛЬТАТА:
    venv311/bin/python3 update_price_history.py --check
"""

import argparse
import os
import json
import requests
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

DB     = os.getenv('DATABASE_URL')
WB_KEY = os.getenv('WB_API_KEY')

WB_HEADERS = {
    'Authorization': WB_KEY,
    'Accept': 'application/json',
    'Content-Type': 'application/json',
}


def get_our_cards() -> dict[int, dict]:
    """
    Возвращает словарь {nm_id: {name, brand, subject}} из WB Content API.
    Content API отдаёт все наши карточки (наименование, бренд, категория).
    """
    cursor = {}
    cards = {}
    while True:
        payload = {
            "settings": {
                "cursor": {**cursor, "limit": 100},
                "filter": {"withPhoto": -1}
            }
        }
        r = requests.post(
            'https://content-api.wildberries.ru/content/v2/get/cards/list',
            headers=WB_HEADERS, json=payload, timeout=30
        )
        if r.status_code != 200:
            print(f'  [content] HTTP {r.status_code}: {r.text[:200]}')
            break
        data  = r.json()
        batch = data.get('cards') or []
        for c in batch:
            nm = c.get('nmID')
            if nm:
                cards[nm] = {
                    'name':    (c.get('title') or '')[:200],
                    'brand':   (c.get('brand') or '')[:100],
                    'subject': (c.get('subjectName') or '')[:100],
                }
        cur = data.get('cursor') or {}
        if not cur.get('updatedAt') or len(batch) < 100:
            break
        cursor = {'updatedAt': cur['updatedAt'], 'nmID': cur.get('nmID')}
    return cards


def get_prices_from_orders(days: int = 90) -> dict[int, dict]:
    """
    Извлекает актуальные цены из заказов за последние N дней (Statistics API).
    Берём самый свежий заказ на каждый nm_id как «текущую» цену.

    Поля: totalPrice, discountPercent, priceWithDisc, finishedPrice.
    """
    date_from = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT00:00:00')
    r = requests.get(
        f'https://statistics-api.wildberries.ru/api/v1/supplier/orders?dateFrom={date_from}',
        headers=WB_HEADERS, timeout=30
    )
    if r.status_code != 200:
        print(f'  [orders] HTTP {r.status_code}: {r.text[:200]}')
        return {}

    orders = r.json() or []
    prices = {}
    for o in orders:
        nm = o.get('nmId')
        if not nm:
            continue
        date_str = o.get('date', '')
        # Обновляем только если запись новее (берём последний заказ = самая свежая цена)
        if nm not in prices or date_str > prices[nm].get('_date', ''):
            base_price    = float(o.get('totalPrice', 0) or 0)
            disc_pct      = int(o.get('discountPercent', 0) or 0)
            price_w_disc  = float(o.get('priceWithDisc', 0) or 0)

            prices[nm] = {
                'price':        price_w_disc or round(base_price * (1 - disc_pct / 100), 2),
                'old_price':    base_price if disc_pct > 0 else None,
                'discount_pct': disc_pct,
                '_date':        date_str,
            }
    return prices


def get_prices_from_sales(days: int = 90) -> dict[int, dict]:
    """
    Дополнительный источник цен — отчёт о продажах (finishedPrice = итоговая).
    Используется для nm_ids, которых нет в orders.
    """
    date_from = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT00:00:00')
    r = requests.get(
        f'https://statistics-api.wildberries.ru/api/v1/supplier/sales?dateFrom={date_from}',
        headers=WB_HEADERS, timeout=30
    )
    if r.status_code != 200:
        return {}

    sales  = r.json() or []
    prices = {}
    for s in sales:
        nm = s.get('nmId')
        if not nm:
            continue
        date_str = s.get('date', '')
        if nm not in prices or date_str > prices[nm].get('_date', ''):
            base  = float(s.get('totalPrice', 0) or 0)
            disc  = int(s.get('discountPercent', 0) or 0)
            final = float(s.get('finishedPrice', 0) or 0)
            prices[nm] = {
                'price':        final or round(base * (1 - disc / 100), 2),
                'old_price':    base if disc > 0 else None,
                'discount_pct': disc,
                '_date':        date_str,
            }
    return prices


def save_to_db(rows: list[dict]) -> int:
    """INSERT снимков в price_history."""
    if not rows:
        return 0
    conn = psycopg2.connect(DB)
    cur  = conn.cursor()
    data = [
        (
            r['nm_id'], r.get('niche_name'), r['name'], r['brand'],
            r['price'], r.get('old_price'), r.get('discount_pct'),
            r.get('rating'), r.get('reviews_count'), r.get('stocks_total'),
        )
        for r in rows
    ]
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO price_history
            (nm_id, niche_name, name, brand, price, old_price,
             discount_pct, rating, reviews_count, stocks_total)
        VALUES %s
        """,
        data,
        page_size=500,
    )
    conn.commit()
    conn.close()
    return len(data)


def _run(days: int = 90) -> dict:
    print(f'\n{"═"*60}')
    print(f'  update_price_history  |  {datetime.now():%Y-%m-%d %H:%M}')
    print(f'  Период заказов: последние {days} дней')
    print(f'{"═"*60}\n')

    # 1. Получить карточки (название, бренд, категория)
    print('  [1/3] Content API → карточки товаров...')
    cards = get_our_cards()
    print(f'        Найдено карточек: {len(cards)}')

    # 2. Цены из заказов
    print('  [2/3] Orders API → цены из заказов...')
    order_prices = get_prices_from_orders(days=days)
    print(f'        nm_ids с ценами из orders: {len(order_prices)}')

    # 3. Цены из продаж (для остатка)
    print('  [3/3] Sales API → цены из продаж (дополнение)...')
    sale_prices = get_prices_from_sales(days=days)
    print(f'        nm_ids с ценами из sales: {len(sale_prices)}')

    # Собираем итоговые строки
    rows = []
    no_price = []
    for nm_id, meta in cards.items():
        price_data = order_prices.get(nm_id) or sale_prices.get(nm_id)
        if not price_data:
            no_price.append(nm_id)
            # Записываем карточку без цены (нет заказов за период)
            rows.append({
                'nm_id':        nm_id,
                'niche_name':   meta['subject'],
                'name':         meta['name'],
                'brand':        meta['brand'],
                'price':        None,
                'old_price':    None,
                'discount_pct': None,
                'rating':       None,
                'reviews_count': None,
                'stocks_total': None,
            })
        else:
            rows.append({
                'nm_id':        nm_id,
                'niche_name':   meta['subject'],
                'name':         meta['name'],
                'brand':        meta['brand'],
                'price':        price_data.get('price'),
                'old_price':    price_data.get('old_price'),
                'discount_pct': price_data.get('discount_pct'),
                'rating':       None,
                'reviews_count': None,
                'stocks_total': None,
            })

    print(f'\n  Итого строк для записи: {len(rows)}')
    if no_price:
        print(f'  Без цены (нет заказов за {days}д): {len(no_price)} nm_ids')

    inserted = save_to_db(rows)

    print(f'\n{"═"*60}')
    print(f'  ✅ Записано строк в price_history: {inserted}')
    print(f'{"═"*60}\n')
    return {'total': inserted, 'cards': len(cards)}


async def update_price_history(days: int = 90):
    """Async-совместимый вход — asyncio.run(update_price_history()) работает."""
    return _run(days=days)


def check_db():
    """Показать статистику из price_history."""
    conn = psycopg2.connect(DB)
    cur  = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM price_history")
    total = cur.fetchone()[0]

    cur.execute("SELECT MIN(snapshot_date), MAX(snapshot_date) FROM price_history")
    dates = cur.fetchone()

    cur.execute("""
        SELECT niche_name, COUNT(*) cnt
        FROM price_history
        GROUP BY niche_name
        ORDER BY cnt DESC
        LIMIT 10
    """)
    by_niche = cur.fetchall()

    cur.execute("""
        SELECT nm_id, name, brand, price, discount_pct, snapshot_date
        FROM price_history
        ORDER BY snapshot_date DESC
        LIMIT 5
    """)
    last5 = cur.fetchall()

    conn.close()

    print(f'\n{"═"*60}')
    print(f'  price_history — статистика')
    print(f'{"═"*60}')
    print(f'  Всего строк:   {total:,}')
    print(f'  Первый снимок: {dates[0]}')
    print(f'  Последний:     {dates[1]}')
    print(f'\n  По категориям (топ-10):')
    for n, c in by_niche:
        print(f'    {c:>4} строк — {n or "(без категории)"}')
    print(f'\n  Последние 5 записей:')
    print(f'  {"nm_id":>12}  {"name":30}  {"brand":15}  {"price":>8}  {"disc%":>5}  snapshot')
    for row in last5:
        nm, name, brand, price, disc, snap = row
        print(f'  {nm:>12}  {str(name or "")[:30]:30}  {str(brand or "")[:15]:15}  {str(price or "—"):>8}  {str(disc or ""):>5}  {snap}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Обновить price_history из WB Supplier API')
    parser.add_argument('--days',  type=int, default=90,
                        help='Период заказов для извлечения цен (default: 90 дней)')
    parser.add_argument('--check', action='store_true',
                        help='Только показать статистику из БД')
    args = parser.parse_args()

    if args.check:
        check_db()
    else:
        _run(days=args.days)
