"""
migrate_order_leads_payment_fields.py — одноразовый (идемпотентный) скрипт:
готовит order_leads (заявки с формы /order на сайте) к будущей оплате,
по образцу уже готовой таблицы orders (бот/miniapp).

- ADD COLUMN IF NOT EXISTS: amount, currency, payment_provider, payment_id, paid_at
- CHECK-ограничение на status — только после проверки, что оно не
  противоречит реально имеющимся данным (см. вывод шага 1 ниже)

Запуск: python3 migrate_order_leads_payment_fields.py
Идемпотентен: ADD COLUMN IF NOT EXISTS и проверка существования
constraint перед его добавлением — повторный запуск ничего не ломает.
"""
import os
import sys
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DB = os.getenv("DATABASE_URL")
if not DB:
    raise SystemExit("DATABASE_URL не найден в .env")

conn = psycopg2.connect(DB)
conn.autocommit = True
cur = conn.cursor()

print("=== Шаг 1: текущее распределение order_leads.status ===")
cur.execute("SELECT status, COUNT(*) FROM order_leads GROUP BY status ORDER BY status;")
rows = cur.fetchall()
for row in rows:
    print(f"  {row[0]!r}: {row[1]}")
existing_statuses = {row[0] for row in rows}

# Допустимый список = реально встречающиеся значения (чтобы не сломать
# существующие строки) + будущие платёжные состояния по образцу
# orders.payment_status (pending/paid/cancelled).
ALLOWED_STATUSES = sorted(existing_statuses | {'new', 'pending', 'paid', 'cancelled'})
print(f"\nДопустимый список для CHECK: {ALLOWED_STATUSES}")

unexpected = existing_statuses - {'new', 'pending', 'paid', 'cancelled'}
if unexpected:
    print(f"\n⚠ Неожиданные значения status, не входящие в ожидаемый набор: {unexpected}")
    print("Не меняю и не перезаписываю их — они включены в CHECK как есть.")

print("\n=== Шаг 2: добавление колонок (ADD COLUMN IF NOT EXISTS) ===")
cur.execute("ALTER TABLE order_leads ADD COLUMN IF NOT EXISTS amount INTEGER;")
cur.execute("ALTER TABLE order_leads ADD COLUMN IF NOT EXISTS currency VARCHAR(10) DEFAULT 'RUB';")
cur.execute("ALTER TABLE order_leads ADD COLUMN IF NOT EXISTS payment_provider VARCHAR(50);")
cur.execute("ALTER TABLE order_leads ADD COLUMN IF NOT EXISTS payment_id VARCHAR(100);")
cur.execute("ALTER TABLE order_leads ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP;")
print("  amount, currency, payment_provider, payment_id, paid_at — готово.")

print("\n=== Шаг 3: CHECK-ограничение на status ===")
cur.execute("""
    SELECT 1 FROM pg_constraint
    WHERE conname = 'order_leads_status_check'
      AND conrelid = 'order_leads'::regclass
""")
if cur.fetchone():
    print("  order_leads_status_check уже существует — пропускаю (идемпотентно).")
else:
    values_sql = ", ".join(f"'{s}'" for s in ALLOWED_STATUSES)
    cur.execute(f"""
        ALTER TABLE order_leads
        ADD CONSTRAINT order_leads_status_check
        CHECK (status IN ({values_sql}))
    """)
    print(f"  Добавлено: CHECK (status IN ({values_sql}))")

print("\n=== Итоговая схема order_leads ===")
cur.execute("""
    SELECT column_name, data_type, column_default
    FROM information_schema.columns
    WHERE table_name = 'order_leads'
    ORDER BY ordinal_position
""")
for row in cur.fetchall():
    print(f"  {row}")

cur.close()
conn.close()
print("\n[OK] order_leads готова к будущей оплате.")
