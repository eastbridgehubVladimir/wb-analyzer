"""
repair_dead_niches.py — Попытка оживить 250 ниш с нулевой выручкой.

Стратегия:
  Ниши с выручкой = 0 за 30 дней могут быть СЕЗОННЫМИ.
  Проверяем 6-месячный диапазон через /api/wb/get/category.
  Если есть товары за 6 мес → ниша живая, обновляем данные.
  Если нет → помечаем как DEAD (новое поле niche_status='dead').

  Дополнительно: нишам со статусом 'dead' UI должен показывать
  "данные по этой нише временно недоступны" (Идея 5).

Запуск:
  python scripts/repair_dead_niches.py              # 6-мес диапазон
  python scripts/repair_dead_niches.py --months 12  # 12-мес диапазон
  python scripts/repair_dead_niches.py --dry-run    # только анализ
"""

import os, sys, time, psycopg2, requests, statistics, argparse
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
DB            = os.getenv('DATABASE_URL', '')
MPSTATS_TOKEN = os.getenv('MPSTATS_TOKEN', '')

parser = argparse.ArgumentParser()
parser.add_argument('--dry-run',  action='store_true')
parser.add_argument('--months',   type=int, default=6)
args = parser.parse_args()

DRY_RUN = args.dry_run
MONTHS  = args.months
D2 = datetime.now().strftime('%Y-%m-%d')
D1 = (datetime.now() - timedelta(days=30 * MONTHS)).strftime('%Y-%m-%d')
DELAY = 0.4


def ensure_status_column(conn):
    """Добавляет niche_status если не существует."""
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name='niches' AND column_name='niche_status'
    """)
    if not cur.fetchone():
        print('[DB] Добавляем колонку niche_status...')
        cur.execute("ALTER TABLE niches ADD COLUMN niche_status VARCHAR(20) DEFAULT NULL")
        conn.commit()
    cur.close()


def fetch_category(path: str, n: int = 50) -> tuple[int, list[dict]]:
    """Возвращает (total, products)."""
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
            data = r.json()
            return data.get('total', 0), data.get('data', [])
        return 0, []
    except Exception as e:
        print(f'    API error: {e}')
        return 0, []


def aggregate_products(products: list[dict]) -> dict:
    """Вычисляет агрегаты из продуктовых данных."""
    revenues = [float(p.get('revenue', 0) or 0) for p in products]
    prices   = [float(p.get('final_price', 0) or 0) for p in products]
    buyouts  = [float(p.get('buyout_percent', 0) or 0) / 100 for p in products]

    total_rev = sum(revenues)
    if total_rev > 0:
        avg_price  = sum(p * r for p, r in zip(prices, revenues)) / total_rev
        avg_buyout = sum(b * r for b, r in zip(buyouts, revenues)) / total_rev
    else:
        valid_p = [p for p in prices if p > 0]
        avg_price  = statistics.median(valid_p) if valid_p else 0
        avg_buyout = 0.7  # fallback

    return {
        'revenue':    round(total_rev),
        'avg_price':  round(avg_price, 2),
        'buyout_pct': round(avg_buyout, 4),
    }


def main():
    if not DB or not MPSTATS_TOKEN:
        print('Нет DATABASE_URL или MPSTATS_TOKEN')
        sys.exit(1)

    conn = psycopg2.connect(DB)
    ensure_status_column(conn)
    cur = conn.cursor()

    # 250 ниш: есть путь, выручка = 0
    cur.execute("""
        SELECT name, COALESCE(display_name, name), mpstats_path
        FROM niches
        WHERE mpstats_path IS NOT NULL
          AND (revenue IS NULL OR revenue = 0)
        ORDER BY name
    """)
    targets = cur.fetchall()
    print(f'К обработке: {len(targets)} ниш с нулевой выручкой')
    print(f'Диапазон проверки: {D1} — {D2} ({MONTHS} мес.)\n')

    revived = 0
    confirmed_dead = 0

    for name, display, path in targets:
        total, products = fetch_category(path)
        time.sleep(DELAY)

        if total > 0 and products:
            agg = aggregate_products(products)
            tag = '✅ ЖИВАЯ'
            print(f'  {tag}  {display[:40]:<40}  total={total}, rev={agg["revenue"]/1e6:.1f}M')
            if not DRY_RUN:
                cur.execute("""
                    UPDATE niches SET
                        revenue    = %s,
                        avg_price  = CASE WHEN avg_price = 0 THEN %s ELSE avg_price END,
                        buyout_pct = CASE WHEN buyout_pct = 0 THEN %s ELSE buyout_pct END,
                        niche_status = 'seasonal'
                    WHERE name = %s
                """, (agg['revenue'], agg['avg_price'], agg['buyout_pct'], name))
                conn.commit()
            revived += 1
        else:
            print(f'  ❌ DEAD   {display[:40]}')
            if not DRY_RUN:
                cur.execute(
                    "UPDATE niches SET niche_status = 'dead' WHERE name = %s", (name,))
                conn.commit()
            confirmed_dead += 1

    cur.close()
    conn.close()

    print(f'\n{"═"*60}')
    print(f'  Оживлено (сезонные): {revived}')
    print(f'  Подтверждены DEAD:   {confirmed_dead}')
    if DRY_RUN:
        print('  (DRY RUN — БД не изменялась)')
    print()
    print('  Следующий шаг:')
    print('    python scripts/check_quality.py')
    print('    (мёртвые ниши с niche_status=dead надо исключить из UI)')


if __name__ == '__main__':
    main()
