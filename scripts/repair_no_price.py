"""
repair_no_price.py — Починить ниши у которых есть путь и выручка но нет цены.

Причина проблемы: для некоторых ниш (лекарства, автомобили, продукты питания)
MPStats /subjects/select возвращает final_price_median=0.
Решение: вызываем /api/wb/get/category для каждой ниши и вычисляем
avg_price, buyout_pct из топ-100 товаров напрямую.

Запуск:
    python scripts/repair_no_price.py
    python scripts/repair_no_price.py --dry-run
"""

import os, sys, time, psycopg2, requests, statistics
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
DB            = os.getenv('DATABASE_URL', '')
MPSTATS_TOKEN = os.getenv('MPSTATS_TOKEN', '')
DRY_RUN       = '--dry-run' in sys.argv

D2 = datetime.now().strftime('%Y-%m-%d')
D1 = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
DELAY = 0.4


def fetch_products(path: str, n: int = 100) -> list[dict]:
    """Возвращает топ-N товаров из категории MPStats."""
    try:
        r = requests.post(
            'https://mpstats.io/api/wb/get/category',
            headers={'X-Mpstats-TOKEN': MPSTATS_TOKEN, 'Content-Type': 'application/json'},
            params={'d1': D1, 'd2': D2, 'path': path},
            json={'startRow': 0, 'endRow': n,
                  'sortModel': [{'colId': 'revenue', 'sort': 'desc'}]},
            timeout=20,
        )
        if r.status_code == 200:
            return r.json().get('data', [])
        return []
    except Exception as e:
        print(f'    API error: {e}')
        return []


def aggregate(products: list[dict]) -> dict:
    """
    Вычисляет агрегаты по топ-товарам:
    avg_price       — средневзвешенная цена (по выручке)
    buyout_pct      — средневзвешенный процент выкупа
    sellers_with_sales — число уникальных продавцов
    """
    if not products:
        return {}

    prices    = [float(p.get('final_price', 0) or 0) for p in products]
    revenues  = [float(p.get('revenue', 0) or 0) for p in products]
    buyouts   = [float(p.get('buyout_percent', 0) or 0) / 100 for p in products]
    sellers   = {p.get('seller_id') or p.get('brand', '') for p in products}

    total_rev = sum(revenues)
    if total_rev > 0:
        weighted_price  = sum(p * r for p, r in zip(prices, revenues)) / total_rev
        weighted_buyout = sum(b * r for b, r in zip(buyouts, revenues)) / total_rev
    else:
        valid_prices = [p for p in prices if p > 0]
        weighted_price  = statistics.median(valid_prices) if valid_prices else 0
        weighted_buyout = statistics.mean([b for b in buyouts if b > 0]) if any(buyouts) else 0

    return {
        'avg_price':          round(weighted_price, 2),
        'buyout_pct':         round(weighted_buyout, 4),
        'sellers_with_sales': len(sellers),
    }


def main():
    if not DB or not MPSTATS_TOKEN:
        print('Нет DATABASE_URL или MPSTATS_TOKEN')
        sys.exit(1)

    conn = psycopg2.connect(DB)
    cur  = conn.cursor()

    # Ниши у которых есть путь + выручка, но нет цены
    cur.execute("""
        SELECT name, COALESCE(display_name, name), mpstats_path,
               CAST(revenue AS FLOAT), avg_price
        FROM niches
        WHERE mpstats_path IS NOT NULL
          AND (revenue IS NOT NULL AND revenue > 0)
          AND (avg_price IS NULL OR avg_price = 0)
        ORDER BY revenue DESC
    """)
    targets = cur.fetchall()
    print(f'К обработке: {len(targets)} ниш с путём + выручкой но без цены\n')

    fixed = 0
    for name, display, path, revenue, _ in targets:
        print(f'  [{revenue/1e6:>8.0f}M] {display[:40]}')
        print(f'    path: {path}')

        products = fetch_products(path)
        time.sleep(DELAY)

        if not products:
            print(f'    ⚠ нет товаров в категории')
            continue

        agg = aggregate(products)
        if not agg or agg['avg_price'] == 0:
            print(f'    ⚠ не удалось вычислить цену из {len(products)} товаров')
            continue

        print(f'    ✅ товаров: {len(products)}, avg_price={agg["avg_price"]:,.0f}₽, '
              f'buyout_pct={agg["buyout_pct"]:.0%}')

        if not DRY_RUN:
            cur.execute("""
                UPDATE niches SET
                    avg_price          = %s,
                    buyout_pct         = CASE WHEN buyout_pct = 0 THEN %s ELSE buyout_pct END,
                    sellers_with_sales = CASE WHEN sellers_with_sales = 0 THEN %s ELSE sellers_with_sales END
                WHERE name = %s
            """, (agg['avg_price'], agg['buyout_pct'], agg['sellers_with_sales'], name))
            conn.commit()
        fixed += 1

    cur.close()
    conn.close()

    print(f'\n{"═"*55}')
    print(f'  Исправлено: {fixed} / {len(targets)}')
    if DRY_RUN:
        print('  (DRY RUN)')
    print(f'\n  Следующий шаг:')
    print(f'    python scripts/check_quality.py')
    print()


if __name__ == '__main__':
    main()
