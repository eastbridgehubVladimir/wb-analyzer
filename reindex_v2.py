import os, psycopg2, requests, time, anthropic
from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv("MPSTATS_TOKEN")
DB = os.getenv("DATABASE_URL")
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
headers = {"X-Mpstats-TOKEN": TOKEN, "Content-Type": "application/json"}

print("Загружаем категории MPStats...")
r = requests.get("https://mpstats.io/api/wb/get/categories", headers=headers, timeout=30)
all_paths = [c["path"] for c in r.json() if "акци" not in c["path"].lower()]
print(f"Категорий: {len(all_paths)}")

def get_candidates(name):
    name_lower = name.lower()
    words = [w for w in name_lower.split() if len(w) > 3]
    if not words:
        words = name_lower.split()

    scored = []
    for path in all_paths:
        path_lower = path.lower()
        last = path.split("/")[-1].lower()
        
        # Считаем сколько слов из названия есть в пути
        matches_in_path = sum(1 for w in words if w in path_lower)
        matches_in_last = sum(1 for w in words if w in last)
        
        if matches_in_path > 0:
            # Приоритет: совпадения в последнем элементе важнее
            score = matches_in_last * 10 + matches_in_path
            scored.append((score, path))
    
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:10]]

def ask_claude(name, candidates):
    if not candidates:
        return None
    paths_text = "\n".join([f"{i+1}. {p}" for i, p in enumerate(candidates)])
    msg = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=5,
        messages=[{"role": "user", "content": f"Ниша WB: \"{name}\"\n\nПути MPStats:\n{paths_text}\n\nНомер САМОГО подходящего пути (только цифра, 0 если нет):"}]
    )
    try:
        choice = int(msg.content[0].text.strip())
        if 1 <= choice <= len(candidates):
            return candidates[choice - 1]
        return None
    except:
        return None

conn = psycopg2.connect(DB)
cur = conn.cursor()
cur.execute("""SELECT name, mpstats_path FROM niches 
WHERE mpstats_path IS NOT NULL 
AND LOWER(name) != LOWER(SPLIT_PART(mpstats_path, '/', -1))
AND (path_verified IS NULL OR path_verified = FALSE)
ORDER BY revenue DESC NULLS LAST""")
niches = cur.fetchall()
print(f"Ниш для переиндексации: {len(niches)}")

fixed = 0
skipped = 0
errors = 0

for i, (name, old_path) in enumerate(niches):
    try:
        candidates = get_candidates(name)
        if not candidates:
            skipped += 1
            continue
        new_path = ask_claude(name, candidates)
        if new_path and new_path != old_path:
            cur.execute("UPDATE niches SET mpstats_path = %s WHERE name = %s", (new_path, name))
            conn.commit()
            fixed += 1
            print(f"[{i+1}] OK {name}: {old_path.split('/')[-1]} -> {new_path.split('/')[-1]}")
        else:
            skipped += 1
        time.sleep(1.2)
        if (i+1) % 50 == 0:
            print(f"Прогресс: {i+1}/{len(niches)} | Исправлено: {fixed} | Пропущено: {skipped} | Ошибок: {errors}")
    except Exception as e:
        errors += 1
        print(f"[{i+1}] ОШИБКА {name}: {e}")
        time.sleep(3)

cur.close()
conn.close()
print(f"Готово! Исправлено: {fixed} | Пропущено: {skipped} | Ошибок: {errors}")
