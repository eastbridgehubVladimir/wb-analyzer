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

def get_all_candidates(name):
    name_lower = name.lower()
    words = [w for w in name_lower.split() if len(w) > 2]
    roots = [w[:5] for w in words]
    scored = []
    for path in all_paths:
        path_lower = path.lower()
        last = path.split("/")[-1].lower()
        matches_in_path = sum(1 for r in roots if r in path_lower)
        matches_in_last = sum(1 for r in roots if r in last)
        if matches_in_path > 0:
            scored.append((matches_in_last * 10 + matches_in_path, path))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:20]]

def ask_claude(name, old_path, candidates):
    if not candidates:
        return None
    paths_text = "\n".join([f"{i+1}. {p}" for i, p in enumerate(candidates)])
    msg = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=10,
        messages=[{"role": "user", "content": f"""Ты эксперт по категориям Wildberries.

Ниша: "{name}"
Текущий путь: "{old_path}"

Варианты путей MPStats:
{paths_text}

Правила:
- Множественное/единственное число считается правильным (Электроды=Электрод)
- Путь правильный если ниша логически входит в категорию
- НЕТ только если категория совсем другая по смыслу

Выбери номер САМОГО подходящего пути.
Если текущий путь уже правильный или близкий — ответь 0.
Отвечай ТОЛЬКО цифрой."""}]
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
cur.execute("""SELECT name, mpstats_path, revenue FROM niches 
WHERE mpstats_path IS NOT NULL
AND (path_verified IS NULL OR path_verified = FALSE)
ORDER BY revenue DESC""")
rows = cur.fetchall()

really_bad = rows
print(f"Ниш для проверки: {len(really_bad)}")

fixed = 0
skipped = 0
errors = 0

for i, (name, old_path, revenue) in enumerate(really_bad):
    try:
        candidates = get_all_candidates(name)
        if not candidates:
            skipped += 1
            continue
        new_path = ask_claude(name, old_path, candidates)
        if new_path and new_path != old_path:
            cur.execute("UPDATE niches SET mpstats_path = %s, path_verified = TRUE WHERE name = %s", (new_path, name))
            conn.commit()
            fixed += 1
            print(f"[{i+1}] OK {name}: {old_path.split('/')[-1]} -> {new_path.split('/')[-1]}")
        else:
            # Помечаем как verified если Claude сказал 0 (путь правильный)
            cur.execute("UPDATE niches SET path_verified = TRUE WHERE name = %s", (name,))
            conn.commit()
            skipped += 1
            print(f"[{i+1}] OK {name}: оставляем {old_path.split('/')[-1]}")
        time.sleep(1.2)
        if (i+1) % 20 == 0:
            print(f"Прогресс: {i+1}/{len(really_bad)} | Исправлено: {fixed} | Пропущено: {skipped}")
    except Exception as e:
        errors += 1
        print(f"[{i+1}] ОШИБКА {name}: {e}")
        time.sleep(3)

cur.close()
conn.close()
print(f"Готово! Исправлено: {fixed} | Подтверждено: {skipped} | Ошибок: {errors}")
