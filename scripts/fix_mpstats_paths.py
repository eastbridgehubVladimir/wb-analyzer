"""
Скрипт исправления mpstats_path для ниш:
1. Находит ниши без пути (168 шт)
2. Находит ниши с явно неверным путём (ключевые слова не пересекаются)
3. Для каждой пробует имя ниши как путь в MPStats API
4. Обновляет БД если нашли рабочий путь

Запуск: python scripts/fix_mpstats_paths.py [--dry-run]
"""
import os, sys, time, json, requests, psycopg2
from datetime import date

DATABASE_URL = os.getenv("DATABASE_URL", "")
MPSTATS_TOKEN = os.getenv("MPSTATS_TOKEN", "")
DRY_RUN = "--dry-run" in sys.argv

STOP_WORDS = {'для', 'при', 'под', 'над', 'без', 'про', 'все', 'или', 'типа'}
D1, D2 = "2024-10-01", "2025-03-31"

def mpstats_category(path: str) -> list:
    """Запрашивает список товаров для категории в MPStats."""
    headers = {"X-Mpstats-TOKEN": MPSTATS_TOKEN, "Content-Type": "application/json"}
    try:
        r = requests.post(
            "https://mpstats.io/api/wb/get/category",
            headers=headers,
            params={"d1": D1, "d2": D2, "path": path},
            json={"startRow": 0, "endRow": 20, "sortModel": [{"colId": "revenue", "sort": "desc"}]},
            timeout=20,
        )
        if r.status_code == 200:
            return r.json().get("data", [])
    except Exception as e:
        print(f"    ⚠ MPStats error: {e}")
    return []


def keyword_match_pct(items: list, name_words: list, roots: list) -> float:
    """Доля товаров, чьё название содержит ключевые слова ниши."""
    if not name_words or not items:
        return 0.0
    sample = items[:20]
    matched = sum(
        1 for item in sample
        if any(kw in (item.get("name", "") or "").lower() for kw in name_words + roots)
    )
    return matched / len(sample)


def name_keywords(name: str):
    words = [w for w in name.lower().replace("-", " ").replace("/", " ").split()
             if len(w) > 3 and w not in STOP_WORDS]
    roots = [w[:5] for w in words if len(w) >= 5]
    return words, roots


def try_find_path(niche_name: str, current_path: str | None) -> tuple[str | None, float]:
    """Возвращает (лучший путь, % совпадения) или (None, 0) если ничего не нашли."""
    words, roots = name_keywords(niche_name)
    candidates = []

    if current_path:
        candidates.append(current_path)
    candidates.append(niche_name)
    if current_path:
        last_seg = current_path.split("/")[-1]
        if last_seg not in candidates:
            candidates.append(last_seg)

    best_path, best_pct = None, 0.0
    for path in candidates:
        items = mpstats_category(path)
        if not items:
            continue
        pct = keyword_match_pct(items, words, roots)
        print(f"    {path!r:60s}  items={len(items):3d}  match={pct:.0%}")
        if pct > best_pct:
            best_pct = pct
            best_path = path
        if pct >= 0.5:
            break
        time.sleep(0.3)

    return (best_path, best_pct) if best_pct >= 0.20 else (None, 0.0)


def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    # --- 1. Ниши без пути ---
    cur.execute("""
        SELECT name, CAST(revenue AS FLOAT)
        FROM niches
        WHERE revenue IS NOT NULL AND mpstats_path IS NULL
        ORDER BY revenue DESC
    """)
    no_path = cur.fetchall()
    print(f"\n=== НИШИ БЕЗ ПУТИ: {len(no_path)} шт ===")

    fixed_no_path, failed_no_path = 0, 0
    for name, rev in no_path:
        print(f"\n[{rev/1e6:.0f} млн] {name!r}")
        best_path, pct = try_find_path(name, None)
        if best_path:
            print(f"  ✅ НАШЛИ: {best_path!r} ({pct:.0%})")
            if not DRY_RUN:
                cur.execute(
                    "UPDATE niches SET mpstats_path=%s, path_verified=%s WHERE name=%s",
                    (best_path, pct >= 0.50, name)
                )
                conn.commit()
            fixed_no_path += 1
        else:
            print(f"  ❌ не нашли подходящего пути")
            failed_no_path += 1
        time.sleep(0.5)

    # --- 2. Ниши с явно неверным путём ---
    cur.execute("""
        SELECT name, mpstats_path, CAST(revenue AS FLOAT)
        FROM niches
        WHERE revenue IS NOT NULL AND mpstats_path IS NOT NULL
        ORDER BY revenue DESC
    """)
    all_with_path = cur.fetchall()

    mismatch = []
    for name, path, rev in all_with_path:
        nw = set(w for w in name.lower().replace("/"," ").split() if len(w) >= 4)
        pw = set(w for w in path.lower().replace("/"," ").split() if len(w) >= 4)
        # Проверяем корни слов
        nw_roots = set(w[:5] for w in nw)
        pw_roots = set(w[:5] for w in pw)
        if not (nw & pw) and not (nw_roots & pw_roots):
            mismatch.append((name, path, rev))

    mismatch.sort(key=lambda x: -x[2])
    print(f"\n\n=== НИШИ С НЕСОВПАДЕНИЕМ ПУТИ: {len(mismatch)} шт ===")

    fixed_mismatch, failed_mismatch = 0, 0
    for name, current_path, rev in mismatch:
        print(f"\n[{rev/1e6:.0f} млн] {name!r}")
        print(f"  Текущий путь: {current_path!r}")
        words, roots = name_keywords(name)
        if not words:
            print("  (нет ключевых слов для проверки)")
            continue
        # Сначала проверяем текущий путь
        items = mpstats_category(current_path)
        if items:
            pct = keyword_match_pct(items, words, roots)
            print(f"  Текущий путь: items={len(items)}, match={pct:.0%}")
            if pct >= 0.20:
                print("  ✓ Текущий путь оказался OK")
                if not DRY_RUN:
                    cur.execute("UPDATE niches SET path_verified=TRUE WHERE name=%s", (name,))
                    conn.commit()
                continue
        # Пробуем найти лучший путь
        best_path, best_pct = try_find_path(name, current_path)
        if best_path and best_path != current_path:
            print(f"  ✅ НОВЫЙ ПУТЬ: {best_path!r} ({best_pct:.0%})")
            if not DRY_RUN:
                cur.execute(
                    "UPDATE niches SET mpstats_path=%s, path_verified=%s WHERE name=%s",
                    (best_path, best_pct >= 0.50, name)
                )
                conn.commit()
            fixed_mismatch += 1
        else:
            print(f"  ❌ не нашли лучшего пути (текущий: {best_pct:.0%})")
            failed_mismatch += 1
        time.sleep(0.5)

    cur.close()
    conn.close()

    print(f"\n{'='*60}")
    print(f"БЕЗ ПУТИ:    исправлено {fixed_no_path}, не найдено {failed_no_path}")
    print(f"НЕСОВПАДЕНИЕ: исправлено {fixed_mismatch}, не найдено {failed_mismatch}")
    if DRY_RUN:
        print("(DRY RUN — БД не изменялась)")


if __name__ == "__main__":
    if not DATABASE_URL:
        print("Нет DATABASE_URL в окружении")
        sys.exit(1)
    if not MPSTATS_TOKEN:
        print("Нет MPSTATS_TOKEN в окружении")
        sys.exit(1)
    main()
