import os
import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('MPSTATS_TOKEN')
DB = os.getenv('DATABASE_URL')

print("Загружаем все ниши из MPStats...")

r = requests.get(
    'https://mpstats.io/api/wb/get/subjects/select',
    headers={'X-Mpstats-TOKEN': TOKEN, 'Content-Type': 'application/json'},
    params={'fbs': 1},
    json={'startRow': 0, 'endRow': 9999, 'filterModel': {}, 'sortModel': []},
    timeout=60
)

data = r.json()
print(f"Получено ниш: {len(data)}")

loaded = 0
errors = 0

for row in data:
    conn = psycopg2.connect(DB)
    cursor = conn.cursor()
    try:
        name = row.get('name', '')
        if not name:
            conn.close()
            continue
        cursor.execute("""
            INSERT INTO niches (
                name, revenue, products, products_with_sales,
                sellers, sellers_with_sales, buyout_pct, turnover,
                profit_pct, avg_rating, lost_revenue_pct, avg_price,
                commission, orders
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (name) DO UPDATE SET
                revenue = EXCLUDED.revenue,
                products = EXCLUDED.products,
                products_with_sales = EXCLUDED.products_with_sales,
                sellers = EXCLUDED.sellers,
                sellers_with_sales = EXCLUDED.sellers_with_sales,
                buyout_pct = EXCLUDED.buyout_pct,
                turnover = EXCLUDED.turnover,
                avg_rating = EXCLUDED.avg_rating,
                lost_revenue_pct = EXCLUDED.lost_revenue_pct,
                avg_price = EXCLUDED.avg_price,
                commission = EXCLUDED.commission,
                orders = EXCLUDED.orders
        """, (
            name,
            float(row.get('revenue', 0) or 0),
            int(row.get('items', 0) or 0),
            int(row.get('items_with_sells', 0) or 0),
            int(row.get('suppliers', 0) or 0),
            int(row.get('suppliers_with_sells', 0) or 0),
            float(row.get('purchase', 0) or 0) / 100,
            float(row.get('turnover_days', 0) or 0),
            0,
            float(row.get('rating_average', 0) or 0),
            float(row.get('lost_profit_percent', 0) or 0) / 100,
            float(row.get('final_price_median', 0) or 0),
            float(row.get('commision_fbo', 0) or 0),
            int(row.get('orders_count', 0) or 0),
        ))
        conn.commit()
        loaded += 1
        if loaded % 100 == 0:
            print(f"Загружено: {loaded}...")
    except Exception as e:
        errors += 1
        if errors <= 3:
            print(f"Ошибка: {e}")
    finally:
        cursor.close()
        conn.close()

print(f"Готово! Загружено: {loaded}, ошибок: {errors}")
