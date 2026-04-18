import os
import time
import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('MPSTATS_TOKEN')
DB = os.getenv('DATABASE_URL')

headers = {
    'X-Mpstats-TOKEN': TOKEN,
    'Content-Type': 'application/json'
}

def get_niche_data(path):
    try:
        response = requests.post(
            'https://mpstats.io/api/wb/get/category',
            params={'d1': '2026-03-01', 'd2': '2026-03-31', 'path': path},
            headers=headers,
            json={'startRow': 0, 'endRow': 100, 'filterModel': {}, 'sortModel': [{'colId': 'revenue', 'sort': 'desc'}]},
            timeout=30
        )
        if response.status_code == 200:
            return response.json()
        else:
            print(f"Ошибка {response.status_code} для {path}")
            return None
    except Exception as e:
        print(f"Ошибка соединения: {e}")
        return None

def get_categories():
    response = requests.get(
        'https://mpstats.io/api/wb/get/categories',
        headers=headers,
        timeout=30
    )
    if response.status_code == 200:
        return response.json()
    return []

print("Загружаем список категорий...")
categories = get_categories()
print(f"Найдено категорий: {len(categories)}")

conn = psycopg2.connect(DB)
cursor = conn.cursor()

loaded = 0
for cat in categories[:50]:
    path = cat['path']
    name = cat['name']
    print(f"Загружаем: {path}")
    
    data = get_niche_data(path)
    if data and 'data' in data:
        for row in data['data'][:10]:
            try:
                cursor.execute("""
                    INSERT INTO niches (name, revenue, products, products_with_sales,
                    sellers, sellers_with_sales, buyout_pct, turnover, profit_pct, avg_rating)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (name) DO UPDATE SET
                    revenue=EXCLUDED.revenue,
                    products=EXCLUDED.products
                """, (
                    row.get('name', name),
                    row.get('revenue', 0),
                    row.get('products', 0),
                    row.get('products_with_sales', 0),
                    row.get('sellers', 0),
                    row.get('sellers_with_sales', 0),
                    row.get('buyout_percent', 0),
                    row.get('turnover', 0),
                    row.get('profit_percent', 0),
                    row.get('rating', 0),
                ))
                loaded += 1
            except Exception as e:
                print(f"Ошибка записи: {e}")
    
    conn.commit()
    time.sleep(0.5)

cursor.close()
conn.close()
print(f"Готово! Загружено записей: {loaded}")
