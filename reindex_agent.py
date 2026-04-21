import os, psycopg2, requests, anthropic, time
from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv("MPSTATS_TOKEN")
DB = os.getenv("DATABASE_URL")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

headers = {"X-Mpstats-TOKEN": TOKEN, "Content-Type": "application/json"}
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

print("Загружаем категории MPStats...")
r = requests.get("https://mpstats.io/api/wb/get/categories", headers=headers, timeout=30)
all_paths = [c["path"] for c in r.json() if "акци" not in c["path"].lower()]
print(f"Категорий: {len(all_paths)}")

def find_path_with_claude(niche_name, candidate_paths):
    if not candidate_paths:
        return None
    paths_text = "\n".join([f"{i+1}. {p}" for i, p in enumerate(candidate_paths[:30])])
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": f"""Ниша WB: "{niche_name}"\n\nВарианты путей MPStats:\n{paths_text}\n\nВыбери номер наиболее подходящего пути. Отвечай ТОЛЬКО цифрой. Если ни один не подходит — ответь 0."""
        }]
    )
    try:
        choice = int(message.content[0].text.strip())
        if 1 <= choice <= len(candidate_paths):
            return candidate_paths[choice - 1]
        return None
    except:
        return None

def get_candidates(niche_name, all_paths):
    first_word = niche_name.lower().split()[0]
    candidates = [p for p in all_paths if first_word in p.lower().split("/")[-1].lower()]
    if not candidates:
        candidates = [p for p in all_paths if first_word in p.lower()]
    return candidates[:30]

conn = psycopg2.connect(DB)
cur = conn.cursor()
cur.execute("""SELECT name, mpstats_path FROM niches 
WHERE mpstats_path IS NOT NULL 
AND LOWER(name) != LOWER(SPLIT_PART(mpstats_path, '/', -1))
ORDER BY revenue DESC NULLS LAST""")
niches = cur.fetchall()
print(f"Ниш для переиндексации: {len(niches)}")

fixed = 0
skipped = 0
errors = 0

for i, (name, old_path) in enumerate(niches):
    try:
        candidates = get_candidates(name, all_paths)
        if not candidates:
            skipped += 1
            continue
        new_path = find_path_with_claude(name, candidates)
        if new_path and new_path != old_path:
            cur.execute("UPDATE niches SET mpstats_path = %s WHERE name = %s", (new_path, name))
            conn.commit()
            fixed += 1
            print(f"[{i+1}/{len(niches)}] OK {name}: -> {new_path.split('/')[-1]}")
        else:
            skipped += 1
        time.sleep(0.5)
        if (i+1) % 50 == 0:
            print(f"Прогресс: {i+1}/{len(niches)} | Исправлено: {fixed} | Пропущено: {skipped}")
    except Exception as e:
        errors += 1
        print(f"[{i+1}] ОШИБКА {name}: {e}")
        time.sleep(2)

cur.close()
conn.close()
print(f"Готово! Исправлено: {fixed} | Пропущено: {skipped} | Ошибок: {errors}")
