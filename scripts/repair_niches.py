"""
repair_niches.py — Массовое обновление данных ниш из MPStats.

Стратегия:
  1. Один bulk-запрос subjects/select → все ниши из MPStats (~7 500 записей)
  2. Обновляет в БД: revenue, avg_price, commission, buyout_pct,
     sellers_with_sales, orders, products и другие поля
  3. Для ниш с profit_pct=0 вычисляет приблизительное значение
     из avg_price и commission (та же формула что в _compute_finance)

После запуска ожидаемый результат:
  - Большинство из 523 предупреждений будут исправлены
  - Часть из 306 критических (zero_revenue с путём) будут исправлены
  - Остаток: 168 ниш без пути → запустите scripts/fix_mpstats_paths.py

Запуск:
  python scripts/repair_niches.py              # только проблемные ниши
  python scripts/repair_niches.py --all        # обновить все ниши
  python scripts/repair_niches.py --dry-run    # показать, без изменений
  python scripts/repair_niches.py --profit     # + пересчитать profit_pct
"""

import os, sys, time, psycopg2, requests
from dotenv import load_dotenv

load_dotenv()

DB            = os.getenv('DATABASE_URL', '')
MPSTATS_TOKEN = os.getenv('MPSTATS_TOKEN', '')

DRY_RUN    = '--dry-run' in sys.argv
UPDATE_ALL = '--all'     in sys.argv
FIX_PROFIT = '--profit'  in sys.argv


# ── Helpers ────────────────────────────────────────────────────────────────────

def fetch_subjects() -> list[dict]:
    """Один запрос — все ниши WB из MPStats."""
    print('[API] Загружаем subjects/select из MPStats...')
    headers = {'X-Mpstats-TOKEN': MPSTATS_TOKEN, 'Content-Type': 'application/json'}
    r = requests.get(
        'https://mpstats.io/api/wb/get/subjects/select',
        headers=headers,
        params={'fbs': 1},
        json={'startRow': 0, 'endRow': 9999, 'filterModel': {}, 'sortModel': []},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    print(f'[API] Получено {len(data)} ниш из MPStats')
    return data


def compute_profit_pct(avg_price: float, commission: float) -> float:
    """
    Приблизительная маржинальность — та же формула что в _compute_finance.
    cost=35%, stor=2%, wb_log=120₽, tax=6%.
    Возвращает margin в диапазоне 0-1.
    """
    if avg_price <= 0:
        return 0.0
    cost  = avg_price * 0.35
    stor  = avg_price * 0.02
    wb_c  = avg_price * commission
    wb_l  = 120.0
    tax   = avg_price * 0.06
    ret   = wb_l * 0.3 * 0.5          # buyout_pct ≈ 0.70 → return_rate=0.30
    profit = avg_price - cost - stor - wb_c - wb_l - tax - ret
    return max(0.0, round(profit / avg_price, 4))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not DB or not MPSTATS_TOKEN:
        print('Нет DATABASE_URL или MPSTATS_TOKEN в .env')
        sys.exit(1)

    conn = psycopg2.connect(DB)
    cur  = conn.cursor()

    # ── 1. Получаем все ниши из MPStats
    subjects = fetch_subjects()
    # Строим словарь name → row
    api_map: dict[str, dict] = {s['name']: s for s in subjects if s.get('name')}

    # ── 2. Отбираем ниши для обновления из БД
    if UPDATE_ALL:
        cur.execute("""
            SELECT name, mpstats_path, revenue, avg_price, commission,
                   buyout_pct, profit_pct, sellers_with_sales
            FROM niches
            ORDER BY revenue DESC NULLS LAST
        """)
    else:
        # Только проблемные (хотя бы одно поле = 0 / NULL)
        cur.execute("""
            SELECT name, mpstats_path, revenue, avg_price, commission,
                   buyout_pct, profit_pct, sellers_with_sales
            FROM niches
            WHERE (revenue       IS NULL OR revenue = 0)
               OR (avg_price     IS NULL OR avg_price = 0)
               OR (commission    IS NULL OR commission = 0)
               OR (buyout_pct    IS NULL OR buyout_pct = 0)
               OR (profit_pct    IS NULL OR profit_pct = 0)
               OR (sellers_with_sales IS NULL OR sellers_with_sales = 0)
            ORDER BY revenue DESC NULLS LAST
        """)
    db_rows = cur.fetchall()
    print(f'\n[DB] К обработке: {len(db_rows)} ниш '
          f'{"(все)" if UPDATE_ALL else "(только проблемные)"}')

    # ── 3. Обновляем
    updated = 0
    not_in_api = 0
    unchanged = 0

    for (name, path, db_rev, db_price, db_comm,
         db_buyout, db_profit, db_sws) in db_rows:

        row = api_map.get(name)
        if not row:
            not_in_api += 1
            continue

        new_revenue = float(row.get('revenue', 0) or 0)
        new_price   = float(row.get('final_price_median', 0) or 0)
        new_comm    = float(row.get('commision_fbo', 0) or 0)
        new_buyout  = float(row.get('purchase', 0) or 0) / 100
        new_sws     = int(row.get('suppliers_with_sells', 0) or 0)
        new_orders  = int(row.get('orders_count', 0) or 0)
        new_prods   = int(row.get('items', 0) or 0)
        new_prods_s = int(row.get('items_with_sells', 0) or 0)
        new_sellers = int(row.get('suppliers', 0) or 0)
        new_turn    = float(row.get('turnover_days', 0) or 0)
        new_lost    = float(row.get('lost_profit_percent', 0) or 0) / 100
        new_rating  = float(row.get('rating_average', 0) or 0)

        # profit_pct: вычисляем если попросили (--profit) или если было 0
        if FIX_PROFIT or not db_profit:
            new_profit = compute_profit_pct(new_price, new_comm)
        else:
            new_profit = float(db_profit or 0)

        if DRY_RUN:
            if new_revenue != float(db_rev or 0):
                print(f'  {name[:40]:<40}  revenue: {db_rev or 0:>12,.0f} → {new_revenue:>12,.0f}')
            updated += 1
            continue

        cur.execute("""
            UPDATE niches SET
                revenue               = %s,
                avg_price             = %s,
                commission            = %s,
                buyout_pct            = %s,
                profit_pct            = %s,
                sellers_with_sales    = %s,
                orders                = %s,
                products              = %s,
                products_with_sales   = %s,
                sellers               = %s,
                turnover              = %s,
                lost_revenue_pct      = %s,
                avg_rating            = %s
            WHERE name = %s
        """, (
            new_revenue, new_price, new_comm, new_buyout, new_profit,
            new_sws, new_orders, new_prods, new_prods_s, new_sellers,
            new_turn, new_lost, new_rating,
            name,
        ))
        updated += 1

        if updated % 200 == 0:
            conn.commit()
            print(f'  Обновлено: {updated}/{len(db_rows)}...')

    if not DRY_RUN:
        conn.commit()

    cur.close()
    conn.close()

    # ── 4. Итоги
    sep = '─' * 55
    print(f'\n{"═"*55}')
    print(f'  РЕЗУЛЬТАТ РЕМОНТА')
    print(f'{"═"*55}')
    print(sep)
    if DRY_RUN:
        print(f'  DRY RUN — БД не изменялась')
        print(f'  Изменений: {updated}')
    else:
        print(f'  ✅ Обновлено ниш:          {updated:>6}')
        print(f'  ⚠  Нет в API MPStats:      {not_in_api:>6}  (мёртвые ниши)')
    print(sep)
    print(f'\n  Следующий шаг:')
    print(f'    python scripts/check_quality.py     ← проверить результат')
    if not_in_api > 0:
        print(f'    python scripts/fix_mpstats_paths.py  ← найти пути для 168 ниш')
    print()


if __name__ == '__main__':
    main()
