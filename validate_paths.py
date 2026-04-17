import requests, psycopg2, os, time
from dotenv import load_dotenv
load_dotenv()

token = os.getenv('MPSTATS_TOKEN')
DB = os.getenv('DATABASE_URL')
headers = {'X-Mpstats-TOKEN': token, 'Content-Type': 'application/json'}

conn = psycopg2.connect(DB)
cur = conn.cursor()
cur.execute('SELECT name, mpstats_path FROM niches WHERE mpstats_path IS NOT NULL ORDER BY revenue DESC')
rows = cur.fetchall()
print(f'Проверяем {len(rows)} путей...')

bad = 0
good = 0
for i, (name, path) in enumerate(rows):
    try:
        r = requests.post('https://mpstats.io/api/wb/get/category', headers=headers,
            params={'d1': '2026-03-01', 'd2': '2026-04-14', 'path': path},
            json={'startRow': 0, 'endRow': 1, 'sortModel': [{'colId': 'revenue', 'sort': 'desc'}]}, timeout=15)
        total = r.json().get('total', 0)
        if total == 0:
            cur.execute('UPDATE niches SET mpstats_path = NULL WHERE name = %s', (name,))
            conn.commit()
            bad += 1
            print(f'[{i+1}] BAD: {name} -> {path}')
        else:
            good += 1
        time.sleep(0.4)
    except Exception as e:
        print(f'Error: {name}: {e}')
        time.sleep(1)

conn.close()
print(f'Готово! Хороших: {good}, плохих (обнулено): {bad}')
