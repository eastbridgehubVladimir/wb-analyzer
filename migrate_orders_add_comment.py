"""
migrate_orders_add_comment.py — одноразовый (идемпотентный) скрипт:
добавляет колонку comment в orders (заказы из бота/miniapp), по образцу
уже имеющегося order_leads.comment (заявки с сайта).

Нужна для того, чтобы комментарий из miniapp (необязательное поле
"пожелания" на экране заказа) сохранялся в базу так же, как он уже
сохраняется для заказов с сайта — для будущей аналитики, а не терялся
после передачи в генерацию PDF.

Запуск: python3 migrate_orders_add_comment.py
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

cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS comment TEXT;")
print("orders.comment — готово.")

cur.close()
conn.close()
