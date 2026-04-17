import os
import psycopg2
import requests
import time
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('MPSTATS_TOKEN')
DB = os.getenv('DATABASE_URL')
headers = {'X-Mpstats-TOKEN': TOKEN, 'Content-Type': 'application/json'}

print("Шаг 1: Загружаем все ниши из MPStats...")
r = requests.get(
    'https://mpstats.io/api/wb/get/subjects/select',
    headers=headers,
    params={'fbs': 1},
    json={'startRow': 0, 'endRow': 9999, 'filterModel': {}, 'sortModel': []},
    timeout=60
)
data = r.json()
print(f"Получено ниш: {len(data)}")

print("Шаг 2: Загружаем список категорий MPStats для маппирования путей...")
r2 = requests.get('https://mpstats.io/api/wb/get/categories', headers=headers, timeout=30)
cats = r2.json()
print(f"Категорий: {len(cats)}")

# Строим словарь name -> list of paths
cat_map = {}
for c in cats:
    name = c['path'].split('/')[-1].lower()
    if name not in cat_map:
        cat_map[name] = []
    cat_map[name].append(c['path'])

EXCLUDE = ['акци', 'подарк', 'свадьб', 'бане', 'сауна']

def find_path_for_niche(name):
    key = name.lower()
    matches = [p for p in cat_map.get(key, []) if not any(ex in p.lower() for ex in EXCLUDE)]
    if not matches:
        soft = ['акци', 'бане', 'сауна']
        matches = [p for p in cat_map.get(key, []) if not any(ex in p.lower() for ex in soft)]
    if not matches:
        return None
    # Берём самый короткий путь
    matches.sort(key=lambda x: len(x.split('/')))
    return matches[0]

print("Шаг 3: Импортируем ниши и сохраняем пути...")
loaded = 0
errors = 0
paths_found = 0

conn = psycopg2.connect(DB)
cursor = conn.cursor()

for row in data:
    try:
        name = row.get('name', '')
        if not name:
            continue

        mpstats_path = find_path_for_niche(name)
        if mpstats_path:
            paths_found += 1

        cursor.execute("""
            INSERT INTO niches (
                name, revenue, products, products_with_sales,
                sellers, sellers_with_sales, buyout_pct, turnover,
                profit_pct, avg_rating, lost_revenue_pct, avg_price,
                commission, orders, mpstats_path
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                orders = EXCLUDED.orders,
                mpstats_path = COALESCE(niches.mpstats_path, EXCLUDED.mpstats_path)
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
            mpstats_path,
        ))
        loaded += 1
        if loaded % 500 == 0:
            conn.commit()
            print(f"Загружено: {loaded}, путей найдено: {paths_found}...")
    except Exception as e:
        errors += 1
        if errors <= 3:
            print(f"Ошибка: {e}")

conn.commit()
cursor.close()
conn.close()
print(f"Готово! Загружено: {loaded}, путей найдено: {paths_found}, ошибок: {errors}")
