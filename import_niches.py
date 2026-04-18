import openpyxl
import psycopg2

print("Читаем таблицу с нишами...")

wb = openpyxl.load_workbook(
    "Шаблон_Анализ_ниш_ _упущенная_выручка_New.xlsx",
    read_only=True, data_only=True
)

ws = wb["Свод"]
rows = list(ws.iter_rows(min_row=4, values_only=True))
print(f"Строк с данными: {len(rows)}")

conn = psycopg2.connect("postgresql://user@localhost:5432/wb_saas")
cursor = conn.cursor()

count = 0
skipped = 0
for row in rows:
    if not row[0]:
        continue
    try:
        cursor.execute("""
            UPDATE niches SET
                buyout_pct = %s,
                turnover = %s,
                profit = %s,
                profit_pct = %s,
                commission = %s,
                avg_comments = %s,
                avg_rating = %s,
                rank = %s,
                avg_price = %s
            WHERE name = %s
        """, (
            float(row[8]) if row[8] else None,   # % выкупа
            float(row[17]) if row[17] else None,  # оборачиваемость
            float(row[23]) if row[23] else None,  # прибыль
            float(row[24]) if row[24] else None,  # % прибыли
            float(row[27]) if row[27] else None,  # комиссия
            float(row[28]) if row[28] else None,  # комментарии
            float(row[29]) if row[29] else None,  # рейтинг
            int(row[41]) if row[41] else None,    # ранг
            float(row[19]) if row[19] else None,  # средняя цена
            str(row[0]),                           # название ниши
        ))
        if cursor.rowcount > 0:
            count += 1
        else:
            skipped += 1
    except Exception as e:
        skipped += 1
        continue

conn.commit()
cursor.close()
conn.close()

print(f"✓ Обновлено {count} ниш!")
print(f"  Пропущено: {skipped}")