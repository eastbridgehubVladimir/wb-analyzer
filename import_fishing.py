import os, psycopg2, requests, time
from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv('MPSTATS_TOKEN')
DB = os.getenv('DATABASE_URL')
headers = {'X-Mpstats-TOKEN': TOKEN, 'Content-Type': 'application/json'}

paths = [
    ('Удилища', 'Спорт/Охота и рыбалка/Рыбалка/Удилища', 'Спорт / Удилища'),
    ('Спиннинги', 'Спорт/Охота и рыбалка/Рыбалка/Рыболовные аксессуары/Удилище', 'Спорт / Спиннинги'),
]

conn = psycopg2.connect(DB)
cur = conn.cursor()

for name, path, display in paths:
    print(f"Импортируем: {name}")
    try:
        r = requests.post(
            'https://mpstats.io/api/wb/get/category',
            headers=headers,
            params={'d1': '2024-04-01', 'd2': '2026-04-01', 'path': path},
            json={'startRow': 0, 'endRow': 100, 'sortModel': [{'colId': 'revenue', 'sort': 'desc'}]},
            timeout=60
        )
        data = r.json()
        items = data.get('data', [])
        if not items:
            print(f"  Нет данных")
            continue
        print(f"  Товаров: {len(items)}")
        revenues = [i.get('revenue', 0) or 0 for i in items]
        orders = [i.get('orders', 0) or 0 for i in items]
        sellers = len(set(i.get('seller', '') for i in items))
        buyout = sum(i.get('buyout_percent', 0) or 0 for i in items) / len(items) / 100
        avg_price = sum(i.get('final_price', 0) or 0 for i in items) / len(items)
        total_revenue = sum(revenues)
        total_orders = sum(orders)

        cur.execute("""
            INSERT INTO niches (name, mpstats_path, display_name, revenue, orders, sellers, buyout_pct, avg_price)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET 
                mpstats_path = %s, display_name = %s, revenue = %s, orders = %s,
                sellers = %s, buyout_pct = %s, avg_price = %s
        """, (name, path, display, total_revenue, total_orders, sellers, buyout, avg_price,
              path, display, total_revenue, total_orders, sellers, buyout, avg_price))
        conn.commit()
        print(f"  Добавлено! Выручка: {total_revenue:,.0f}")
    except Exception as e:
        print(f"  Ошибка: {e}")
    time.sleep(1)

cur.close()
conn.close()
print("Готово!")
