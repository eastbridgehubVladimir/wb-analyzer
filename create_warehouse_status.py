"""
create_warehouse_status.py — одноразовый (идемпотентный) скрипт создания и
заполнения таблицы warehouse_status (актуальный статус складов WB) и
warehouse_monitor_log (лог запусков агента мониторинга warehouse_monitor.py).

Запуск: python3 create_warehouse_status.py

Идемпотентен: CREATE TABLE IF NOT EXISTS, сиды вставляются через
ON CONFLICT (name) DO NOTHING — повторный запуск ничего не дублирует
и не портит уже обновлённые вручную/агентом записи.
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DB = os.getenv("DATABASE_URL")
if not DB:
    raise SystemExit("DATABASE_URL не найден в .env")

conn = psycopg2.connect(DB)
conn.autocommit = True
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS warehouse_status (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    city VARCHAR(100),
    region VARCHAR(100),
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    status_note TEXT,
    attacked_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT NOW(),
    source_url TEXT
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS warehouse_monitor_log (
    id SERIAL PRIMARY KEY,
    run_at TIMESTAMP DEFAULT NOW(),
    found BOOLEAN NOT NULL DEFAULT FALSE,
    warehouse VARCHAR(100),
    status VARCHAR(20),
    summary TEXT,
    source_url TEXT,
    raw_response TEXT
);
""")

# (name, city, region, status, status_note)
ACTIVE_AND_LIMITED = [
    ('Казань',        'Казань',        'ПФО',      'active',  None),
    ('Самара',        'Самара',        'ПФО',      'active',  None),
    ('Екатеринбург',  'Екатеринбург',  'УФО',      'active',  None),
    ('Новосибирск',   'Новосибирск',   'СФО',      'active',  None),
    ('Смоленск',      'Смоленск',      'ЦФО',      'active',  'Для СНГ-поставщиков'),
    ('Коледино',      'Коледино (МО)', 'ЦФО',      'limited', 'Уточняйте актуальный статус перед отгрузкой'),
]

# (name, city, region, attacked_at, status_note)
ATTACKED = [
    ('Электросталь',   'Электросталь',  'ЦФО (Московская обл.)',      '2026-07-18', None),
    ('Котовск',        'Котовск',       'ЦФО (Тамбовская обл.)',      '2026-07-18', None),
    ('Подольск',       'Подольск',      'ЦФО (Московская обл.)',      '2026-07-20', 'Эвакуация'),
    ('Краснодар',      'Краснодар',     'ЮФО (Краснодарский край)',   '2026-07-22', None),
    ('Невинномысск',   'Невинномысск',  'СКФО (Ставропольский край)', '2026-07-22', None),
    ('Шушары',         'Санкт-Петербург', 'СЗФО',                     '2026-07-24', None),
    ('Новосаратовка',  'Ленинградская обл.', 'СЗФО',                  '2026-07-24', None),
    ('Симферополь',    'Симферополь',   'Крым',                       '2026-07-24', 'Эвакуация'),
    ('Сарапул',        'Сарапул',       'ПФО (Удмуртия)',             '2026-07-27', 'Эвакуация'),
    ('Рязань',         'Рязань',        'ЦФО (Рязанская обл.)',       '2026-07-29', 'Эвакуация'),
    ('Пенза',          'Пенза',         'ПФО (Пензенская обл.)',      '2026-07-30', None),
    ('Волгоград',      'Волгоград',     'ЮФО (Волгоградская обл.)',   '2026-07-31', None),
]

for name, city, region, status, note in ACTIVE_AND_LIMITED:
    cur.execute("""
        INSERT INTO warehouse_status (name, city, region, status, status_note)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (name) DO NOTHING
    """, (name, city, region, status, note))

for name, city, region, attacked_at, note in ATTACKED:
    cur.execute("""
        INSERT INTO warehouse_status (name, city, region, status, status_note, attacked_at)
        VALUES (%s, %s, %s, 'attacked', %s, %s)
        ON CONFLICT (name) DO NOTHING
    """, (name, city, region, note, attacked_at))

cur.execute("SELECT status, COUNT(*) FROM warehouse_status GROUP BY status ORDER BY status")
print("[warehouse_status] по статусам:")
for row in cur.fetchall():
    print(f"  {row[0]}: {row[1]}")

cur.execute("SELECT COUNT(*) FROM warehouse_status")
print(f"[warehouse_status] всего строк: {cur.fetchone()[0]}")

cur.close()
conn.close()
print("[OK] Таблицы warehouse_status и warehouse_monitor_log созданы/проверены, сиды загружены (идемпотентно).")
