"""
repair_missing_paths.py — Находит mpstats_path для 168 ниш без пути.

Стратегия (без Claude, быстро):
  1. Загружает все непромо-категории MPStats (~10к paths)
  2. Строит lookup-карту по нормализованным именам
  3. Для каждой ниши без пути пробует 6+ вариантов склонений
  4. Валидирует через MPStats API (total > 0)
  5. Выбирает кратчайший путь (минус промо-категории)
  6. Обновляет DB

Запуск:
  python scripts/repair_missing_paths.py
  python scripts/repair_missing_paths.py --dry-run
  python scripts/repair_missing_paths.py --limit 20
"""

import os, sys, time, re, requests, psycopg2
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

DB            = os.getenv('DATABASE_URL', '')
MPSTATS_TOKEN = os.getenv('MPSTATS_TOKEN', '')

DRY_RUN = '--dry-run' in sys.argv
LIMIT   = None
for i, a in enumerate(sys.argv):
    if a == '--limit' and i + 1 < len(sys.argv):
        LIMIT = int(sys.argv[i + 1])

D2 = datetime.now().strftime('%Y-%m-%d')
D1 = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')

PROMO_WORDS = {'акци', 'распродаж', 'подарк', 'новогодн', 'хит ', 'хиты', 'встречаем'}
API_DELAY   = 0.35


# ── Normalization helpers ──────────────────────────────────────────────────────

def normalize(name: str) -> list[str]:
    """
    Возвращает список кандидатов: исходное имя + варианты склонений.
    Русский язык: множ → един число, основные окончания.
    """
    n = name.lower().strip()
    variants = [n]

    # Словарь известных пар (расширенный)
    PAIRS = {
        # Инструменты
        'шляпы': 'шляпа', 'стремянки': 'стремянка', 'дрели': 'дрель',
        'сверла': 'сверло', 'матрешки': 'матрёшка', 'уровни строительные': 'уровень строительный',
        'уровни': 'уровень', 'струбцины': 'струбцина', 'ножовки': 'ножовка',
        'антресоли': 'антресоль', 'таблетницы': 'таблетница',
        # Автотовары
        'шарниры автомобильные': 'шарнир', 'турбонагнетатели': 'турбонагнетатель',
        'цилиндры автомобильные': 'цилиндр', 'кожухи автомобильные': 'кожух',
        'катализаторы автомобильные': 'катализатор',
        'отбойники автомобильные': 'отбойник', 'пускатели': 'пускатель',
        # Прочее
        'бетономешалки': 'бетономешалка', 'электрорубанки': 'электрорубанок',
        'припои': 'припой', 'матрешки': 'матрешка',
    }
    if n in PAIRS:
        variants.append(PAIRS[n])
    # Также проверяем первое слово многословного нейминга
    first_word = n.split()[0]
    if first_word in PAIRS:
        variants.append(PAIRS[first_word])

    # Общие окончания множественного → единственного
    replacements = [
        (r'ки$', 'ка'), (r'ги$', 'га'), (r'хи$', 'ха'), (r'ни$', 'нь'),
        (r'ли$', 'ль'), (r'зи$', 'зь'), (r'ри$', 'рь'),
        (r'ы$',  'а'),  (r'и$',  ''),   (r'а$',  ''),
        (r'ей$', 'ь'),  (r'ей$', 'й'),  (r'ов$', ''),
    ]
    for pattern, repl in replacements:
        candidate = re.sub(pattern, repl, n)
        if candidate != n and len(candidate) > 2:
            variants.append(candidate)

    # Root только длиной 8+ символов (избегаем ложных срабатываний)
    if len(n) >= 8:
        variants.append(n[:8])

    return list(dict.fromkeys(variants))  # уникальные, сохраняем порядок


def is_promo(path: str) -> bool:
    path_l = path.lower()
    return any(w in path_l for w in PROMO_WORDS)


# ── Build category lookup ──────────────────────────────────────────────────────

def build_cat_map(headers: dict) -> dict[str, list[str]]:
    """Загружает категории MPStats и строит нормализованный lookup."""
    print('[API] Загружаем категории MPStats...')
    r = requests.get('https://mpstats.io/api/wb/get/categories', headers=headers, timeout=30)
    r.raise_for_status()
    cats = r.json()
    print(f'[API] Категорий: {len(cats)}')

    cat_map: dict[str, list[str]] = {}
    for c in cats:
        path = c.get('path', '')
        if not path or is_promo(path):
            continue
        last = path.split('/')[-1].lower().strip()
        if last not in cat_map:
            cat_map[last] = []
        cat_map[last].append(path)

    return cat_map


# ── MPStats validation ─────────────────────────────────────────────────────────

def mpstats_total(path: str, headers: dict) -> int:
    try:
        r = requests.post(
            'https://mpstats.io/api/wb/get/category',
            headers=headers,
            params={'d1': D1, 'd2': D2, 'path': path},
            json={'startRow': 0, 'endRow': 1, 'sortModel': [{'colId': 'revenue', 'sort': 'desc'}]},
            timeout=15,
        )
        return r.json().get('total', 0) if r.status_code == 200 else 0
    except Exception:
        return 0


def find_best_path(niche_name: str, cat_map: dict, headers: dict) -> tuple[str | None, int]:
    """
    Возвращает (лучший путь, total) или (None, 0).
    Только точные совпадения по последнему сегменту пути — без опасных root-подстрок.
    """
    variants = normalize(niche_name)
    # Только первые варианты (exact + specific transformations), без root-вариантов
    # Root-варианты (len<=6 chars) используем ТОЛЬКО как PREFIX последнего сегмента
    candidates_raw = []

    for variant in variants:
        is_root = len(variant) <= 6 and variant not in [niche_name.lower()]

        if is_root:
            # Root: принимаем только если это ПРЕФИКС последнего сегмента
            for last, paths in cat_map.items():
                if last.startswith(variant) and last != variant:
                    for path in paths:
                        if path not in candidates_raw:
                            candidates_raw.append(path)
        else:
            # Полное совпадение с последним сегментом
            if variant in cat_map:
                for path in cat_map[variant]:
                    if path not in candidates_raw:
                        candidates_raw.append(path)

    # Сортируем: предпочитаем более конкретные (длинные) пути среди точных совпадений,
    # и менее конкретные (короткие) для root-совпадений
    exact_matches   = [p for p in candidates_raw if p.split('/')[-1].lower() in variants]
    root_matches    = [p for p in candidates_raw if p not in exact_matches]
    candidates = (
        sorted(exact_matches, key=lambda p: -len(p.split('/')))    # точные: глубокие первыми
        + sorted(root_matches, key=lambda p: len(p.split('/')))[:5] # root: короткие первыми
    )[:12]

    if not candidates:
        return None, 0

    for path in candidates:
        total = mpstats_total(path, headers)
        print(f'    {path[:65]:<65} → {total}')
        time.sleep(API_DELAY)
        if total > 0:
            return path, total

    return None, 0


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not DB or not MPSTATS_TOKEN:
        print('Нет DATABASE_URL или MPSTATS_TOKEN')
        sys.exit(1)

    headers = {'X-Mpstats-TOKEN': MPSTATS_TOKEN, 'Content-Type': 'application/json'}
    conn = psycopg2.connect(DB)
    cur  = conn.cursor()

    # Загружаем карту категорий
    cat_map = build_cat_map(headers)
    print(f'Уникальных сегментов в карте: {len(cat_map)}')

    # Ниши без пути (сортируем по выручке — важные первыми)
    cur.execute("""
        SELECT name, COALESCE(display_name, name), CAST(revenue AS FLOAT)
        FROM niches
        WHERE mpstats_path IS NULL
        ORDER BY revenue DESC NULLS LAST
    """)
    no_path = cur.fetchall()
    if LIMIT:
        no_path = no_path[:LIMIT]

    print(f'\nК обработке: {len(no_path)} ниш (DRY_RUN={DRY_RUN})\n')

    fixed, failed = 0, 0
    for name, display, rev in no_path:
        rev_m = (rev or 0) / 1e6
        print(f'[{rev_m:>7.0f}M] {display[:40]}')
        best_path, total = find_best_path(name, cat_map, headers)

        if best_path:
            print(f'  ✅ {best_path}  ({total} товаров)')
            if not DRY_RUN:
                cur.execute(
                    'UPDATE niches SET mpstats_path = %s WHERE name = %s',
                    (best_path, name),
                )
                conn.commit()
            fixed += 1
        else:
            print(f'  ❌ путь не найден')
            failed += 1

    cur.close()
    conn.close()

    print(f'\n{"═"*55}')
    print(f'  Найдено путей: {fixed}  |  Не найдено: {failed}')
    if DRY_RUN:
        print('  (DRY RUN — БД не изменялась)')
    if fixed > 0 and not DRY_RUN:
        print(f'\n  Следующий шаг:')
        print(f'    python scripts/repair_niches.py  ← загрузить данные для новых путей')
        print(f'    python scripts/check_quality.py  ← проверить результат')
    print()


if __name__ == '__main__':
    main()
