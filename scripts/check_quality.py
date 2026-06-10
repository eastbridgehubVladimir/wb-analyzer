"""
check_quality.py — Аудит качества всех ниш в БД.

Запуск:
    python scripts/check_quality.py              # только DB-проверка (быстро)
    python scripts/check_quality.py --api        # + выборочная проверка MPStats
    python scripts/check_quality.py --api --fix  # + помечает плохие пути в БД

Результат:
    - Вывод в консоль: полный отчёт по категориям проблем
    - quality_report.csv — список всех проблемных ниш с причинами
    - quality_ok.csv    — список "чистых" ниш готовых к монетизации
"""

import os, sys, csv, time, json, argparse, requests, psycopg2
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

DB            = os.getenv('DATABASE_URL')
MPSTATS_TOKEN = os.getenv('MPSTATS_TOKEN', '')
API_SAMPLE    = 50        # сколько ниш проверять через MPStats API
API_DELAY     = 0.4       # пауза между запросами (сек)


# ── Проблемные категории (могут комбинироваться) ───────────────────────────────
ISSUES = {
    'no_path':        'Нет mpstats_path — нельзя получить живые данные',
    'zero_revenue':   'Выручка = 0 — пустая ниша',
    'no_price':       'avg_price = 0 — нет данных о цене',
    'no_commission':  'commission = 0 — нет комиссии WB (влияет на юнит-экономику)',
    'no_buyout':      'buyout_pct = 0 — нет данных о выкупе (влияет на прогноз)',
    'no_profit':      'profit_pct = 0 — нет данных о маржинальности (влияет на финмодель)',
    'no_sellers':     'sellers_with_sales = 0 — нет активных продавцов',
    'api_empty':      'MPStats API вернул 0 товаров — путь нерабочий',
}

# Критические ошибки (PDF будет явно сломан)
CRITICAL = {'no_path', 'zero_revenue', 'no_price', 'api_empty'}


def db_audit(conn) -> tuple[list, list]:
    """Проверяет все ниши на уровне БД. Возвращает (проблемные, чистые)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT name, COALESCE(display_name, name),
               revenue, avg_price, commission, buyout_pct, profit_pct,
               sellers_with_sales, mpstats_path
        FROM niches
        ORDER BY revenue DESC NULLS LAST
    """)
    rows = cur.fetchall()
    cur.close()

    broken, ok = [], []
    for row in rows:
        name, display, revenue, avg_price, commission, buyout_pct, profit_pct, sws, path = row

        issues = []
        if not path:
            issues.append('no_path')
        if not revenue or revenue == 0:
            issues.append('zero_revenue')
        if not avg_price or avg_price == 0:
            issues.append('no_price')
        if not commission or commission == 0:
            issues.append('no_commission')
        if not buyout_pct or buyout_pct == 0:
            issues.append('no_buyout')
        if not profit_pct or profit_pct == 0:
            issues.append('no_profit')
        if not sws or sws == 0:
            issues.append('no_sellers')

        record = {
            'name': name,
            'display': display,
            'revenue': revenue or 0,
            'avg_price': avg_price or 0,
            'commission': commission or 0,
            'buyout_pct': buyout_pct or 0,
            'profit_pct': profit_pct or 0,
            'sellers_with_sales': sws or 0,
            'mpstats_path': path or '',
            'issues': issues,
            'is_critical': any(i in CRITICAL for i in issues),
        }

        if issues:
            broken.append(record)
        else:
            ok.append(record)

    return broken, ok


def api_check(niches: list, fix: bool, conn) -> list:
    """
    Проверяет выборку ниш через MPStats API.
    Возвращает список ниш у которых API вернул пустой результат.
    """
    if not MPSTATS_TOKEN:
        print('[API] MPSTATS_TOKEN не задан — пропускаем API-проверку')
        return []

    headers = {'X-Mpstats-TOKEN': MPSTATS_TOKEN, 'Content-Type': 'application/json'}
    d2 = datetime.now().strftime('%Y-%m-%d')
    d1 = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')

    bad_api = []
    cur = conn.cursor() if fix else None

    # Берём выборку: часть из "чистых" + все у кого не было no_path
    sample = [n for n in niches if n['mpstats_path']][:API_SAMPLE]
    print(f'\n[API] Проверяем {len(sample)} ниш через MPStats...')

    for i, niche in enumerate(sample):
        path = niche['mpstats_path']
        try:
            r = requests.post(
                'https://mpstats.io/api/wb/get/category',
                headers=headers,
                params={'d1': d1, 'd2': d2, 'path': path},
                json={'startRow': 0, 'endRow': 1, 'sortModel': [{'colId': 'revenue', 'sort': 'desc'}]},
                timeout=15,
            )
            total = r.json().get('total', 0)
            if total == 0:
                niche['issues'].append('api_empty')
                niche['is_critical'] = True
                bad_api.append(niche)
                status = 'ПУСТО'
                if fix and cur:
                    cur.execute('UPDATE niches SET mpstats_path = NULL WHERE name = %s', (niche['name'],))
                    conn.commit()
                    status += ' → путь обнулён'
            else:
                status = f'OK ({total} товаров)'
            print(f'  [{i+1:>3}/{len(sample)}] {niche["display"][:35]:<35} {status}')
        except Exception as e:
            print(f'  [{i+1:>3}/{len(sample)}] {niche["display"][:35]:<35} ОШИБКА: {e}')
        time.sleep(API_DELAY)

    if cur:
        cur.close()
    return bad_api


def print_report(broken: list, ok: list, api_bad: list, total: int):
    """Выводит подробный отчёт в консоль."""
    sep = '─' * 60
    print(f'\n{"═"*60}')
    print(f'  АУДИТ КАЧЕСТВА НИШ WBAnalyzer')
    print(f'  {datetime.now().strftime("%d.%m.%Y %H:%M")}')
    print(f'{"═"*60}')

    print(f'\n📊 ОБЩАЯ СТАТИСТИКА')
    print(sep)
    print(f'  Всего ниш в БД:           {total:>6}')
    print(f'  ✅ Готовы к продаже:       {len(ok):>6}  ({len(ok)/total*100:.1f}%)')
    print(f'  ❌ Проблемные:             {len(broken):>6}  ({len(broken)/total*100:.1f}%)')

    critical = [n for n in broken if n['is_critical']]
    warnings = [n for n in broken if not n['is_critical']]
    print(f'     ⛔ Критические ошибки:  {len(critical):>6}  (PDF будет сломан)')
    print(f'     ⚠️  Предупреждения:      {len(warnings):>6}  (PDF работает, но неполный)')

    print(f'\n🔍 РАЗБИВКА ПО ТИПАМ ПРОБЛЕМ')
    print(sep)
    issue_counts = {}
    for n in broken:
        for iss in n['issues']:
            issue_counts[iss] = issue_counts.get(iss, 0) + 1

    for issue_key, count in sorted(issue_counts.items(), key=lambda x: -x[1]):
        crit_mark = '⛔' if issue_key in CRITICAL else '⚠️ '
        print(f'  {crit_mark} {count:>4}  {ISSUES[issue_key]}')

    if api_bad:
        print(f'\n🌐 РЕЗУЛЬТАТЫ API-ПРОВЕРКИ')
        print(sep)
        print(f'  Плохих путей найдено: {len(api_bad)}')

    print(f'\n💡 ВЫВОД ДЛЯ МОНЕТИЗАЦИИ')
    print(sep)
    print(f'  Безопасно предлагать клиентам: {len(ok)} ниш')
    print(f'  Нужен ремонт данных:           {len(critical)} ниш (критические)')
    print(f'  Работает, но неполно:          {len(warnings)} ниш')
    print(f'\n  Рекомендация: при поиске ниши клиентом — проверять наличие')
    print(f'  в "чистом" списке ДО того как предлагать оплату (Идея 5).')
    print(f'{"═"*60}\n')


def save_csv(broken: list, ok: list):
    """Сохраняет CSV-отчёты."""
    # Проблемные
    with open('quality_report.csv', 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=[
            'display', 'name', 'revenue', 'avg_price', 'commission',
            'buyout_pct', 'profit_pct', 'sellers_with_sales',
            'mpstats_path', 'issues', 'is_critical',
        ])
        w.writeheader()
        for n in broken:
            row = dict(n)
            row['issues'] = ', '.join(n['issues'])
            row['revenue'] = f"{n['revenue']:,.0f}"
            row['avg_price'] = f"{n['avg_price']:,.0f}"
            row['commission'] = f"{n['commission']*100:.1f}%"
            row['buyout_pct'] = f"{n['buyout_pct']*100:.1f}%"
            row['profit_pct'] = f"{n['profit_pct']*100:.1f}%"
            w.writerow(row)
    print(f'  → quality_report.csv ({len(broken)} строк)')

    # Чистые
    with open('quality_ok.csv', 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=[
            'display', 'name', 'revenue', 'avg_price',
            'commission', 'buyout_pct', 'profit_pct', 'sellers_with_sales',
        ])
        w.writeheader()
        for n in ok:
            row = {
                'display': n['display'],
                'name': n['name'],
                'revenue': f"{n['revenue']:,.0f}",
                'avg_price': f"{n['avg_price']:,.0f}",
                'commission': f"{n['commission']*100:.1f}%",
                'buyout_pct': f"{n['buyout_pct']*100:.1f}%",
                'profit_pct': f"{n['profit_pct']*100:.1f}%",
                'sellers_with_sales': n['sellers_with_sales'],
            }
            w.writerow(row)
    print(f'  → quality_ok.csv ({len(ok)} строк — готовы к монетизации)')


def main():
    parser = argparse.ArgumentParser(description='Аудит качества ниш WBAnalyzer')
    parser.add_argument('--api',  action='store_true', help='Проверить выборку через MPStats API')
    parser.add_argument('--fix',  action='store_true', help='Обнулять плохие пути в БД (только с --api)')
    parser.add_argument('--sample', type=int, default=API_SAMPLE, help=f'Кол-во ниш для API-проверки (по умолч. {API_SAMPLE})')
    args = parser.parse_args()

    sample_size = args.sample

    print(f'[АУДИТ] Подключаемся к БД...')
    conn = psycopg2.connect(DB)

    # 1. DB-уровень (быстро)
    print(f'[АУДИТ] Проверяем все ниши на уровне БД...')
    broken, ok = db_audit(conn)
    total = len(broken) + len(ok)
    print(f'[АУДИТ] БД-проверка завершена: {total} ниш за мгновение')

    # 2. API-уровень (медленно, только по флагу)
    api_bad = []
    if args.api:
        # Берём выборку из "чистых" (чтобы убедиться что пути рабочие)
        sample_pool = ok + [n for n in broken if n['mpstats_path'] and 'api_empty' not in n['issues']]
        api_bad = api_check(sample_pool[:sample_size], args.fix, conn)
        # Перемещаем в broken если нашли новые плохие
        for bad in api_bad:
            if bad in ok:
                ok.remove(bad)
                broken.append(bad)

    conn.close()

    # 3. Отчёт
    print_report(broken, ok, api_bad, total)

    print('💾 Сохраняем CSV...')
    save_csv(broken, ok)
    print('\nГотово!')


if __name__ == '__main__':
    main()
