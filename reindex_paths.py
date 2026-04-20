import os, psycopg2, requests
from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv('MPSTATS_TOKEN')
DB = os.getenv('DATABASE_URL')
headers = {'X-Mpstats-TOKEN': TOKEN, 'Content-Type': 'application/json'}

print("Загружаем категории MPStats...")
r = requests.get('https://mpstats.io/api/wb/get/categories', headers=headers, timeout=30)
cats = [c['path'] for c in r.json() if 'акци' not in c['path'].lower()]
print(f"Категорий (без акций): {len(cats)}")

def find_best_path(name, old_path):
    name_lower = name.lower()
    name_words = set(name_lower.split())
    candidates = []
    for path in cats:
        last = path.split('/')[-1].lower()
        # Точное совпадение последнего элемента
        if last == name_lower:
            candidates.append((100, path))
            continue
        # Все слова названия есть в пути
        path_lower = path.lower()
        if all(w in path_lower for w in name_words):
            score = 50 + len(name_words)
            candidates.append((score, path))
            continue
        # Первое слово названия совпадает с последним элементом пути
        first_word = name_lower.split()[0]
        if last == first_word or last.startswith(first_word):
            # Проверяем что старый путь и новый в одной категории
            old_top = old_path.split('/')[0].lower() if old_path else ''
            new_top = path.split('/')[0].lower()
            score = 20 + (10 if old_top == new_top else 0)
            candidates.append((score, path))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], len(x[1])))
    return candidates[0][1]

conn = psycopg2.connect(DB)
cur = conn.cursor()
cur.execute("""SELECT name, mpstats_path FROM niches 
WHERE mpstats_path IS NOT NULL 
AND LOWER(name) != LOWER(SPLIT_PART(mpstats_path, '/', -1))""")
niches = cur.fetchall()
print(f"Ниш для переиндексации: {len(niches)}")

fixed = 0
not_found = 0
for name, old_path in niches:
    best = find_best_path(name, old_path)
    if not best:
        not_found += 1
        continue
    if best != old_path:
        cur.execute("UPDATE niches SET mpstats_path = %s WHERE name = %s", (best, name))
        fixed += 1

conn.commit()
cur.close()
conn.close()
print(f"Исправлено: {fixed}")
print(f"Не найдено: {not_found}")
print("Готово!")
