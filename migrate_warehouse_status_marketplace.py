"""
migrate_warehouse_status_marketplace.py — одноразовый (идемпотентный) скрипт:
добавляет колонку marketplace в warehouse_status, чтобы таблица могла хранить
не только склады Wildberries, но и склады Ozon (раньше находки по Ozon шли
только в справочное уведомление админу, в БД не попадали).

Существующие 28 строк — все WB, поэтому DEFAULT 'wb' им подходит без ручного
бэкофилла.

Запуск: python3 migrate_warehouse_status_marketplace.py
Идемпотентен: ADD COLUMN IF NOT EXISTS — повторный запуск ничего не ломает.
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

cur.execute("ALTER TABLE warehouse_status ADD COLUMN IF NOT EXISTS marketplace VARCHAR(10) NOT NULL DEFAULT 'wb';")
print("warehouse_status.marketplace — готово.")

cur.execute("SELECT marketplace, COUNT(*) FROM warehouse_status GROUP BY marketplace")
for row in cur.fetchall():
    print(f"  {row[0]!r}: {row[1]}")

cur.close()
conn.close()
