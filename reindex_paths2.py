import os, psycopg2, requests
from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv('MPSTATS_TOKEN')
DB = os.getenv('DATABASE_URL')
headers = {'X-Mpstats-TOKEN': TOKEN, 'Content-Type': 'application/json'}

print("Загружаем категории MPStats...")
r = requests.get('https://mpstats.io/api/wb/get/categories', headers=headers, timeout=30)
cats = [c['path'] for c in r.json() if 'акци' not in c['path'].lower()]

def find_best_path(name, old_path):
    name_lower = name.lower()
    name_words = name_lower.split()
    first_word = name_words[0]
    other_words = name_words[1:] if len(name_words) > 1 else []
    candidates = []
    for path in cats:
        path_lower = path.lower()
        last = path.split('/')[-1].lower()
        # Последний элемент содержит первое слово названия
        if first_word not in last:
            continue
        # Считаем сколько слов из названия есть в полном пути
        score = sum(1 for w in other_words if w in path_lower)
        candidates.append((score, len(path), path))
    if not candidates:
        return None
    # Сортируем: больше совпадений слов, короче путь
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0][2]

conn = psycopg2.connect(DB)
cur = conn.cursor()
cur.execute("""SELECT name, mpstats_path FROM niches 
WHERE mpstats_path IS NOT NULL 
AND LOWER(name) != LOWER(SPLIT_PART(mpstats_path, '/', -1))""")
niches = cur.fetchall()
print(f"Ниш для переиндексации: {len(niches)}")

fixed = 0
not_found = 0
examples = []
for name, old_path in niches:
    best = find_best_path(name, old_path)
    if not best:
        not_found += 1
        continue
    if best != old_path:
        if len(examples) < 10:
            examples.append(f"{name}: {old_path} -> {best}")
        cur.execute("UPDATE niches SET mpstats_path = %s WHERE name = %s", (best, name))
        fixed += 1

conn.commit()
cur.close()
conn.close()

print(f"Исправлено: {fixed}")
print(f"Не найдено: {not_found}")
print("\nПримеры исправлений:")
for e in examples:
    print(" ", e)
print("Готово!")
