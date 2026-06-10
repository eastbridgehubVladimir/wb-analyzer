"""
Скрипт исправления mpstats_path для ниш в production БД.

Стратегия:
  1. Собираем пути из уже корректных ниш → известный контекст для Claude
  2. Claude генерирует 3-5 кандидатов полного WB-пути для каждой «сломанной» ниши
  3. Каждый кандидат валидируется через MPStats API (≥20% keyword match)
  4. Лучший путь записывается в БД

Запуск:
  DATABASE_URL=... MPSTATS_TOKEN=... ANTHROPIC_API_KEY=... \\
  python scripts/fix_mpstats_paths.py --dry-run          # только показать
  python scripts/fix_mpstats_paths.py --limit 20         # первые 20 ниш
  python scripts/fix_mpstats_paths.py                    # всё

Зависимости (дополнительно к основным):
  pip install anthropic psycopg2-binary requests
"""
import os, sys, time, json, requests, re

DATABASE_URL  = os.getenv("DATABASE_URL", "")
MPSTATS_TOKEN = os.getenv("MPSTATS_TOKEN", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

DRY_RUN = "--dry-run" in sys.argv
LIMIT   = None
for i, arg in enumerate(sys.argv):
    if arg == "--limit" and i + 1 < len(sys.argv):
        LIMIT = int(sys.argv[i + 1])

from datetime import datetime, timedelta as _td
D2 = datetime.now().strftime('%Y-%m-%d')
D1 = (datetime.now() - _td(days=60)).strftime('%Y-%m-%d')
STOP_WORDS = {'для', 'при', 'под', 'над', 'без', 'про', 'все', 'или', 'типа', 'ниша'}


# ── MPStats API ────────────────────────────────────────────────────────────────

def mpstats_category(path: str, end_row: int = 30) -> list:
    try:
        r = requests.post(
            "https://mpstats.io/api/wb/get/category",
            headers={"X-Mpstats-TOKEN": MPSTATS_TOKEN, "Content-Type": "application/json"},
            params={"d1": D1, "d2": D2, "path": path},
            json={"startRow": 0, "endRow": end_row,
                  "sortModel": [{"colId": "revenue", "sort": "desc"}]},
            timeout=20,
        )
        if r.status_code == 200:
            return r.json().get("data", [])
        return []
    except Exception as e:
        print(f"    ⚠ MPStats error for {path!r}: {e}")
        return []


# ── Keyword match ──────────────────────────────────────────────────────────────

def keyword_match_pct(items: list, niche_name: str) -> float:
    words = [w for w in niche_name.lower().replace("-", " ").replace("/", " ").split()
             if len(w) > 3 and w not in STOP_WORDS]
    roots = [w[:5] for w in words if len(w) >= 5]
    if not words or not items:
        return 0.0
    sample = items[:20]
    matched = sum(
        1 for item in sample
        if any(kw in (item.get("name", "") or "").lower()
               for kw in words + roots)
    )
    return matched / len(sample)


# ── Claude path suggestion ─────────────────────────────────────────────────────

def claude_suggest_paths(niche_names: list[str], known_paths: list[str]) -> dict[str, list[str]]:
    """
    Для каждой ниши из niche_names предлагает 3-5 полных WB-путей.
    Возвращает {niche_name: [path1, path2, ...]}.
    """
    try:
        import anthropic
    except ImportError:
        print("  ⚠ Пакет 'anthropic' не найден. Установите: pip install anthropic")
        return {n: [] for n in niche_names}

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    # Берём уникальные родительские пути как контекст
    parent_paths = sorted(set(
        "/".join(p.split("/")[:-1]) for p in known_paths if "/" in p
    ))[:30]
    parents_str = "\n".join(f"  - {p}" for p in parent_paths)
    niches_json = json.dumps(niche_names, ensure_ascii=False)

    prompt = f"""Ты эксперт по структуре категорий Wildberries (и MPStats, который использует ту же иерархию).

Вот известные родительские пути из нашей базы:
{parents_str}

Примеры правильных полных путей:
  «Шуруповерты»   → «Для ремонта/Инструменты и оснастка/Сверление, долбление, закручивание/Шуруповерт»
  «Перфораторы»   → «Для ремонта/Инструменты и оснастка/Сверление, долбление, закручивание/Перфоратор»
  «Пылесосы»      → «Бытовая техника/Техника для дома/Пылесосы»
  «Гайковерты»    → «Автотовары/Инструменты/Автосервисное оборудование/Гайковерт»
  «Матрешки»      → «Детские игрушки и игры/Игрушки/Матрёшки» или «Сувениры/Матрешки»
  «Шляпы»         → «Женщинам/Аксессуары/Головные уборы/Шляпы»
  «Стремянки»     → «Для ремонта/Инструменты и оснастка/Стремянки и лестницы/Стремянка»
  «Таблетницы»    → «Красота и здоровье/Уход за собой/Таблетницы»

Для каждой ниши из списка предложи 3-5 наиболее вероятных ПОЛНЫХ путей.
Путь = полная иерархия через /, включая конечную категорию (название — единственное или множественное число как в WB).

Список ниш: {niches_json}

Верни ТОЛЬКО JSON-объект без markdown:
{{"Название ниши": ["путь1", "путь2", "путь3"], ...}}"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # strip markdown fences if present
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                stripped = part.strip().lstrip("json").strip()
                if stripped.startswith("{"):
                    raw = stripped
                    break
        result = json.loads(raw)
        # Нормализуем: только строки с "/"
        return {
            k: [p for p in v if isinstance(p, str) and "/" in p]
            for k, v in result.items()
            if isinstance(v, list)
        }
    except Exception as e:
        print(f"  ⚠ Claude error: {e}")
        print(f"  Raw response: {raw[:300] if 'raw' in dir() else '?'}")
        return {n: [] for n in niche_names}


# ── Core logic ─────────────────────────────────────────────────────────────────

def find_best_path(niche_name: str, claude_paths: list[str], current_path: str | None) -> tuple[str | None, float]:
    """
    Проверяет кандидатов в MPStats.
    Возвращает (лучший_путь, match_%) или (None, 0).
    """
    candidates = list(claude_paths)  # копия
    # Текущий путь из БД проверяем последним (он, вероятно, неправильный)
    if current_path and current_path not in candidates:
        candidates.append(current_path)

    if not candidates:
        return None, 0.0

    best_path, best_pct = None, 0.0
    for path in candidates:
        items = mpstats_category(path)
        if not items:
            print(f"    {path!r} → 0 товаров")
            time.sleep(0.3)
            continue
        pct = keyword_match_pct(items, niche_name)
        print(f"    {path!r} → {len(items)} тов., match={pct:.0%}")
        if pct > best_pct:
            best_pct = pct
            best_path = path
        if pct >= 0.5:
            break
        time.sleep(0.3)

    return (best_path, best_pct) if best_pct >= 0.20 else (None, 0.0)


# ── DB helpers ─────────────────────────────────────────────────────────────────

def ensure_path_verified_column(cur):
    """Добавляет path_verified BOOLEAN если колонки ещё нет."""
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name='niches' AND column_name='path_verified'
    """)
    if not cur.fetchone():
        print("  → Добавляем колонку path_verified в niches...")
        cur.execute("ALTER TABLE niches ADD COLUMN path_verified BOOLEAN DEFAULT NULL")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    try:
        import psycopg2
    except ImportError:
        print("Пакет 'psycopg2' не найден. Установите: pip install psycopg2-binary")
        sys.exit(1)

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    ensure_path_verified_column(cur)
    conn.commit()

    # ── Собираем «правильные» пути из БД (они будут контекстом для Claude) ──
    cur.execute("""
        SELECT DISTINCT mpstats_path FROM niches
        WHERE mpstats_path IS NOT NULL
          AND (path_verified IS TRUE OR path_verified IS NULL)
        LIMIT 500
    """)
    known_paths = [row[0] for row in cur.fetchall() if row[0]]
    print(f"Известных путей в БД: {len(known_paths)}")

    # ── Ниши без пути ──
    cur.execute("""
        SELECT name, mpstats_path, CAST(revenue AS FLOAT)
        FROM niches
        WHERE revenue IS NOT NULL AND mpstats_path IS NULL
        ORDER BY revenue DESC
    """)
    no_path = cur.fetchall()

    # ── Ниши с явно несовпадающим путём ──
    cur.execute("""
        SELECT name, mpstats_path, CAST(revenue AS FLOAT)
        FROM niches
        WHERE revenue IS NOT NULL AND mpstats_path IS NOT NULL
          AND (path_verified IS NULL OR path_verified = FALSE)
        ORDER BY revenue DESC
    """)
    unverified = []
    for name, path, rev in cur.fetchall():
        nw = set(w[:5] for w in name.lower().replace("/", " ").split() if len(w) >= 4)
        pw = set(w[:5] for w in path.lower().replace("/", " ").split() if len(w) >= 4)
        if not (nw & pw):
            unverified.append((name, path, rev))

    to_process = no_path + unverified
    if LIMIT:
        to_process = to_process[:LIMIT]

    print(f"\nК обработке: {len(to_process)} ниш")
    print(f"  Без пути:        {len(no_path)}")
    print(f"  Несовпадение:    {len(unverified)}")
    print(f"  DRY RUN: {DRY_RUN}\n")

    # ── Батчами по 10 спрашиваем Claude ──
    BATCH = 10
    all_suggestions: dict[str, list[str]] = {}
    names = [row[0] for row in to_process]
    for i in range(0, len(names), BATCH):
        batch = names[i:i+BATCH]
        print(f"Claude: батч {i//BATCH + 1} / {(len(names)-1)//BATCH + 1}  ({batch})")
        suggestions = claude_suggest_paths(batch, known_paths)
        all_suggestions.update(suggestions)
        time.sleep(0.5)  # уважаем rate-limit

    # ── Проверяем каждую нишу в MPStats ──
    fixed, failed = 0, 0
    for name, cur_path, rev in to_process:
        print(f"\n[{rev/1e6:.0f} млн] {name!r}  (текущий: {cur_path!r})")
        claude_paths = all_suggestions.get(name, [])
        print(f"  Claude: {claude_paths}")

        best_path, pct = find_best_path(name, claude_paths, cur_path)
        if best_path:
            changed = best_path != cur_path
            tag = "НОВЫЙ" if changed else "ПОДТВЕРЖДЁН"
            print(f"  ✅ {tag}: {best_path!r}  match={pct:.0%}")
            if not DRY_RUN:
                cur.execute(
                    "UPDATE niches SET mpstats_path=%s, path_verified=%s WHERE name=%s",
                    (best_path, pct >= 0.50, name),
                )
                conn.commit()
            fixed += 1
        else:
            print(f"  ❌ не нашли подходящего пути")
            if not DRY_RUN and cur_path:
                cur.execute(
                    "UPDATE niches SET path_verified=%s WHERE name=%s",
                    (False, name),
                )
                conn.commit()
            failed += 1

        time.sleep(0.5)

    cur.close()
    conn.close()

    print(f"\n{'='*60}")
    print(f"Исправлено: {fixed}  |  Не найдено: {failed}")
    if DRY_RUN:
        print("(DRY RUN — БД не изменялась)")


if __name__ == "__main__":
    missing = [n for v, n in [(DATABASE_URL, "DATABASE_URL"), (MPSTATS_TOKEN, "MPSTATS_TOKEN"), (ANTHROPIC_KEY, "ANTHROPIC_API_KEY")] if not v]
    if missing:
        print(f"Нет env-переменных: {', '.join(missing)}")
        sys.exit(1)
    main()
