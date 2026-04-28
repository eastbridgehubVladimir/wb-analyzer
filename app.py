from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import asyncio
import subprocess, sys

# Устанавливаем все зависимости при старте
def install(pkg):
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', pkg], stdout=subprocess.DEVNULL)

try:
    import psycopg2, requests, anthropic
    from dotenv import load_dotenv
except ImportError:
    print("Устанавливаем зависимости...")
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 
        'psycopg2-binary', 'requests', 'anthropic', 'python-dotenv'])
    import psycopg2, requests, anthropic
    from dotenv import load_dotenv
    print("Зависимости установлены!")

import requests as mpstats_req



import datetime
import sys
import os
load_dotenv()

os.chdir(os.path.dirname(os.path.abspath(__file__)))

DB = os.getenv("DATABASE_URL", "postgresql://user@localhost:5432/wb_saas")
MPSTATS_TOKEN = os.getenv("MPSTATS_TOKEN", "")

def clean_name(name):
    """Возвращает название ниши как есть."""
    if not name:
        return name
    return name

def get_category(name):
    """Возвращает категорию ниши."""
    if not name:
        return ""
    return name.split(' ', 1)[0]

def find_niche(query):
    conn = psycopg2.connect(DB)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT name, products, products_with_sales, sellers, sellers_with_sales,
               revenue, potential_revenue, lost_revenue, lost_revenue_pct, orders,
               buyout_pct, turnover, profit_pct, avg_rating, rank, commission, avg_price
        FROM niches
        WHERE (LOWER(name) LIKE LOWER(%s) OR LOWER(COALESCE(display_name,name)) LIKE LOWER(%s)) AND revenue IS NOT NULL
        ORDER BY CASE WHEN LOWER(name)=LOWER(%s) THEN 0 WHEN LOWER(name) LIKE LOWER(%s) THEN 1 ELSE 2 END, revenue DESC LIMIT 1
    """, (f"%{query}%", f"%{query}%", query, f"{query}%"))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row

def get_suggestions(query):
    """Возвращает список подсказок для автодополнения."""
    conn = psycopg2.connect(DB)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT name, revenue, COALESCE(display_name, name) as display_name FROM niches
        WHERE LOWER(name) LIKE LOWER(%s) AND revenue IS NOT NULL
        ORDER BY revenue DESC LIMIT 8
    """, (f"%{query}%",))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [{'name': r[2], 'full': r[0], 'revenue': float(r[1])} for r in rows]

def calculate_score(row):
    """Расчёт score: приоритет выкупу, прибыльности, оборачиваемости."""
    name, products, products_with_sales, sellers, sellers_with_sales, \
    revenue, potential_revenue, lost_revenue, lost_revenue_pct, orders, \
    buyout_pct, turnover, profit_pct, avg_rating, rank, commission, avg_price = row

    score = 0

    # 1. Выкуп (макс 25 очков) — главный показатель
    buyout = float(buyout_pct or 0)
    if buyout >= 0.85:
        score += 25
    elif buyout >= 0.75:
        score += 18
    elif buyout >= 0.60:
        score += 10
    elif buyout >= 0.45:
        score += 4
    else:
        score += 0

    # 2. Прибыльность (макс 25 очков) — главный показатель
    profit = float(profit_pct or 0)
    if profit >= 0.50:
        score += 25
    elif profit >= 0.35:
        score += 18
    elif profit >= 0.20:
        score += 10
    elif profit >= 0.10:
        score += 4
    else:
        score += 0

    # 3. Оборачиваемость (макс 20 очков) — чем быстрее тем лучше
    turn = float(turnover or 0)
    if turn <= 20:
        score += 20
    elif turn <= 40:
        score += 15
    elif turn <= 60:
        score += 10
    elif turn <= 90:
        score += 5
    elif turn <= 180:
        score += 2
    else:
        score += 0
    # Штраф за оборачиваемость более 180 дней
    if turn > 180:
        score = min(score, 55)

    # 4. Выручка на продавца с продажами (макс 20 очков)
    avg_rev = float(revenue or 0) / (sellers_with_sales or 1)
    if avg_rev >= 5_000_000:
        score += 20
    elif avg_rev >= 2_000_000:
        score += 15
    elif avg_rev >= 500_000:
        score += 8
    elif avg_rev >= 100_000:
        score += 3
    else:
        score += 0

    # 5. Упущенная выручка (макс 10 очков)
    lost_pct = float(lost_revenue_pct or 0)
    if lost_pct >= 30:
        score += 10
    elif lost_pct >= 15:
        score += 7
    elif lost_pct >= 5:
        score += 4
    else:
        score += 1

    # 6. Конкуренция (штраф по активным продавцам)
    active_sellers = int(sellers_with_sales or 0)
    if active_sellers < 50:
        score += 5
    elif active_sellers < 200:
        score += 0
    elif active_sellers < 500:
        score -= 5
    elif active_sellers < 1000:
        score -= 10
    else:
        score -= 15

    return min(max(score, 0), 100)


def get_verdict(score):
    if score >= 65:
        return "BUY"
    elif score >= 40:
        return "TEST"
    else:
        return "SKIP"


def get_ai_insights(row):
    name, products, products_with_sales, sellers, sellers_with_sales, \
    revenue, potential_revenue, lost_revenue, lost_revenue_pct, orders, \
    buyout_pct, turnover, profit_pct, avg_rating, rank, commission, avg_price = row

    avg_price_val = float(avg_price or 0) if avg_price else float(revenue or 0) / (orders or 1)
    orders_per_day = float(orders or 0) / 30
    real_turnover = round(float(turnover or 0) / float(buyout_pct or 1))

    try:
        import anthropic, os
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        prompt = f"""Ты эксперт по торговле на Wildberries. Проанализируй нишу и дай конкретные инсайты.

Ниша: {name}
Выручка: {float(revenue or 0):,.0f} ₽/мес
Заказов: {orders or 0} в месяц ({orders_per_day:.0f}/день)
Продавцов всего: {sellers or 0}
Продавцов с продажами: {sellers_with_sales or 0}
Выкуп: {float(buyout_pct or 0)*100:.0f}%
Оборачиваемость реальная: {real_turnover} дней
Маржинальность: {float(profit_pct or 0)*100:.0f}%
Средняя цена: {avg_price_val:,.0f} ₽
Комиссия WB: {float(commission or 0):.0f}%
Упущенная выручка: {float(lost_revenue_pct or 0)*100:.0f}%

Дай ровно 3 коротких инсайта (каждый 1-2 предложения) и 2 гипотезы для проверки.
Гипотезы пиши в формате действия: "Протестировать X, чтобы Y" или "Войти через Z, так как W".
Отвечай в формате JSON:
{{"insights": ["инсайт1", "инсайт2", "инсайт3"], "hypotheses": ["гипотеза1", "гипотеза2"], "analysis": "краткий вывод 1-2 предложения"}}
Только JSON, никакого другого текста."""

        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        import json
        text = message.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        return result
    except Exception as e:
        print(f"AI Insights error: {e}")
        # Fallback на шаблонные инсайты если API недоступен
        insights = []
        if sellers > 10000:
            insights.append(f"Ниша насыщена: {sellers} продавцов. Вход без УТП рискован.")
        elif sellers > 1000:
            insights.append(f"Умеренная конкуренция: {sellers} продавцов. Есть место для новых игроков.")
        else:
            insights.append(f"Низкая конкуренция: {sellers} продавцов. Хорошая возможность для входа.")
        insights.append(f"Спрос: {orders_per_day:.0f} заказов/день. Выручка {float(revenue or 0):,.0f} ₽/мес.")
        insights.append(f"Выкуп {float(buyout_pct or 0)*100:.0f}%, оборачиваемость {real_turnover} дней.")
        hypotheses = [
            f"Протестировать вход с минимальной партией 30-50 единиц по цене {avg_price_val*0.95:,.0f}–{avg_price_val*1.05:,.0f} ₽.",
            f"Проверить сезонность: сравнить заказы за последние 30 и 90 дней."
        ]
        return {"insights": insights, "hypotheses": hypotheses, "analysis": " ".join(insights)}

HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WB Niche Analyzer</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }@media (max-width: 768px) {
  .header { padding: 16px 20px; }
  .main { padding: 0 16px; margin: 24px auto; }
  .hero h1 { font-size: 28px; }
  .search-row { flex-direction: column; }
  .btn { width: 100%; }
  .metrics-grid { grid-template-columns: repeat(2, 1fr) !important; gap: 8px; }
  .metric-card { padding: 10px; display: flex; flex-direction: column; justify-content: center; }
  .metric-value { font-size: 16px; }
  .metric-label { font-size: 10px; }
  .metric-sub { font-size: 10px; }
  .charts-grid { grid-template-columns: 1fr; }
  .calc-grid { grid-template-columns: 1fr; }
  .verdict-card { flex-direction: column; text-align: center; }
  .score-bar { width: 100%; }
  .verdict-badge { width: 100%; text-align: center; }
  div[style*="padding:10px 40px"] { padding: 8px 12px !important; flex-wrap: nowrap; gap: 4px; overflow-x: auto; }
  div[style*="padding:10px 40px"] button { padding: 6px 10px !important; font-size: 11px !important; white-space: nowrap; }
}
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f13; color: #e8e8e8; min-height: 100vh; }
.header { background: #1a1a24; border-bottom: 1px solid #2a2a3a; padding: 20px 40px; display: flex; align-items: center; gap: 16px; }
.page-wrap { display: flex; min-height: calc(100vh - 57px); }
.sidebar { width: 220px; background: #141418; border-right: 1px solid #2a2a3a; padding: 16px; flex-shrink: 0; }
.sidebar-label { font-size: 10px; color: #444; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 8px; }
.sidebar-item { display: flex; align-items: center; padding: 10px 12px; border-radius: 8px; cursor: pointer; font-size: 16px; color: #888; margin-bottom: 4px; transition: all 0.15s; }
.sidebar-item:hover { background: #1a1a24; color: #ddd; }
.sidebar-item.active { background: #6c63ff22; color: #6c63ff; }
.content-area { flex: 1; overflow-y: auto; }
@media (max-width: 768px) { .sidebar { display: none; } }
.logo { font-size: 22px; font-weight: 700; color: #fff; letter-spacing: -0.5px; }
.logo span { color: #6c63ff; }
.tagline { color: #666; font-size: 13px; }
.main { max-width: 100%; margin: 0; padding: 32px 40px; }
.hero { text-align: center; margin-bottom: 48px; }
.hero h1 { font-size: 42px; font-weight: 700; color: #fff; line-height: 1.2; margin-bottom: 16px; }
.hero h1 span { color: #6c63ff; }
.hero p { color: #888; font-size: 16px; }
.search-box { background: #1a1a24; border: 1px solid #2a2a3a; border-radius: 16px; padding: 24px; margin-bottom: 32px; }
.search-row { display: flex; gap: 12px; }
.search-input { flex: 1; background: #0f0f13; border: 1px solid #2a2a3a; border-radius: 10px; padding: 14px 18px; color: #fff; font-size: 15px; outline: none; transition: border-color 0.2s; }
.search-input:focus { border-color: #6c63ff; }
.search-input::placeholder { color: #444; }
#suggestions div.highlighted { background: #6c63ff33 !important; border-left: 2px solid #6c63ff; }
.btn { background: #6c63ff; color: #fff; border: none; border-radius: 10px; padding: 14px 28px; font-size: 15px; font-weight: 600; cursor: pointer; transition: background 0.2s; white-space: nowrap; }
.btn:hover { background: #5a52e0; }
.btn:disabled { background: #333; cursor: not-allowed; }
.examples { margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; }
.chip { background: #0f0f13; border: 1px solid #2a2a3a; border-radius: 20px; padding: 6px 14px; font-size: 12px; color: #888; cursor: pointer; transition: all 0.2s; }
.chip:hover { border-color: #6c63ff; color: #6c63ff; }
.result { display: none; }
.metrics-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 24px; }
.metric-card { background: #1a1a24; border: 1px solid #2a2a3a; border-radius: 12px; padding: 20px; }
.metric-label { font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }.currency-switch { display: flex; gap: 4px; margin-top: 6px; }
.currency-btn { background: transparent; border: 1px solid #2a2a3a; border-radius: 4px; color: #555; font-size: 10px; padding: 2px 6px; cursor: pointer; }
.currency-btn.active { background: #6c63ff22; border-color: #6c63ff; color: #6c63ff; }
.metric-value { font-size: 24px; font-weight: 700; color: #fff; }
.metric-sub { font-size: 12px; color: #555; margin-top: 4px; }.turn-fast { color: #22c55e; }
.turn-normal { color: #22c55e; }
.turn-seasonal { color: #eab308; }
.turn-slow { color: #ef4444; }
.verdict-card { background: #1a1a24; border: 1px solid #2a2a3a; border-radius: 12px; padding: 24px; margin-bottom: 24px; display: flex; align-items: center; gap: 20px; }
.verdict-badge { padding: 8px 20px; border-radius: 8px; font-size: 18px; font-weight: 700; }
.verdict-BUY { background: #0d2a1a; color: #22c55e; border: 1px solid #166534; }
.verdict-TEST { background: #1a1a0d; color: #eab308; border: 1px solid #713f12; }
.verdict-SKIP { background: #2a0d0d; color: #ef4444; border: 1px solid #7f1d1d; }
.verdict-text { flex: 1; }
.verdict-title { font-size: 16px; font-weight: 600; color: #fff; margin-bottom: 4px; }
.verdict-desc { font-size: 13px; color: #666; }
.score-bar { width: 120px; }
.score-num { font-size: 36px; font-weight: 700; color: #fff; text-align: center; }
.score-label { font-size: 11px; color: #555; text-align: center; }
.ai-card { background: #1a1a24; border: 1px solid #2a2a3a; border-radius: 12px; padding: 24px; margin-bottom: 24px; }
.ai-header { display: flex; align-items: center; gap: 10px; margin-bottom: 20px; }
.ai-dot { width: 8px; height: 8px; background: #6c63ff; border-radius: 50%; }
.ai-title { font-size: 14px; font-weight: 600; color: #fff; }
.insight-item { display: flex; gap: 12px; margin-bottom: 14px; padding-bottom: 14px; border-bottom: 1px solid #1f1f2e; }
.insight-item:last-child { border-bottom: none; margin-bottom: 0; padding-bottom: 0; }
.insight-num { width: 24px; height: 24px; background: #6c63ff22; border-radius: 6px; display: flex; align-items: center; justify-content: center; font-size: 11px; color: #6c63ff; font-weight: 700; flex-shrink: 0; margin-top: 1px; }
.insight-text { font-size: 14px; color: #bbb; line-height: 1.6; }
.hyp-item { background: #0f0f13; border-radius: 8px; padding: 12px 16px; margin-bottom: 8px; font-size: 13px; color: #888; line-height: 1.5; }
.hyp-item:before { content: "→ "; color: #6c63ff; }
.analysis-box { background: #0f0f13; border-left: 3px solid #6c63ff; border-radius: 0 8px 8px 0; padding: 16px; font-size: 14px; color: #aaa; line-height: 1.7; }
.charts-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin-bottom: 24px; }
.chart-card { background: #1a1a24; border: 1px solid #2a2a3a; border-radius: 12px; padding: 20px; box-sizing: border-box; overflow: hidden; }
.chart-title { font-size: 12px; color: #555; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 16px; }
.bar-chart { display: flex; align-items: flex-end; gap: 4px; height: 80px; }
.bar { flex: 1; border-radius: 3px 3px 0 0; transition: opacity 0.2s; min-width: 8px; }
.bar:hover { opacity: 0.8; }
.gauge-wrap { display: flex; align-items: center; justify-content: center; flex-direction: column; height: 80px; }
.gauge-ring { width: 80px; height: 80px; }
.metric-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #1f1f2e; }
.metric-row:last-child { border-bottom: none; }
.metric-row-label { font-size: 13px; color: #666; }
.metric-row-value { font-size: 13px; color: #ddd; font-weight: 500; }
.metric-row-bar { height: 4px; background: #1f1f2e; border-radius: 2px; margin-top: 4px; }
.metric-row-fill { height: 4px; border-radius: 2px; background: #6c63ff; }
.section-title { font-size: 12px; color: #555; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 12px; }
.calc-wrap { background: #1a1a24; border: 1px solid #2a2a3a; border-radius: 12px; padding: 24px; margin-bottom: 24px; display: none; }
.calc-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
.calc-field { display: flex; flex-direction: column; gap: 6px; }
.calc-label { font-size: 12px; color: #555; text-transform: uppercase; letter-spacing: 0.5px; }
.calc-input { background: #0f0f13; border: 1px solid #2a2a3a; border-radius: 8px; padding: 10px 14px; color: #fff; font-size: 15px; outline: none; } .calc-input::placeholder { color: #333; font-size: 13px; }
.calc-input:focus { border-color: #6c63ff; }
.calc-result { background: #0f0f13; border-radius: 10px; padding: 20px; }
.calc-result-row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #1f1f2e; font-size: 14px; }
.calc-result-row:last-child { border-bottom: none; font-weight: 600; font-size: 16px; }
.calc-result-row span:first-child { color: #888; }
.calc-result-row span:last-child { color: #fff; }
.calc-positive { color: #22c55e !important; }
.calc-negative { color: #ef4444 !important; }
.scheme-tabs { display: flex; gap: 8px; margin-bottom: 16px; }
.scheme-tab { padding: 8px 16px; border-radius: 8px; border: 1px solid #2a2a3a; background: transparent; color: #888; font-size: 13px; cursor: pointer; }
.scheme-tab.active { background: #6c63ff; border-color: #6c63ff; color: #fff; }.loading { text-align: center; padding: 40px; color: #555; display: none; }
.modal-overlay { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:#000000cc; z-index:1000; align-items:center; justify-content:center; }
.modal-overlay.active { display:flex; }
.modal-content { background:#1a1a24; border:1px solid #2a2a3a; border-radius:16px; padding:28px; width:90%; max-width:1100px; max-height:90vh; overflow-y:auto; position:relative; }
.modal-close { position:absolute; top:16px; right:16px; background:#2a2a3a; border:none; color:#888; width:32px; height:32px; border-radius:8px; cursor:pointer; font-size:18px; display:flex; align-items:center; justify-content:center; }
.modal-close:hover { background:#3a3a4a; color:#fff; }
.chart-card { cursor:pointer; transition:border-color 0.2s; }
.chart-card:hover { border-color:#6c63ff55; }
.spinner { width: 32px; height: 32px; border: 2px solid #2a2a3a; border-top-color: #6c63ff; border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 16px; }
@keyframes spin { to { transform: rotate(360deg); } }
.error { background: #2a0d0d; border: 1px solid #7f1d1d; border-radius: 12px; padding: 20px; color: #ef4444; display: none; }
.niche-name { font-size: 28px; font-weight: 700; color: #fff; margin-bottom: 24px; }
/* Тултипы для метрик */
.metric-card { position: relative; }
.metric-tooltip {
  display: none;
  position: absolute;
  bottom: calc(100% + 8px);
  left: 50%;
  transform: translateX(-50%);
  background: #1a1a2e;
  border: 1px solid #2a2a3a;
  border-radius: 8px;
  padding: 10px 14px;
  width: 220px;
  font-size: 12px;
  color: #aaa;
  line-height: 1.5;
  z-index: 100;
  box-shadow: 0 4px 20px rgba(0,0,0,0.5);
  pointer-events: none;
}
.metric-tooltip::after {
  content: '';
  position: absolute;
  top: 100%;
  left: 50%;
  transform: translateX(-50%);
  border: 6px solid transparent;
  border-top-color: #2a2a3a;
}
.metric-card:hover .metric-tooltip { display: block; }
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
</head>
<body>
<div class="header">
  <div>
    <div class="logo" onclick="goHome()" style="cursor:pointer;">WB<span>Analyzer</span></div>
    <div class="tagline">AI-платформа анализа товарных ниш</div>
  </div>
  <div style="margin-left:auto;display:flex;align-items:center;gap:6px;">
    <button id="gcur-rub" onclick="setGlobalCurrency('rub')" style="background:#6c63ff22;border:1px solid #6c63ff;border-radius:6px;color:#6c63ff;font-size:13px;padding:6px 12px;cursor:pointer;font-weight:600;">₽</button>
    <button id="gcur-usd" onclick="setGlobalCurrency('usd')" style="background:transparent;border:1px solid #2a2a3a;border-radius:6px;color:#555;font-size:13px;padding:6px 12px;cursor:pointer;font-weight:600;">$</button>
    <button id="gcur-eur" onclick="setGlobalCurrency('eur')" style="background:transparent;border:1px solid #2a2a3a;border-radius:6px;color:#555;font-size:13px;padding:6px 12px;cursor:pointer;font-weight:600;">€</button>
    <button id="gcur-byn" onclick="setGlobalCurrency('byn')" style="background:transparent;border:1px solid #2a2a3a;border-radius:6px;color:#555;font-size:13px;padding:6px 12px;cursor:pointer;font-weight:600;">Br</button>
  </div>
</div>
<div class="page-wrap">
<div id="chartModal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:#000000cc;z-index:1000;align-items:center;justify-content:center;" onclick="if(event.target.id=='chartModal'){this.style.display='none';if(modalChartInstance){modalChartInstance.destroy();modalChartInstance=null;}}">
    <div style="background:#1a1a24;border:1px solid #2a2a3a;border-radius:16px;padding:28px;width:90%;max-width:1100px;max-height:90vh;overflow-y:auto;position:relative;">
      <button onclick="document.getElementById('chartModal').style.display='none';if(modalChartInstance){modalChartInstance.destroy();modalChartInstance=null;}" style="position:absolute;top:16px;right:16px;background:#2a2a3a;border:none;color:#888;width:32px;height:32px;border-radius:8px;cursor:pointer;font-size:18px;">✕</button>
      <div id="modalTitle" style="font-size:14px;color:#555;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:20px;"></div>
      <div style="position:relative;height:500px;width:100%;"><canvas id="modalChart"></canvas></div>
    </div>
  </div>
<div class="sidebar">

  <div class="sidebar-item active" onclick="showCatalog()">🔍 Все ниши</div>
  <div class="sidebar-item" onclick="showTopNiches()">⭐ Топ ниши</div>
  <div class="sidebar-item" onclick="showPortfolio()">🎯 Подбор</div>
  <div class="sidebar-item" onclick="showCalc()">🧮 Калькулятор</div>
  <div class="sidebar-item" onclick="showPortfolioStub()">📦 Портфель</div>
  <div class="sidebar-item" id="watchlist-menu" onclick="showWatchlist()">📌 В работе <span id="watchlist-count" style="background:#6c63ff33;color:#a78bfa;border-radius:10px;padding:1px 7px;font-size:11px;margin-left:4px;"></span></div>
</div>
<div class="content-area">
<div class="main">
  <div class="search-box">
    <div class="search-row">
      <input class="search-input" id="query" autocomplete="off" placeholder="Введите нишу, например: платья, термосы, наушники..." />
      <button class="btn" id="analyze-btn" onclick="analyze()" disabled>Анализировать</button>
    </div>
    <div class="examples" id="top-chips">
      <span style="font-size:11px;color:#555;align-self:center;">🔥 Топ ниши:</span>
    </div>
  </div>
  <div id="top-niches" style="display:none;margin-top:24px;"></div>
  <div id="portfolio" style="display:none;margin-top:24px;"></div>
  <div id="watchlist" style="display:none;margin-top:24px;"></div><div id="catalog" style="display:none;margin-top:24px;">
    <div style="display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap;align-items:center;">
      <input id="cat-search" placeholder="Фильтр по названию..." style="background:#1a1a24;border:1px solid #2a2a3a;border-radius:8px;padding:10px 14px;color:#fff;font-size:13px;outline:none;flex:1;min-width:220px;" oninput="filterCatalog()"/>
      <select id="cat-sort" onchange="filterCatalog()" style="background:#1a1a24;border:1px solid #2a2a3a;border-radius:8px;padding:10px 14px;color:#888;font-size:13px;outline:none;">
        <option value="revenue">По выручке</option>
        <option value="orders">По заказам</option>
        <option value="profit">По прибыльности</option>
        <option value="buyout">По выкупу</option>
        <option value="turnover">По оборачиваемости</option>
      </select>
    </div>
    <div id="cat-chips" style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;"></div>
    <div id="cat-stats" style="font-size:12px;color:#555;margin-bottom:12px;"></div>
    <div id="cat-list"></div>
    </div><div class="calc-wrap" id="calculator" style="display:none;"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;">
      <div style="font-size:16px;font-weight:600;color:#fff;">Калькулятор юнит-экономики</div>
      <div style="display:flex;gap:12px;align-items:center;">
        <div class="scheme-tabs">
          <button class="scheme-tab active" onclick="setScheme('fbo')">FBO</button>
          <button class="scheme-tab" onclick="setScheme('fbs')">FBS</button>
          <button class="scheme-tab" onclick="setScheme('china')">Китай</button>
        </div>

      </div>
    </div>
    <div class="calc-grid">
      <div class="calc-field">
        <div class="calc-label" id="label-price">Цена продажи, ₽</div>
        <input class="calc-input" id="c-price" type="number" placeholder="цена продажи" oninput="calcUnit()"/>
      </div>
      <div class="calc-field">
        <div class="calc-label" id="label-cost">Себестоимость, ₽</div>
        <input class="calc-input" id="c-cost" type="number" placeholder="себестоимость" oninput="calcUnit()"/>
      </div>
      <div class="calc-field">
        <div class="calc-label">Комиссия WB, %</div>
        <input class="calc-input" id="c-commission" type="number" placeholder="% комиссии" oninput="calcUnit()"/>
      </div>
      <div class="calc-field">
        <div class="calc-label" id="label-logistic">Логистика, ₽</div>
        <input class="calc-input" id="c-logistic" type="number" placeholder="стоимость логистики" oninput="calcUnit()"/>
      </div>
      <div class="calc-field">
        <div class="calc-label">Процент выкупа, %</div>
        <input class="calc-input" id="c-buyout" type="number" placeholder="% выкупа" oninput="calcUnit()"/>
      </div>
      <div class="calc-field">
        <div class="calc-label">Налог, %</div>
        <input class="calc-input" id="c-tax" type="number" placeholder="% налога" oninput="calcUnit()"/>
      </div>
    </div><div id="china-block" style="display:none;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px;">
      <div class="calc-field">
        <div class="calc-label">Цена товара в Китае, $</div>
        <input class="calc-input" id="c-china-price" type="number" placeholder="цена в $" oninput="calcUnit()"/>
      </div>
      <div class="calc-field">
        <div class="calc-label">Курс доллара, ₽</div>
        <input class="calc-input" id="c-rate" type="number" placeholder="курс ₽/$" oninput="calcUnit()"/>
      </div>
      <div class="calc-field">
        <div class="calc-label">Доставка из Китая, $ за кг</div>
        <input class="calc-input" id="c-delivery" type="number" placeholder="$ за кг" oninput="calcUnit()"/>
      </div>
      <div class="calc-field">
        <div class="calc-label">Вес товара, кг</div>
        <input class="calc-input" id="c-weight" type="number" placeholder="вес кг" oninput="calcUnit()"/>
      </div>
      <div class="calc-field">
        <div class="calc-label">Таможенная пошлина, %</div>
        <input class="calc-input" id="c-customs" type="number" placeholder="% комиссии" oninput="calcUnit()"/>
      </div>
      <div class="calc-field">
        <div class="calc-label">НДС на импорт, %</div>
        <input class="calc-input" id="c-vat" type="number" placeholder="% НДС" oninput="calcUnit()"/>
      </div>
    </div>
    <div class="calc-result" id="calc-result">
      <div style="color:#555;font-size:14px;text-align:center;">Введите данные для расчёта</div>
    </div>
  </div><div class="loading" id="loading">
    <div class="spinner"></div>
    <div>Анализируем нишу...</div>
  </div>
  <div class="error" id="error"></div>
  <div class="result" id="result"></div>
</div>
<script>
function getWatchlist(){
  try{return JSON.parse(localStorage.getItem('watchlist')||'[]');}
  catch(e){return[];}
}
function saveWatchlist(list){
  localStorage.setItem('watchlist',JSON.stringify(list));
  var el=document.getElementById('watchlist-count');
  if(el)el.textContent=list.length>0?list.length:'';
}
function toggleWatchlist(full,name,revenue){
  var list=getWatchlist();
  var idx=list.findIndex(function(n){return n.full===full;});
  if(idx>=0){list.splice(idx,1);}
  else{list.push({full:full,name:name,revenue:revenue});}
  saveWatchlist(list);
  document.querySelectorAll('[data-wl]').forEach(function(b){
    if(b.getAttribute('data-wl')===full)
      b.textContent=list.some(function(n){return n.full===full;})?'📌':'🔖';
  });
}
function isInWatchlist(full){
  return getWatchlist().some(function(n){return n.full===full;});
}
function updateWatchlistBtn(full){
  var btn=document.getElementById('watchlist-btn');
  if(btn)btn.textContent=isInWatchlist(full)?'📌 В работе':'🔖 В работе';
}
function removeFromWatchlist(full){
  var list=getWatchlist();
  list=list.filter(function(n){return n.full!==full;});
  saveWatchlist(list);
  showWatchlist();
}
function openFromWatchlist(full){
  setQuery(full);
}
function showWatchlist(){
  hideAll();
  document.querySelectorAll('.sidebar-item').forEach(function(t){t.classList.remove('active');});
  var m=document.getElementById('watchlist-menu');
  if(m)m.classList.add('active');
  var div=document.getElementById('watchlist');
  if(!div)return;
  div.style.display='block';
  var list=getWatchlist();
  if(list.length===0){
    div.innerHTML='<div style="color:#555;padding:40px;text-align:center;">Нет ниш в работе.<br><small>Добавьте ниши из Топ ниш.</small></div>';
    return;
  }
  var html='<div style="font-size:20px;font-weight:700;color:#fff;margin-bottom:20px;">📌 Ниши в работе</div><div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;">';
  for(var i=0;i<list.length;i++){
    var n=list[i];
    var sn=n.name.includes(' / ')?n.name.split(' / ').slice(1).join(' / '):n.name;
    html+='<div style="background:#1a1a24;border:1px solid #6c63ff44;border-radius:12px;padding:16px;">';
    html+='<div style="font-size:15px;font-weight:600;color:#fff;margin-bottom:8px;">'+sn+'</div>';
    html+='<div style="font-size:13px;color:#555;margin-bottom:12px;">'+fmt(n.revenue/2)+'/год</div>';
    html+='<div style="display:flex;gap:8px;">';
    html+='<button id="wl-open-'+i+'" style="flex:1;background:#6c63ff22;border:1px solid #6c63ff44;border-radius:6px;color:#a78bfa;padding:6px;cursor:pointer;font-size:12px;">Открыть</button>';
    html+='<button id="wl-del-'+i+'" style="background:#ef444422;border:1px solid #ef444444;border-radius:6px;color:#ef4444;padding:6px 10px;cursor:pointer;font-size:12px;">✕</button>';
    html+='</div></div>';
  }
  html+='</div>';
  div.innerHTML=html;
  for(var j=0;j<list.length;j++){
    (function(idx){
      var openBtn=document.getElementById('wl-open-'+idx);
      var delBtn=document.getElementById('wl-del-'+idx);
      if(openBtn)openBtn.addEventListener('click',function(){openFromWatchlist(list[idx].full);});
      if(delBtn)delBtn.addEventListener('click',function(){removeFromWatchlist(list[idx].full);});
    })(j);
  }
}

let suggestTimer = null;
let catalogData = [];
let currentCurrency = 'rub';
const rates = { rub: 1, usd: 0.011, eur: 0.010, byn: 0.036 };
const symbols = { rub: '₽', usd: '$', eur: '€', byn: 'Br' };

async function deepAnalysis(d) {
  const block = document.getElementById('deep-analysis-block');
  const content = document.getElementById('deep-content');
  const loading = document.getElementById('deep-loading');
  
  block.style.display = 'block';
  loading.style.display = 'block';
  content.innerHTML = '';
  
  // Скроллим к блоку
  block.scrollIntoView({behavior: 'smooth', block: 'start'});
  
  try {
    const params = new URLSearchParams({
      name: d.full || d.name,
      revenue: d.revenue,
      avg_price: d.avg_price,
      commission: d.commission,
      buyout_pct: d.buyout_pct,
      profit_pct: d.profit_pct,
      turnover: d.turnover,
      sellers: d.sellers,
      sellers_with_sales: d.sellers_with_sales,
      currency: currentCurrency
    });
    const r = await fetch('/deep-analysis?' + params);
    const data = await r.json();
    loading.style.display = 'none';
    if (data.error) {
      content.innerHTML = '<div style="color:#f87171;padding:16px;">' + data.error + '</div>';
      return;
    }
    content.innerHTML = data.html;
  } catch(e) {
    loading.style.display = 'none';
    content.innerHTML = '<div style="color:#f87171;padding:16px;">Ошибка соединения</div>';
  }
}

function fillCalculator(avg_price, commission, buyout_pct) {
  // Заполняем калькулятор данными из ниши
  showCalc();
  setTimeout(() => {
    const price = document.getElementById('c-price');
    const comm = document.getElementById('c-commission');
    const buyout = document.getElementById('c-buyout');
    const logistic = document.getElementById('c-logistic');
    if (price) price.value = Math.round(avg_price);
    if (comm) comm.value = Math.round(commission);
    if (buyout) buyout.value = Math.round(buyout_pct * 100);
    if (logistic) logistic.value = Math.round(avg_price * 0.06);
    calcUnit();
  }, 100);
}

function setGlobalCurrency(cur) {
  setCurrency(cur);
  // Синхронизируем с калькулятором (у него нет EUR)
  const calcCur = cur === 'eur' ? 'rub' : cur;
  if (typeof setCalcCurrency === 'function') setCalcCurrency(calcCur);
  // Обновляем глубокий анализ если открыт
  if (window.currentNiche && document.getElementById('deep-analysis-block').style.display !== 'none') {
    deepAnalysis(window.currentNiche);
  }
  // Обновляем кнопки в шапке
  ['rub','usd','eur','byn'].forEach(c => {
    const btn = document.getElementById('gcur-' + c);
    if (!btn) return;
    if (c === cur) {
      btn.style.background = '#6c63ff22';
      btn.style.borderColor = '#6c63ff';
      btn.style.color = '#6c63ff';
    } else {
      btn.style.background = 'transparent';
      btn.style.borderColor = '#2a2a3a';
      btn.style.color = '#555';
    }
  });
}
function updateChartsForCurrency(cur) {
  const rate = rates[cur];
  const sym = symbols[cur];
  // Пересчитываем метки графика цен
  if (window._chartData && window._chartData.price_labels_rub && window.priceChartInstance) {
    const newLabels = window._chartData.price_labels_rub.map(label => {
      return label.replace(/\d+/g, n => Math.round(parseInt(n) * rate).toLocaleString('ru')) + ' ' + sym;
    });
    window.priceChartInstance.data.labels = newLabels;
    window.priceChartInstance.update('active');
  }
  // Пересчитываем прогноз выручки — пересоздаём график
  if (window._chartData && window._chartData.forecast_data_rub && window.forecastChartInstance) {
    const last6rev = window._chartData.revenue_rub.map(v => +(v * rate).toFixed(2));
    const forecastData = window._chartData.forecast_data_rub.map(v => +(v * rate).toFixed(2));
    const allRevenue = [...last6rev, ...Array(forecastData.length).fill(null)];
    const forecastFull = [...Array(last6rev.length - 1).fill(null), last6rev[last6rev.length-1], ...forecastData];
    window.forecastChartInstance.data.datasets[0].data = allRevenue;
    window.forecastChartInstance.data.datasets[1].data = forecastFull;
    window.forecastChartInstance.data.datasets[0].label = 'Факт (млн ' + sym + ')';
    window.forecastChartInstance.data.datasets[1].label = 'Прогноз (млн ' + sym + ')';
    window.forecastChartInstance.update('active');
  }
}
function setCurrency(cur) {
  currentCurrency = cur;
  document.querySelectorAll('.currency-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll(`.currency-btn[data-cur="${cur}"]`).forEach(b => b.classList.add('active'));
  if (window.lastResult) {
    renderResult(window.lastResult);
    setTimeout(() => updateChartsForCurrency(cur), 100);
  }
}

function fmtCurrency(rub) {
  const val = rub * rates[currentCurrency];
  const sym = symbols[currentCurrency];
  if (val >= 1e9) return (val/1e9).toFixed(1) + ' млрд ' + sym;
  if (val >= 1e6) return (val/1e6).toFixed(1) + ' млн ' + sym;
  if (val >= 1e3) return (val/1e3).toFixed(0) + ' тыс ' + sym;
  return Math.round(val) + ' ' + sym;
}let currentScheme = 'fbo';

function setScheme(scheme) {
  currentScheme = scheme;
  document.querySelectorAll('.scheme-tab').forEach(t => t.classList.remove('active'));
  setActiveMenu(event.target);
  const chinaBlock = document.getElementById('china-block');
  const logistic = document.getElementById('c-logistic');
  if (scheme === 'fbo') {
    logistic.placeholder = 'руб. (FBO ~80)';
    document.getElementById('c-logistic').value = '';
    chinaBlock.style.display = 'none';
  } else if (scheme === 'fbs') {
    logistic.placeholder = 'руб. (FBS ~150)';
    document.getElementById('c-logistic').value = '';
    chinaBlock.style.display = 'none';
  } else if (scheme === 'china') {
    logistic.placeholder = 'руб. (FBO ~80)';
    document.getElementById('c-logistic').value = '';
    chinaBlock.style.display = 'grid';
  }
  calcUnit();
}
let calcCurrency = 'rub';
let calcRates = { rub: 1, usd: 0.011, byn: 0.036 };
const calcSymbols = { rub: '₽', usd: '$', byn: 'BYN' };

async function loadCalcRates() {
  try {
    const r = await fetch('https://www.cbr-xml-daily.ru/daily_json.js');
    const d = await r.json();
    const usdRate = d.Valute.USD.Value;
    const eurRate = d.Valute.EUR.Value;
    const bynRate = d.Valute.BYN.Value / d.Valute.BYN.Nominal;
    calcRates = { rub: 1, usd: 1/usdRate, byn: 1/bynRate };
    // Обновляем курсы и для карточки ниши
    rates.usd = 1/usdRate;
    rates.eur = 1/eurRate;
    rates.byn = 1/bynRate;
    console.log('Курсы загружены: USD=' + usdRate + ' EUR=' + eurRate + ' BYN=' + bynRate);
  } catch(e) {
    console.log('Курсы не загружены, используем дефолтные');
  }
}

function setCalcCurrency(cur) {
  calcCurrency = cur;
  const sym = calcSymbols[cur];
  ['rub','usd','byn'].forEach(c => {
    const btn = document.getElementById('calc-cur-' + c);
    if(btn) btn.classList.toggle('active', c === cur);
  });
  const lp = document.getElementById('label-price');
  const lc = document.getElementById('label-cost');
  const ll = document.getElementById('label-logistic');
  if(lp) lp.textContent = 'Цена продажи, ' + sym;
  if(lc) lc.textContent = 'Себестоимость, ' + sym;
  if(ll) ll.textContent = 'Логистика, ' + sym;
  calcUnit();
}

function fmtCalc(val) {
  const converted = val * calcRates[calcCurrency];
  return converted.toLocaleString('ru', {maximumFractionDigits: calcCurrency === 'rub' ? 0 : 2}) + ' ' + calcSymbols[calcCurrency];
}

function calcUnit() {
  const price = parseFloat(document.getElementById('c-price').value) || 0;
  const commission = parseFloat(document.getElementById('c-commission').value) || 0;
  const logistic = parseFloat(document.getElementById('c-logistic').value) || 0;
  const buyout = parseFloat(document.getElementById('c-buyout').value) || 80;
  const tax = parseFloat(document.getElementById('c-tax').value) || 0;

  let cost = parseFloat(document.getElementById('c-cost').value) || 0;

  if (currentScheme === 'china') {
    const chinaPrice = parseFloat(document.getElementById('c-china-price').value) || 0;
    const rate = parseFloat(document.getElementById('c-rate').value) || 90;
    const delivery = parseFloat(document.getElementById('c-delivery').value) || 0;
    const weight = parseFloat(document.getElementById('c-weight').value) || 0;
    const customs = parseFloat(document.getElementById('c-customs').value) || 0;
    const vat = parseFloat(document.getElementById('c-vat').value) || 0;

    const chinaPriceRub = chinaPrice * rate;
    const deliveryCost = delivery * weight * rate;
    const customsAmt = chinaPriceRub * (customs / 100);
    const vatAmt = (chinaPriceRub + customsAmt) * (vat / 100);
    cost = chinaPriceRub + deliveryCost + customsAmt + vatAmt;
    document.getElementById('c-cost').value = Math.round(cost);
  }

  if (!price || !cost) {
    document.getElementById('calc-result').innerHTML = '<div style="color:#555;font-size:14px;text-align:center;">Введите данные для расчёта</div>';
    return;
  }

  const buyoutRate = buyout / 100;
  const commissionAmt = price * (commission / 100);
  const taxAmt = price * (tax / 100);
  const logisticTotal = logistic / buyoutRate;
  const revenue = price - commissionAmt - logisticTotal - taxAmt;
  const profit = revenue - cost;
  const margin = price > 0 ? (profit / price * 100) : 0;
  const roi = cost > 0 ? (profit / cost * 100) : 0;

  const profitColor = profit >= 0 ? 'calc-positive' : 'calc-negative';
  const marginColor = margin >= 20 ? 'calc-positive' : margin >= 0 ? '' : 'calc-negative';

  const chinaDetails = currentScheme === 'china' ? `
    <div class="calc-result-row"><span>Себестоимость из Китая</span><span>${fmtCalc(cost)}</span></div>
  ` : `
    <div class="calc-result-row"><span>Себестоимость</span><span>-${fmtCalc(cost)}</span></div>
  `;

  document.getElementById('calc-result').innerHTML = `
    <div class="calc-result-row"><span>Цена продажи</span><span>${fmtCalc(price)}</span></div>
    <div class="calc-result-row"><span>Комиссия WB</span><span>-${fmtCalc(commissionAmt)}</span></div>
    <div class="calc-result-row"><span>Логистика (с учётом выкупа)</span><span>-${fmtCalc(logisticTotal)}</span></div>
    <div class="calc-result-row"><span>Налог</span><span>-${fmtCalc(taxAmt)}</span></div>
    ${chinaDetails}
    <div class="calc-result-row"><span>Маржа</span><span class="${marginColor}">${margin.toFixed(1)}%</span></div>
    <div class="calc-result-row"><span>ROI</span><span class="${profitColor}">${roi.toFixed(1)}%</span></div>
    <div class="calc-result-row"><span style="color:#fff;font-weight:600">Прибыль с единицы</span><span class="${profitColor}">${fmtCalc(profit)}</span></div>
  `;
}
function hideAll() {
  ['catalog','calculator','result','top-niches','watchlist','history','portfolio'].forEach(id => {
    const el = document.getElementById(id);
    if(el) el.style.display = 'none';
  });
  const sb = document.querySelector('.search-box');
  if(sb) sb.style.display = 'block';
}

function setActiveMenu(el) {
  document.querySelectorAll('.sidebar-item').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
}

let topNichesOffset = 0;
function refreshTopNiches() {
  topNichesOffset += 21;
  showTopNiches();
}
let portfolioOffset = 0;
function refreshPortfolio() {
  portfolioOffset += 15;
  showPortfolio();
}
// ===== СИСТЕМА ПОДБОРА ПОРТФЕЛЯ =====
var portfolioParams = null;

async function showPortfolio() {
  hideAll();
  setActiveMenu(event.target);
  const div = document.getElementById('portfolio');
  div.style.display = 'block';
  renderPortfolioQuestionnaire(div);
}

function renderPortfolioQuestionnaire(div) {
  div.innerHTML = `
    <div style="max-width:1100px;margin:0 auto;">
      <div style="margin-bottom:32px;">
        <div style="font-size:22px;font-weight:700;color:#fff;margin-bottom:8px;">🎯 Подбор товарного портфеля</div>
        <div style="font-size:14px;color:#555;">Ответьте на 5 вопросов — система подберёт оптимальные ниши для вашего бизнеса</div>
      </div>

      <!-- Вопрос 1: Бюджет на один SKU -->
      <div style="background:#1a1a24;border-radius:12px;padding:20px;margin-bottom:16px;">
        <div style="font-size:13px;color:#a78bfa;font-weight:600;margin-bottom:4px;">ВОПРОС 1 из 5</div>
        <div style="font-size:16px;font-weight:600;color:#fff;margin-bottom:6px;">Бюджет на пробную партию одного SKU</div>
        <div style="font-size:12px;color:#555;margin-bottom:16px;">Включает закупку + доставку карго из Китая</div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;" id="q1-options">
          <div onclick="selectOption('q1','micro')" id="q1-micro" class="q-option" style="background:#0f0f13;border:1px solid #2a2a3a;border-radius:8px;padding:14px;cursor:pointer;">
            <div style="font-size:15px;font-weight:700;color:#4ade80;">до $200</div>
            <div style="font-size:11px;color:#555;margin-top:4px;">200 шт · $0.5-1/шт · цена до 400₽ · расходники</div>
          </div>
          <div onclick="selectOption('q1','low')" id="q1-low" class="q-option" style="background:#0f0f13;border:1px solid #2a2a3a;border-radius:8px;padding:14px;cursor:pointer;">
            <div style="font-size:15px;font-weight:700;color:#38bdf8;">$200 — $500</div>
            <div style="font-size:11px;color:#555;margin-top:4px;">150 шт · $1-3.3/шт · цена 300-1200₽</div>
          </div>
          <div onclick="selectOption('q1','mid')" id="q1-mid" class="q-option" style="background:#0f0f13;border:1px solid #2a2a3a;border-radius:8px;padding:14px;cursor:pointer;">
            <div style="font-size:15px;font-weight:700;color:#fbbf24;">$500 — $1500</div>
            <div style="font-size:11px;color:#555;margin-top:4px;">100 шт · $3-15/шт · цена 800-5000₽</div>
          </div>
          <div onclick="selectOption('q1','high')" id="q1-high" class="q-option" style="background:#0f0f13;border:1px solid #2a2a3a;border-radius:8px;padding:14px;cursor:pointer;">
            <div style="font-size:15px;font-weight:700;color:#f59e0b;">$1500 — $3000</div>
            <div style="font-size:11px;color:#555;margin-top:4px;">50 шт · $15-60/шт · цена 3500-18500₽</div>
          </div>
          <div onclick="selectOption('q1','premium')" id="q1-premium" class="q-option" style="background:#0f0f13;border:1px solid #2a2a3a;border-radius:8px;padding:14px;cursor:pointer;">
            <div style="font-size:15px;font-weight:700;color:#a78bfa;">$3000 — $7000</div>
            <div style="font-size:11px;color:#555;margin-top:4px;">20 шт · $60-350/шт · цена 12000-99000₽</div>
          </div>
        </div>
      </div>

      <!-- Вопрос 2: Цикличность -->
      <div style="background:#1a1a24;border-radius:12px;padding:20px;margin-bottom:16px;">
        <div style="font-size:13px;color:#a78bfa;font-weight:600;margin-bottom:4px;">ВОПРОС 2 из 5</div>
        <div style="font-size:16px;font-weight:600;color:#fff;margin-bottom:6px;">Желаемая скорость оборота капитала</div>
        <div style="font-size:12px;color:#555;margin-bottom:16px;">Доставка из Китая ~45 дней. Товар должен продаться до следующей поставки.</div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;" id="q2-options">
          <div onclick="selectOption('q2','fast')" id="q2-fast" class="q-option" style="background:#0f0f13;border:1px solid #2a2a3a;border-radius:8px;padding:14px;cursor:pointer;">
            <div style="font-size:14px;font-weight:700;color:#4ade80;">8-12 циклов/год</div>
            <div style="font-size:11px;color:#555;margin-top:4px;">Оборот каждые 30-45 дней</div>
          </div>
          <div onclick="selectOption('q2','medium')" id="q2-medium" class="q-option" style="background:#0f0f13;border:1px solid #2a2a3a;border-radius:8px;padding:14px;cursor:pointer;">
            <div style="font-size:14px;font-weight:700;color:#fbbf24;">4-6 циклов/год</div>
            <div style="font-size:11px;color:#555;margin-top:4px;">Оборот каждые 60-90 дней</div>
          </div>
          <div onclick="selectOption('q2','slow')" id="q2-slow" class="q-option" style="background:#0f0f13;border:1px solid #2a2a3a;border-radius:8px;padding:14px;cursor:pointer;">
            <div style="font-size:14px;font-weight:700;color:#f59e0b;">2-4 цикла/год</div>
            <div style="font-size:11px;color:#555;margin-top:4px;">Оборот каждые 90-180 дней</div>
          </div>
        </div>
      </div>

      <!-- Вопрос 3: Сезонность -->
      <div style="background:#1a1a24;border-radius:12px;padding:20px;margin-bottom:16px;">
        <div style="font-size:13px;color:#a78bfa;font-weight:600;margin-bottom:4px;">ВОПРОС 3 из 5</div>
        <div style="font-size:16px;font-weight:600;color:#fff;margin-bottom:6px;">Сезонные товары</div>
        <div style="font-size:12px;color:#555;margin-bottom:16px;">Сезонные товары дают высокую маржу но требуют точного планирования поставок</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;" id="q3-options">
          <div onclick="selectOption('q3','no')" id="q3-no" class="q-option" style="background:#0f0f13;border:1px solid #2a2a3a;border-radius:8px;padding:14px;cursor:pointer;">
            <div style="font-size:14px;font-weight:700;color:#4ade80;">Только круглогодичные</div>
            <div style="font-size:11px;color:#555;margin-top:4px;">Стабильный спрос весь год, меньше рисков</div>
          </div>
          <div onclick="selectOption('q3','yes')" id="q3-yes" class="q-option" style="background:#0f0f13;border:1px solid #2a2a3a;border-radius:8px;padding:14px;cursor:pointer;">
            <div style="font-size:14px;font-weight:700;color:#fbbf24;">Включая сезонные</div>
            <div style="font-size:11px;color:#555;margin-top:4px;">Готовы планировать заранее, выше маржа</div>
          </div>
        </div>
      </div>

      <!-- Вопрос 4: Конкуренция -->
      <div style="background:#1a1a24;border-radius:12px;padding:20px;margin-bottom:16px;">
        <div style="font-size:13px;color:#a78bfa;font-weight:600;margin-bottom:4px;">ВОПРОС 4 из 5</div>
        <div style="font-size:16px;font-weight:600;color:#fff;margin-bottom:6px;">Уровень конкуренции</div>
        <div style="font-size:12px;color:#555;margin-bottom:16px;">Свободные ниши легче войти, конкурентные — выше оборот</div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;" id="q4-options">
          <div onclick="selectOption('q4','low')" id="q4-low" class="q-option" style="background:#0f0f13;border:1px solid #2a2a3a;border-radius:8px;padding:14px;cursor:pointer;">
            <div style="font-size:13px;font-weight:700;color:#4ade80;">Свободные ниши</div>
            <div style="font-size:11px;color:#555;margin-top:4px;">до 50 активных продавцов</div>
          </div>
          <div onclick="selectOption('q4','mid')" id="q4-mid" class="q-option" style="background:#0f0f13;border:1px solid #2a2a3a;border-radius:8px;padding:14px;cursor:pointer;">
            <div style="font-size:13px;font-weight:700;color:#fbbf24;">Умеренная</div>
            <div style="font-size:11px;color:#555;margin-top:4px;">50-300 активных продавцов</div>
          </div>
          <div onclick="selectOption('q4','high')" id="q4-high" class="q-option" style="background:#0f0f13;border:1px solid #2a2a3a;border-radius:8px;padding:14px;cursor:pointer;">
            <div style="font-size:13px;font-weight:700;color:#f59e0b;">Готовы конкурировать</div>
            <div style="font-size:11px;color:#555;margin-top:4px;">300+ продавцов, высокий спрос</div>
          </div>
        </div>
      </div>

      <!-- Вопрос 5: Исключения -->
      <div style="background:#1a1a24;border-radius:12px;padding:20px;margin-bottom:24px;">
        <div style="font-size:13px;color:#a78bfa;font-weight:600;margin-bottom:4px;">ВОПРОС 5 из 5</div>
        <div style="font-size:16px;font-weight:600;color:#fff;margin-bottom:6px;">Приоритетные направления</div>
        <div style="font-size:12px;color:#555;margin-bottom:16px;">Выберите 2-4 направления в которых хотите работать</div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;" id="q5-options">
          ${[
            {cat:'Здоровье и медицина', icon:'🏥', desc:'тонометры, бандажи, витамины'},
            {cat:'Красота и уход', icon:'💄', desc:'косметика, уход за кожей и волосами'},
            {cat:'Одежда и аксессуары', icon:'👕', desc:'одежда, сумки, украшения'},
            {cat:'Дом и интерьер', icon:'🏠', desc:'декор, текстиль, организация'},
            {cat:'Канцелярия и офис', icon:'📚', desc:'канцтовары, расходники, орг.техника'},
            {cat:'Детские товары', icon:'🧸', desc:'игрушки, одежда, аксессуары'},
            {cat:'Спорт и активный отдых', icon:'🏋️', desc:'тренажёры, экипировка, питание'},
            {cat:'Инструменты и хозтовары', icon:'🔧', desc:'инструменты, уборка, ремонт'},
            {cat:'Электроника и гаджеты', icon:'📱', desc:'аксессуары, умный дом, гаджеты'},
            {cat:'Автотовары', icon:'🚗', desc:'аксессуары, уход, запчасти'}
          ].map(item =>
            '<div onclick="toggleDirection(this,this.dataset.cat)" data-cat="'+item.cat+'" style="background:#0f0f13;border:1px solid #2a2a3a;border-radius:8px;padding:12px;cursor:pointer;">' +
            '<div style="font-size:20px;margin-bottom:4px;">'+item.icon+'</div>' +
            '<div style="font-size:12px;font-weight:600;color:#888;margin-bottom:2px;">'+item.cat+'</div>' +
            '<div style="font-size:10px;color:#444;">'+item.desc+'</div>' +
            '</div>'
          ).join('')}
        </div>
        <div style="font-size:11px;color:#444;margin-top:10px;">* Если не выбрано ни одного — ищем по всем направлениям</div>
      </div>

      <!-- Кнопка -->
      <button onclick="runPortfolioAnalysis()" style="width:100%;background:linear-gradient(135deg,#6c63ff,#8b5cf6);color:#fff;border:none;border-radius:10px;padding:16px;font-size:15px;font-weight:700;cursor:pointer;">
        🚀 Подобрать портфель
      </button>

      <div id="portfolio-result" style="margin-top:24px;"></div>
    </div>
  `;
}

function selectOption(question, value) {
  // Убираем выделение со всех вариантов вопроса
  document.querySelectorAll('#' + question + '-options .q-option').forEach(el => {
    el.style.borderColor = '#2a2a3a';
    el.style.background = '#0f0f13';
  });
  // Выделяем выбранный
  var selected = document.getElementById(question + '-' + value);
  if (selected) {
    selected.style.borderColor = '#6c63ff';
    selected.style.background = '#6c63ff11';
  }
  // Сохраняем ответ
  if (!window.portfolioAnswers) window.portfolioAnswers = {};
  window.portfolioAnswers[question] = value;
}

function toggleExclude(el, cat) {
  toggleDirection(el, cat);
}

function toggleDirection(el, cat) {
  if (!window.portfolioAnswers) window.portfolioAnswers = {};
  if (!window.portfolioAnswers.q5) window.portfolioAnswers.q5 = [];
  var idx = window.portfolioAnswers.q5.indexOf(cat);
  // Максимум 4 направления
  if (idx >= 0) {
    window.portfolioAnswers.q5.splice(idx, 1);
    el.style.borderColor = '#2a2a3a';
    el.querySelector('div:nth-child(2)').style.color = '#888';
    el.style.background = '#0f0f13';
  } else {
    if (window.portfolioAnswers.q5.length >= 4) {
      alert('Выберите не более 4 направлений');
      return;
    }
    window.portfolioAnswers.q5.push(cat);
    el.style.borderColor = '#6c63ff';
    el.querySelector('div:nth-child(2)').style.color = '#a78bfa';
    el.style.background = '#6c63ff11';
  }
}

async function runPortfolioAnalysis() {
  var answers = window.portfolioAnswers || {};
  // Проверяем что ответили на обязательные вопросы
  if (!answers.q1 || !answers.q2 || !answers.q3 || !answers.q4) {
    alert('Пожалуйста ответьте на все вопросы 1-4');
    return;
  }

  var resultDiv = document.getElementById('portfolio-result');
  resultDiv.innerHTML = '<div style="background:#0f0f13;border-radius:12px;padding:30px;text-align:center;"><div style="font-size:32px;margin-bottom:12px;">🤖</div><div style="font-size:14px;color:#aaa;">Claude анализирует 7000+ ниш и подбирает портфель...</div><div style="font-size:12px;color:#555;margin-top:8px;">Обычно занимает 20-30 секунд</div></div>';

  // Scroll to result
  resultDiv.scrollIntoView({behavior:'smooth'});

  try {
    var resp = await fetch('/portfolio-ai', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({answers: answers, currency: currentCurrency, rate: rates[currentCurrency], symbol: symbols[currentCurrency]})
    });
    var data = await resp.json();
    if (data.error) throw new Error(data.error);
    renderPortfolioResult(data, symbols[currentCurrency], rates[currentCurrency]);
  } catch(e) {
    resultDiv.innerHTML = '<div style="color:#ef4444;padding:16px;background:#1a0a0a;border-radius:8px;">❌ ' + e.message + '</div>';
  }
}

function renderPortfolioResult(data, sym, rate) {
  var div = document.getElementById('portfolio-result');
  var fmtM = function(rub) {
    if (!rub) return '—';
    var v = Math.round(rub * rate);
    if (v >= 1000000) return (v/1000000).toFixed(1) + ' млн ' + sym;
    if (v >= 1000) return (v/1000).toFixed(0) + ' тыс ' + sym;
    return v + ' ' + sym;
  };

  var niches = data.niches || [];
  var summary = data.summary || {};

  var html = '<div style="border-top:1px solid #1a1a2e;padding-top:24px;">';

  // Заголовок с итогами
  html += '<div style="background:linear-gradient(135deg,#1a1a2e,#0f0f1a);border-radius:12px;padding:20px;margin-bottom:24px;border:1px solid #6c63ff33;">' +
    '<div style="font-size:10px;color:#6c63ff;letter-spacing:1px;margin-bottom:8px;">РЕКОМЕНДАЦИЯ AI</div>' +
    '<div style="font-size:17px;font-weight:700;color:#fff;margin-bottom:10px;">' + (summary.title||'Подобранный портфель') + '</div>' +
    '<div style="font-size:13px;color:#aaa;line-height:1.6;margin-bottom:16px;">' + (summary.description||'') + '</div>' +
    '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;">' +
      '<div style="background:#0f0f13;border-radius:8px;padding:10px;text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">НИШ В ПОРТФЕЛЕ</div><div style="font-size:18px;font-weight:700;color:#a78bfa;">' + niches.length + '</div></div>' +
      '<div style="background:#0f0f13;border-radius:8px;padding:10px;text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">БЮДЖЕТ ВХОДА</div><div style="font-size:16px;font-weight:700;color:#38bdf8;">' + fmtM(summary.total_budget_rub) + '</div></div>' +
      '<div style="background:#0f0f13;border-radius:8px;padding:10px;text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">ПОТЕНЦИАЛ/МЕС</div><div style="font-size:16px;font-weight:700;color:#4ade80;">' + fmtM(summary.monthly_potential_rub) + '</div></div>' +
      '<div style="background:#0f0f13;border-radius:8px;padding:10px;text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">ОКУПАЕМОСТЬ</div><div style="font-size:16px;font-weight:700;color:#fbbf24;">' + (summary.payback_months||'—') + ' мес</div></div>' +
    '</div>' +
  '</div>';

  // Карточки ниш
  html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">';
  niches.forEach(function(n, i) {
    var priorityColor = n.priority === 'high' ? '#4ade80' : n.priority === 'medium' ? '#fbbf24' : '#38bdf8';
    var priorityLabel = n.priority === 'high' ? '🔥 Высокий приоритет' : n.priority === 'medium' ? '⭐ Средний приоритет' : '🌱 На перспективу';
    html += '<div style="background:#1a1a24;border-radius:12px;padding:16px;border-left:3px solid ' + priorityColor + ';">' +
      '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;">' +
        '<div>' +
          '<div style="font-size:13px;font-weight:700;color:#fff;margin-bottom:4px;">' + (n.name||'') + '</div>' +
          '<div style="font-size:11px;color:' + priorityColor + ';">' + priorityLabel + '</div>' +
        '</div>' +
        '<button onclick="openNicheFromPortfolio(this)" data-full="' + (n.full||n.name||'') + '" style="background:#6c63ff22;border:1px solid #6c63ff44;border-radius:6px;padding:4px 10px;color:#a78bfa;font-size:11px;cursor:pointer;">Открыть</button>' +
      '</div>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px;">' +
        '<div style="background:#0f0f13;border-radius:6px;padding:8px;text-align:center;"><div style="font-size:9px;color:#555;margin-bottom:2px;">ОБОРОТ</div><div style="font-size:13px;font-weight:700;color:#38bdf8;">' + (n.turnover_days||'—') + ' дн</div></div>' +
        '<div style="background:#0f0f13;border-radius:6px;padding:8px;text-align:center;"><div style="font-size:9px;color:#555;margin-bottom:2px;">МАРЖА</div><div style="font-size:13px;font-weight:700;color:#a78bfa;">' + (n.margin_pct||'—') + '%</div></div>' +
        '<div style="background:#0f0f13;border-radius:6px;padding:8px;text-align:center;"><div style="font-size:9px;color:#555;margin-bottom:2px;">ВЫКУП</div><div style="font-size:13px;font-weight:700;color:#4ade80;">' + (n.buyout_pct||'—') + '%</div></div>' +
      '</div>' +
      '<div style="background:#0f0f13;border-radius:8px;padding:10px;margin-bottom:10px;">' +
        '<div style="display:flex;justify-content:space-between;margin-bottom:6px;font-size:11px;"><span style="color:#555;">Вход (закупка+доставка)</span><span style="color:#fff;">' + fmtM(n.entry_cost_rub) + '</span></div>' +
        '<div style="display:flex;justify-content:space-between;margin-bottom:6px;font-size:11px;"><span style="color:#555;">Реклама старт</span><span style="color:#fff;">' + fmtM(n.ad_budget_rub) + '</span></div>' +
        '<div style="display:flex;justify-content:space-between;font-size:11px;border-top:1px solid #2a2a3a;padding-top:6px;"><span style="color:#aaa;">Потенциал/цикл</span><span style="color:#4ade80;font-weight:700;">' + fmtM(n.profit_per_cycle_rub) + '</span></div>' +
      '</div>' +
      '<div style="font-size:11px;color:#555;line-height:1.5;">' + (n.reason||'') + '</div>' +
      (n.seasonal_warning ? '<div style="margin-top:8px;background:#fbbf2422;border:1px solid #fbbf2444;border-radius:6px;padding:10px;font-size:11px;color:#fbbf24;"><div style="font-weight:600;margin-bottom:3px;">&#127810; Сезонный товар</div><div style="color:#aaa;">' + n.seasonal_warning + '</div></div>' : '') +
    '</div>';
  });
  html += '</div>';

  // Кнопка сохранить портфель
  html += '<div style="margin-top:20px;display:flex;gap:12px;">' +
  '<button onclick="resetPortfolioForm()" style="flex:1;background:#1a1a24;border:1px solid #2a2a3a;border-radius:8px;padding:12px;color:#888;cursor:pointer;font-size:13px;">&#128260; Изменить параметры</button>' +
  '<button onclick="runPortfolioAnalysis()" style="flex:1;background:#6c63ff22;border:1px solid #6c63ff44;border-radius:8px;padding:12px;color:#a78bfa;cursor:pointer;font-size:13px;">&#128260; Показать другие варианты</button>' +
  '</div>';
  html += '</div>';
  div.innerHTML = html;
}

async function showTopNiches() {
  hideAll();
  setActiveMenu(event.target);
  const topDiv = document.getElementById('top-niches');
  topDiv.style.display = 'block';
  topDiv.innerHTML = '<div style="color:#555;padding:20px">Загружаем топ ниш...</div>';
  const r = await fetch('/top-niches?offset=' + topNichesOffset);
  const data = await r.json();
  topDiv.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;"><div style="font-size:20px;font-weight:700;color:#fff;">Топ ниши по потенциалу</div><button onclick="refreshTopNiches()" style="background:#1a1a24;border:1px solid #2a2a3a;border-radius:8px;padding:8px 16px;color:#888;cursor:pointer;font-size:13px;">🔄 Показать другие</button></div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;">
      ${data.map(n => `
        <div onclick="setQuery('${n.full}')" style="background:#1a1a24;border:1px solid #2a2a3a;border-radius:12px;padding:16px;cursor:pointer;" onmouseover="this.style.borderColor='#6c63ff'" onmouseout="this.style.borderColor='#2a2a3a'">
          <div style="font-size:15px;font-weight:600;color:#fff;margin-bottom:8px">${n.full}</div>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="font-size:13px;color:#555">${fmt(n.revenue/2)}/год</div>
            <div style="font-size:18px;font-weight:700;color:${n.score>=65?'#22c55e':n.score>=40?'#eab308':'#ef4444'}">${n.score}</div>
          </div>
        </div>
      `).join('')}
    </div>
  `;
}
function showCalc() {
  hideAll();
  loadCalcRates();
  setActiveMenu(event.target);
  document.getElementById('calculator').style.display = 'block';
}
async function showCatalog() {
  hideAll();
  setActiveMenu(event.target);
  document.getElementById('catalog').style.display = 'block';
  // search-box остаётся видимым в каталоге
  if (catalogData.length > 0) { filterCatalog(); return; }
  document.getElementById('cat-list').innerHTML = '<div style="color:#555;padding:20px">Загружаем ниши...</div>';
  const r = await fetch('/catalog');
  catalogData = await r.json();
  buildCatChips();
  filterCatalog();
}

let activeCatFilter = 'Все';

function buildCatChips() {
  const cats = {'Все': catalogData.length};
  catalogData.forEach(n => {
    const cat = n.category || 'Другое';
    cats[cat] = (cats[cat] || 0) + 1;
  });
  const order = ['Все','Женщинам','Мужчинам','Обувь','Дом','Электроника','Автотовары','Для ремонта','Бытовая техника','Красота','Спорт','Детям','Сад и дача','Зоотовары','Продукты','Аксессуары','Мебель','Канцтовары','Здоровье','Книги','Игрушки','Товары для взрослых','Ювелирные изделия','Транспортные средства','Акции'];
  const sorted = order.filter(k => cats[k]).concat(Object.keys(cats).filter(k => !order.includes(k) && k !== 'Все'));
  document.getElementById('cat-chips').innerHTML = sorted.map(cat => {
    const active = activeCatFilter === cat;
    return `<span onclick="setCatFilter('${cat}')" style="cursor:pointer;padding:6px 14px;border-radius:20px;font-size:12px;white-space:nowrap;border:1px solid ${active ? '#6c63ff' : '#2a2a3a'};background:${active ? '#6c63ff22' : 'transparent'};color:${active ? '#a78bfa' : '#666'};">${cat} <span style="color:#444;">${cats[cat] || 0}</span></span>`;
  }).join('');
}

function setCatFilter(cat) {
  activeCatFilter = cat;
  buildCatChips();
  filterCatalog();
}

function filterCatalog() {
  const search = document.getElementById('cat-search').value.toLowerCase();
  const sort = document.getElementById('cat-sort').value;
  let data = catalogData.filter(n => {
    const matchSearch = n.name.toLowerCase().includes(search);
    const matchCat = activeCatFilter === 'Все' || (n.category || 'Другое') === activeCatFilter;
    return matchSearch && matchCat;
  });
  if (sort === 'revenue') data.sort((a,b) => b.revenue - a.revenue);
  else if (sort === 'orders') data.sort((a,b) => b.orders - a.orders);
  else if (sort === 'profit') data.sort((a,b) => b.profit_pct - a.profit_pct);
  else if (sort === 'buyout') data.sort((a,b) => b.buyout_pct - a.buyout_pct);
  else if (sort === 'turnover') data.sort((a,b) => a.turnover - b.turnover);
  document.getElementById('cat-stats').textContent = `Показано ${data.length} ниш`;
  document.getElementById('cat-list').innerHTML = data.map(n => `
    <div onclick="selectFromCatalog('${n.full}')" style="background:#1a1a24;border:1px solid #2a2a3a;border-radius:10px;padding:16px;margin-bottom:8px;cursor:pointer;display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center;" onmouseover="this.style.borderColor='#6c63ff'" onmouseout="this.style.borderColor='#2a2a3a'">
      <div>
        <div style="font-size:15px;color:#fff;font-weight:500;margin-bottom:6px;">${activeCatFilter !== 'Все' && n.name.includes(' / ') ? n.name.split(' / ').slice(1).join(' / ') : n.name}</div>
        <div style="display:flex;gap:16px;flex-wrap:wrap;">
          <span style="font-size:12px;color:#555;">${fmt(n.revenue/2)}/год</span>
          <span style="font-size:12px;color:#555;">${n.sellers} продавцов</span>
          <span style="font-size:12px;color:#555;">выкуп ${Math.round(n.buyout_pct*100)}%</span>
          <span style="font-size:12px;color:#555;">маржа ${Math.round(n.profit_pct*100)}% до себест.</span>
        </div>
      </div>
      <div style="text-align:right;">
        <div style="font-size:20px;font-weight:700;color:#fff;">${getScoreColor(n)}</div>
        <div style="font-size:11px;color:#555;">потенциал</div>
      </div>
    </div>
  `).join('');
}

function getScoreColor(n) {
  const activity = n.sellers_with_sales / (n.sellers || 1);
  if (activity >= 0.7 && n.profit_pct >= 0.2 && n.buyout_pct >= 0.7) return '🟢';
  if (activity >= 0.4 && n.profit_pct >= 0.1) return '🟡';
  return '🔴';
}

function selectFromCatalog(full) {
  document.getElementById('catalog').style.display = 'none';
  document.getElementById('query').value = clean_name_js(full);
  setQuery(full);
}

function clean_name_js(name) {
  return name;
}
let revenueChartInstance = null;
let salesChartInstance = null;

async function loadCharts(name) {
  try {
    const r = await fetch('/charts?q=' + encodeURIComponent(name));
    const data = await r.json();
    
    const loadingEl = document.getElementById('chart-loading');
    if (loadingEl) loadingEl.style.display = 'none';
    
    if (data.error || !data.labels || data.labels.length === 0) return;
    
    window._chartData = data;
    
    if (revenueChartInstance) revenueChartInstance.destroy();
    if (salesChartInstance) salesChartInstance.destroy();
    
    const gridColor = '#1a1a2e';
    const tickColor = '#555';
    const commonScales = {
      x: { ticks: { color: tickColor, font: { size: 11 } }, grid: { color: gridColor } },
      y: { ticks: { color: tickColor, font: { size: 11 } }, grid: { color: gridColor } }
    };
    
    const maxRev = Math.max(...data.revenue);
    
    revenueChartInstance = new Chart(document.getElementById('revenueChart'), {
      type: 'bar',
      data: {
        labels: data.labels,
        datasets: [{
          label: 'Выручка (млн ₽)',
          data: data.revenue,
          backgroundColor: data.revenue.map(v => v === maxRev ? '#38bdf8' : '#0ea5e966'),
          borderColor: data.revenue.map(v => v === maxRev ? '#7dd3fc' : '#38bdf8'),
          borderWidth: 1,
          borderRadius: 4
        }]
      },
      options: {
        responsive: true,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: ctx => ctx.parsed.y + ' млн ₽'
            }
          }
        },
        scales: commonScales
      }
    });
    
    salesChartInstance = new Chart(document.getElementById('salesChart'), {
      type: 'line',
      data: {
        labels: data.labels,
        datasets: [{
          label: 'Заказов',
          data: data.sales,
          borderColor: '#4ade80',
          backgroundColor: '#4ade8015',
          fill: true,
          tension: 0.4,
          pointRadius: 4,
          pointBackgroundColor: '#4ade80',
          pointBorderColor: '#4ade80',
          borderWidth: 2
        }]
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: commonScales
      }
    });
    // График распределения цен
    if (document.getElementById('priceChart') && data.price_labels) {
      if (window.priceChartInstance) window.priceChartInstance.destroy();
      const prRate = rates[currentCurrency];
      const prSym = symbols[currentCurrency];
      const convertedPriceLabels = data.price_labels.map(label => {
        return label.replace(/\d+/g, n => Math.round(parseInt(n) * prRate).toLocaleString('ru')) + ' ' + prSym;
      });
      window.priceChartInstance = new Chart(document.getElementById('priceChart'), {
        type: 'bar',
        data: {
          labels: convertedPriceLabels,
          datasets: [{
            data: data.price_data,
            backgroundColor: ['#fbbf24aa','#fbbf24bb','#fbbf24cc','#fbbf24dd','#fbbf24ee','#fbbf24'],
            borderColor: '#fde68a',
            borderWidth: 1,
            borderRadius: 4
          }]
        },
        options: {
          responsive: true,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: '#555', font: { size: 10 } }, grid: { color: '#1a1a2e' } },
            y: { ticks: { color: '#555', font: { size: 10 } }, grid: { color: '#1a1a2e' } }
          }
        }
      });
    }

    // График топ продавцов (doughnut)
    if (document.getElementById('sellersChart') && data.seller_labels) {
      if (window.sellersChartInstance) window.sellersChartInstance.destroy();
      const sellerPct = data.seller_pct || data.seller_data;
      window.sellersChartInstance = new Chart(document.getElementById('sellersChart'), {
        type: 'doughnut',
        data: {
          labels: data.seller_labels,
          datasets: [{
            data: sellerPct,
            backgroundColor: [
              '#06b6d4', '#f97316', '#fbbf24', '#4ade80',
              '#38bdf8', '#a78bfa', '#fb7185', '#34d399',
              '#6b7280'
            ],
            borderColor: '#0f0f13',
            borderWidth: 2
          }]
        },
        options: {
          responsive: true,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: ctx => ' ' + ctx.label + ': ' + ctx.parsed + '%'
              }
            }
          }
        }
      });
    }

    // Статистика продавцов
    if (document.getElementById('sellersStats') && data.seller_labels && data.seller_pct) {
      const colors = ['#06b6d4','#f97316','#fbbf24','#4ade80','#38bdf8','#a78bfa','#fb7185','#34d399','#6b7280'];
      let html = '<div style="font-size:10px;color:#555;margin-bottom:6px;letter-spacing:1px;">ДОЛЯ РЫНКА</div>';
      data.seller_labels.forEach((label, i) => {
        const pct = data.seller_pct[i] || 0;
        const color = colors[i] || '#6b7280';
        html += `<div style="display:flex;align-items:center;gap:4px;margin-bottom:5px;">
          <div style="width:7px;height:7px;border-radius:50%;background:${color};flex-shrink:0;"></div>
          <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#aaa;font-size:11px;flex:1;">${label}</div>
          <div style="color:#fff;font-weight:600;font-size:11px;white-space:nowrap;text-align:right;min-width:40px;">${pct}%</div>
        </div>`;
      });
      document.getElementById('sellersStats').innerHTML = html;
    }

    // Блок топ товаров
    if (document.getElementById('topItemsContent') && data.top_items && data.top_items.length > 0) {
      let html = '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:12px;">';
      html += '<tr style="color:#555;border-bottom:1px solid #2a2a3a;">';
      html += '<th style="text-align:left;padding:6px 8px;">#</th>';
      html += '<th style="text-align:left;padding:6px 8px;">Товар</th>';
      html += '<th style="text-align:left;padding:6px 8px;">Продавец</th>';
      html += '<th style="text-align:right;padding:6px 8px;">Цена</th>';
      html += '<th style="text-align:right;padding:6px 8px;">Выручка</th>';
      html += '<th style="text-align:right;padding:6px 8px;">Продажи</th>';
      html += '<th style="text-align:right;padding:6px 8px;">Рейтинг</th>';
      html += '<th style="text-align:center;padding:6px 8px;">Артикул WB</th>';
      html += '</tr>';
      data.top_items.forEach((item, i) => {
        html += `<tr style="border-bottom:1px solid #1a1a2e;cursor:pointer;" onmouseover="this.style.background='#1a1a2e'" onmouseout="this.style.background=''">`; 
        html += `<td style="padding:8px;color:#555;">${i+1}</td>`;
        html += `<td style="padding:8px;color:#ddd;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${item.name}</td>`;
        html += `<td style="padding:8px;color:#888;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${item.seller}</td>`;
        html += `<td style="padding:8px;color:#fff;text-align:right;">${fmtCurrency(item.price)}</td>`;
        html += `<td style="padding:8px;color:#38bdf8;text-align:right;">${fmtCurrency(item.revenue * 1000)}</td>`;
        html += `<td style="padding:8px;color:#4ade80;text-align:right;">${item.sales.toLocaleString('ru')}</td>`;
        html += `<td style="padding:8px;color:#fbbf24;text-align:right;">${item.rating > 0 ? '★ ' + item.rating : '—'}</td>`;
        html += `<td style="padding:8px;text-align:center;">${item.url ? '<a href="' + item.url + '" target="_blank" style="color:#6c63ff;text-decoration:none;font-size:11px;">' + item.id + '</a>' : '—'}</td>`;
        html += '</tr>';
      });
      html += '</table></div>';
      document.getElementById('topItemsContent').innerHTML = html;
    }

    // Блок рекламы
    if (data.warehouse_stats) {
      window._warehouseStats = data.warehouse_stats;
      renderWarehouseMetrics(data);
    }
    if (data.package_data) {
      window._nichePackageData = data.package_data;
    }

    if (data.avg_cpm !== undefined) {
      if (!window._chartData) window._chartData = {};
      window._chartData.avg_cpm = data.avg_cpm;
      window._chartData.ad_pct = data.ad_pct;
      window._chartData.cpm_status = data.cpm_status;
      window._chartData.ad_verdict = data.ad_verdict;
      window._chartData.top_ad_sellers = data.top_ad_sellers;
      // Обновляем компактные метрики
      var cpmColors = {green:'#4ade80', yellow:'#fbbf24', red:'#ef4444'};
      var cpmVal = document.getElementById('metric-cpm-val');
      var cpmSub = document.getElementById('metric-cpm-sub');
      var adpctVal = document.getElementById('metric-adpct-val');
      var adpctSub = document.getElementById('metric-adpct-sub');
      if (cpmVal) cpmVal.textContent = data.avg_cpm > 0 ? data.avg_cpm + ' ₽' : 'Нет данных';
      if (cpmVal) cpmVal.style.color = data.avg_cpm > 0 ? cpmColors[data.cpm_status] : '#555';
      if (cpmSub) cpmSub.textContent = data.avg_cpm > 0 ? data.cpm_label : 'MPStats не предоставляет';
      if (adpctVal) adpctVal.textContent = data.ad_pct + '%';
      if (adpctVal) adpctVal.style.color = cpmColors[data.ad_pct_status];
      if (adpctSub) adpctSub.textContent = data.ad_pct < 30 ? 'низкая конкуренция' : data.ad_pct < 60 ? 'умеренная' : 'высокая';
    }

    if (data.warehouse_stats) {
      window._warehouseStats = data.warehouse_stats;
      renderWarehouseMetrics(data);
    }
    if (data.package_data) {
      window._nichePackageData = data.package_data;
    }

    if (data.avg_cpm !== undefined) {
      if (!window._chartData) window._chartData = {};
      window._chartData.avg_cpm = data.avg_cpm;
      window._chartData.ad_pct = data.ad_pct;
      window._chartData.cpm_status = data.cpm_status;
      window._chartData.ad_verdict = data.ad_verdict;
      window._chartData.top_ad_sellers = data.top_ad_sellers;
      // Обновляем компактные метрики
      var cpmColors = {green:'#4ade80', yellow:'#fbbf24', red:'#ef4444'};
      var cpmVal = document.getElementById('metric-cpm-val');
      var cpmSub = document.getElementById('metric-cpm-sub');
      var adpctVal = document.getElementById('metric-adpct-val');
      var adpctSub = document.getElementById('metric-adpct-sub');
      if (cpmVal) cpmVal.textContent = data.avg_cpm > 0 ? data.avg_cpm + ' ₽' : 'Нет данных';
      if (cpmVal) cpmVal.style.color = data.avg_cpm > 0 ? cpmColors[data.cpm_status] : '#555';
      if (cpmSub) cpmSub.textContent = data.avg_cpm > 0 ? data.cpm_label : 'MPStats не предоставляет';
      if (adpctVal) adpctVal.textContent = data.ad_pct + '%';
      if (adpctVal) adpctVal.style.color = cpmColors[data.ad_pct_status];
      if (adpctSub) adpctSub.textContent = data.ad_pct < 30 ? 'низкая конкуренция' : data.ad_pct < 60 ? 'умеренная' : 'высокая';
    }

    if (document.getElementById('adContent') && data.avg_cpm !== undefined) {
      const cpmColors = { green: '#4ade80', yellow: '#fbbf24', red: '#ef4444' };
      const cpmEmoji = { green: '🟢', yellow: '🟡', red: '🔴' };
      let adHtml = `
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:16px;">
          <div style="background:#0f0f13;border-radius:8px;padding:12px;">
            <div style="font-size:10px;color:#555;margin-bottom:4px;letter-spacing:1px;">СРЕДНИЙ CPM</div>
            <div style="font-size:22px;font-weight:700;color:#fff;">${data.avg_cpm > 0 ? data.avg_cpm + ' ₽' : 'Нет данных'}</div>
            <div style="font-size:11px;color:${data.avg_cpm > 0 ? cpmColors[data.cpm_status] : '#555'};margin-top:4px;">${data.avg_cpm > 0 ? cpmEmoji[data.cpm_status] + ' ' + data.cpm_label : 'MPStats не предоставляет'}</div>
          </div>
          <div style="background:#0f0f13;border-radius:8px;padding:12px;">
            <div style="font-size:10px;color:#555;margin-bottom:4px;letter-spacing:1px;">ТОВАРОВ С РЕКЛАМОЙ</div>
            <div style="font-size:22px;font-weight:700;color:#fff;">${data.ad_pct}%</div>
            <div style="font-size:11px;color:${cpmColors[data.ad_pct_status]};margin-top:4px;">${cpmEmoji[data.ad_pct_status]} ${data.ad_pct < 30 ? 'Низкая конкуренция' : data.ad_pct < 60 ? 'Умеренная конкуренция' : 'Высокая конкуренция'}</div>
          </div>
          <div style="background:#0f0f13;border-radius:8px;padding:12px;">
            <div style="font-size:10px;color:#555;margin-bottom:4px;letter-spacing:1px;">РЕКЛАМНАЯ НАГРУЗКА</div>
            <div style="font-size:14px;font-weight:700;color:${data.ad_verdict_color};margin-top:8px;">${data.ad_verdict}</div>
          </div>
        </div>`;
      document.getElementById('adContent').innerHTML = adHtml;
    }

    // График прогноза — последние 6 месяцев факта + 3 месяца прогноза
    if (document.getElementById('forecastChart') && data.forecast_labels && data.forecast_labels.length > 0) {
      if (window.forecastChartInstance) window.forecastChartInstance.destroy();
      const last6rev = data.revenue.slice(-6);
      const last6labels = data.labels.slice(-6);
      const allLabels = [...last6labels, ...data.forecast_labels];
      const allRevenue = [...last6rev, ...Array(data.forecast_labels.length).fill(null)];
      // Связующая точка — последнее фактическое значение
      const forecastFull = [...Array(last6rev.length - 1).fill(null), last6rev[last6rev.length - 1], ...data.forecast_data];
      // Связующая точка
      forecastFull[data.revenue.length - 1] = data.revenue[data.revenue.length - 1];

      // Сохраняем оригинальные данные в рублях для конвертации
      window._chartData.revenue_rub = last6rev;
      window._chartData.forecast_data_rub = data.forecast_data;
      window._chartData.price_labels_rub = data.price_labels;

      // Конвертируем данные в текущую валюту
      const fcRate = rates[currentCurrency];
      const fcSym = symbols[currentCurrency];
      const allRevenueConverted = allRevenue.map(v => v === null ? null : +(v * fcRate).toFixed(2));
      const forecastFullConverted = forecastFull.map(v => v === null ? null : +(v * fcRate).toFixed(2));

      window.forecastChartInstance = new Chart(document.getElementById('forecastChart'), {
        type: 'line',
        data: {
          labels: allLabels,
          datasets: [
            {
              label: 'Факт (млн ' + fcSym + ')',
              data: allRevenueConverted,
              borderColor: '#ffffff',
              backgroundColor: '#ffffff10',
              fill: true,
              tension: 0.4,
              pointRadius: 3,
              pointBackgroundColor: '#ffffff',
              borderWidth: 2
            },
            {
              label: 'Прогноз (млн ' + fcSym + ')',
              data: forecastFullConverted,
              borderColor: '#00d4ff',
              backgroundColor: '#00d4ff10',
              fill: true,
              tension: 0.4,
              pointRadius: 4,
              pointBackgroundColor: '#00d4ff',
              borderWidth: 2,
              borderDash: [6, 3]
            }
          ]
        },
        options: {
          responsive: true,
          plugins: {
            legend: {
              display: true,
              labels: { color: '#666', font: { size: 11 } }
            },
            tooltip: { callbacks: { label: ctx => ctx.dataset.label + ': ' + ctx.parsed.y + ' млн ₽' } }
          },
          scales: {
            x: { ticks: { color: '#555', font: { size: 10 } }, grid: { color: '#1a1a2e' } },
            y: { ticks: { color: '#555', font: { size: 10 } }, grid: { color: '#1a1a2e' } }
          }
        }
      });
    }

    // График тренда
    if (document.getElementById('trendChart') && data.revenue.length >= 4) {
      const half = Math.floor(data.revenue.length / 2);
      const avg1 = data.revenue.slice(0, half).reduce((a,b) => a+b, 0) / half;
      const avg2 = data.revenue.slice(half).reduce((a,b) => a+b, 0) / (data.revenue.length - half);
      const trendUp = avg2 > avg1;
      const diff = ((avg2 - avg1) / avg1 * 100).toFixed(1);
      const trendColor = trendUp ? '#f97316' : '#ef4444';
      window._trendColor = trendColor;
      const trendSign = trendUp ? '+' : '';

      const titleEl = document.getElementById('trend-title');
      if (titleEl) titleEl.innerHTML = (trendUp ? '📈' : '📉') + ' Тренд ниши <span style="color:' + trendColor + ';font-size:13px;">' + (trendUp ? '▲' : '▼') + ' ' + trendSign + diff + '%</span> <span style="font-size:10px;color:#555;">(млн ₽)</span>';

      // Тренд ниши — только исторические данные за 2 года, без прогноза
      if (window.trendChartInstance) window.trendChartInstance.destroy();
      window.trendChartInstance = new Chart(document.getElementById('trendChart'), {
        type: 'line',
        data: {
          labels: data.labels,
          datasets: [{
            data: data.revenue,
            borderColor: trendColor,
            backgroundColor: trendUp ? '#f9731615' : '#ef444415',
            fill: true,
            tension: 0.4,
            pointRadius: 3,
            pointBackgroundColor: trendColor,
            borderWidth: 2
          }]
        },
        options: {
          responsive: true,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: '#555', font: { size: 10 } }, grid: { color: '#1a1a2e' } },
            y: { ticks: { color: '#555', font: { size: 10 } }, grid: { color: '#1a1a2e' } }
          }
        }
      });
    }

  } catch(e) {
    console.error('Charts error:', e);
  }
}

function goHome() {
  hideAll();
  document.querySelector('.search-box').style.display = 'block';
  document.getElementById('query').value = '';
  document.querySelectorAll('.sidebar-item').forEach(t => t.classList.remove('active'));
  document.querySelector('.sidebar-item').classList.add('active');
  var sp = document.getElementById('sticky-agents');
  if (sp) sp.style.display = 'none';
}

let modalChartInstance = null;

function closeModal(event) {
  if (event.target.id === 'chartModal') {
    document.getElementById('chartModal').classList.remove('active');
    if (modalChartInstance) { modalChartInstance.destroy(); modalChartInstance = null; }
  }
}

function openChartModal(title, type, labels, data, color, isHorizontal) {
  if (!labels || !data || !document.getElementById('modalTitle')) return;
  document.getElementById('modalTitle').textContent = title;
  document.getElementById('chartModal').style.display = 'flex';
  if (modalChartInstance) { modalChartInstance.destroy(); modalChartInstance = null; }
  const modalCanvas = document.getElementById('modalChart');
  if (type === 'price') {
    modalChartInstance = new Chart(modalCanvas, {
      type: 'bar',
      data: { labels: labels, datasets: [{ data: data, backgroundColor: ['#fbbf24aa','#fbbf24bb','#fbbf24cc','#fbbf24dd','#fbbf24ee','#fbbf24'], borderColor: '#fde68a', borderWidth: 1, borderRadius: 4 }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#888', font: { size: 12 } }, grid: { color: '#1a1a2e' } }, y: { ticks: { color: '#888', font: { size: 12 } }, grid: { color: '#1a1a2e' } } } }
    });
    return;
  }
  if (type === 'forecast') {
    const allLabels = [...window._chartData.labels, ...window._chartData.forecast_labels];
    const allRevenue = [...window._chartData.revenue, ...Array(window._chartData.forecast_labels.length).fill(null)];
    const forecastFull = [...Array(window._chartData.revenue.length - 1).fill(null), window._chartData.revenue[window._chartData.revenue.length-1], ...window._chartData.forecast_data];
    modalChartInstance = new Chart(modalCanvas, {
      type: 'line',
      data: { labels: allLabels, datasets: [
        { data: allRevenue, borderColor: '#ffffff', backgroundColor: '#ffffff11', borderWidth: 2, tension: 0.4, fill: true, pointRadius: 3, pointBackgroundColor: '#ffffff' },
        { data: forecastFull, borderColor: '#38bdf8', backgroundColor: 'transparent', borderWidth: 2, borderDash: [6,4], tension: 0.4, fill: false, pointRadius: 4, pointBackgroundColor: '#38bdf8' }
      ]},
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#888', font: { size: 11 } }, grid: { color: '#1a1a2e' } }, y: { ticks: { color: '#888', font: { size: 11 } }, grid: { color: '#1a1a2e' } } } }
    });
    return;
  }
  if (type === 'doughnut') {
    const doughnutColors = ['#06b6d4','#f97316','#fbbf24','#4ade80','#38bdf8','#a78bfa','#fb7185','#34d399','#6b7280'];
    modalChartInstance = new Chart(modalCanvas, {
      type: 'doughnut',
      data: { labels: labels, datasets: [{ data: data, backgroundColor: doughnutColors, borderColor: '#0f0f13', borderWidth: 2 }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: true, position: 'right', labels: { color: '#aaa', font: { size: 12 }, padding: 16 } }, tooltip: { callbacks: { label: ctx => ' ' + ctx.label + ': ' + ctx.parsed + '%' } } } }
    });
    return;
  }
  const dataset = { data: data, borderRadius: 4, tension: 0.4, fill: true, pointRadius: 4, borderWidth: 2 };
  if (type === 'bar') { dataset.backgroundColor = color + '88'; dataset.borderColor = color; }
  else { dataset.borderColor = color; dataset.backgroundColor = color + '22'; dataset.pointBackgroundColor = color; }
  const options = {
    responsive: true,
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { color: '#888', font: { size: 12 } }, grid: { color: '#1a1a2e' } },
      y: { ticks: { color: '#888', font: { size: 12 } }, grid: { color: '#1a1a2e' } }
    }
  };
  if (isHorizontal) options.indexAxis = 'y';
  const modalCanvasMain = document.getElementById('modalChart');
  modalChartInstance = new Chart(modalCanvasMain, {
    type: type, 
    data: { labels: labels, datasets: [dataset] }, 
    options: {...options, maintainAspectRatio: false}
  });
}

function loadTopChips() {
  const box = document.getElementById('top-chips');
  const recent = JSON.parse(localStorage.getItem('recent_niches') || '[]');
  box.innerHTML = '<span style="font-size:11px;color:#555;align-self:center;">🕐 Недавние:</span>';
  if (recent.length === 0) {
    box.innerHTML += '<span style="font-size:11px;color:#444;">история пуста</span>';
    return;
  }
  recent.slice(0, 7).forEach(n => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = n;
    chip.onclick = () => setQuery(n);
    box.appendChild(chip);
  });
}
function addToRecent(name) {
  let recent = JSON.parse(localStorage.getItem('recent_niches') || '[]');
  recent = recent.filter(n => n !== name);
  recent.unshift(name);
  recent = recent.slice(0, 7);
  localStorage.setItem('recent_niches', JSON.stringify(recent));
  loadTopChips();
loadCalcRates();

window.addEventListener('popstate', function(e) {
  _navigating = true;
  if (e.state && e.state.page === 'niche') {
    document.getElementById('query').value = e.state.query;
    analyze().then(() => { _navigating = false; });
  } else if (e.state && e.state.page === 'catalog') {
    showCatalog(); _navigating = false;
  } else if (e.state && e.state.page === 'top') {
    showTopNiches(); _navigating = false;
  } else {
    goHome(); _navigating = false;
  }
});

}
loadTopChips();
loadCalcRates();

window.addEventListener('popstate', function(e) {
  _navigating = true;
  if (e.state && e.state.page === 'niche') {
    document.getElementById('query').value = e.state.query;
    analyze().then(() => { _navigating = false; });
  } else if (e.state && e.state.page === 'catalog') {
    showCatalog(); _navigating = false;
  } else if (e.state && e.state.page === 'top') {
    showTopNiches(); _navigating = false;
  } else {
    goHome(); _navigating = false;
  }
});


function setQuery(q) {
  const displayName = q.includes(' / ') ? q.split(' / ').slice(1).join(' / ') : q;
  document.getElementById('query').value = q;
  document.getElementById('query').setAttribute('data-display', displayName);
  hideSuggestions();
  analyze();
}
document.getElementById('query').addEventListener('keypress', e => {
  if (e.key === 'Enter') { hideSuggestions(); analyze(); }
});document.getElementById('query').addEventListener('keydown', e => {
  const box = document.getElementById('suggestions');
  if (!box || box.style.display === 'none') return;
  const items = Array.from(box.querySelectorAll('div'));
  if (!items.length) return;
  let idx = items.findIndex(el => el.classList.contains('highlighted'));
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (idx >= 0) items[idx].classList.remove('highlighted');
    idx = Math.min(idx + 1, items.length - 1);
    items[idx].classList.add('highlighted');
    items[idx].style.background = '#2a2a3a';
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (idx >= 0) items[idx].classList.remove('highlighted');
    idx = Math.max(idx - 1, 0);
    items[idx].classList.add('highlighted');
    items[idx].style.background = '#2a2a3a';
  } else if (e.key === 'Enter' && idx >= 0) {
    items[idx].click();
  }
});

document.getElementById('query').addEventListener('input', e => {
  const q = e.target.value.trim();
  const btn = document.getElementById('analyze-btn');
  if (btn) btn.disabled = q.length < 2;
  clearTimeout(suggestTimer);
  if (q.length < 2) { hideSuggestions(); return; }
  suggestTimer = setTimeout(() => loadSuggestions(q), 250);
});

document.addEventListener('click', e => {
  if (!e.target.closest('.search-box')) hideSuggestions();
});

async function loadSuggestions(q) {
  const r = await fetch('/suggest?q=' + encodeURIComponent(q));
  const data = await r.json();
  if (data.length < 2 && q.length >= 3) {
    showSuggestions(data);
    // Запускаем умный поиск если обычный дал мало результатов
    clearTimeout(window._smartTimer);
    window._smartSearchDone = false;
    window._smartSearchQuery = q;
    window._smartTimer = setTimeout(() => {
      // Запускаем только если запрос не изменился
      if (window._smartSearchQuery === q) smartSearch(q);
    }, 800);
  } else {
    // Скрываем умный баннер если обычный поиск дал результаты
    const sb = document.getElementById('smart-banner');
    if (sb) sb.remove();
    showSuggestions(data);
  }
}

async function smartSearch(q) {
  // Показываем индикатор умного поиска
  let box = document.getElementById('suggestions');
  if (!box) {
    box = document.createElement('div');
    box.id = 'suggestions';
    box.style.cssText = 'position:absolute;top:100%;left:0;right:0;background:#1a1a24;border:1px solid #2a2a3a;border-radius:10px;margin-top:4px;overflow:hidden;z-index:100;';
    document.querySelector('.search-row').style.position = 'relative';
    document.querySelector('.search-row').appendChild(box);
  }
  
  // Добавляем строку умного поиска
  const existingContent = box.innerHTML;
  const smartRow = '<div id="smart-search-row" style="padding:10px 16px;color:#555;font-size:12px;display:flex;align-items:center;gap:8px;border-top:1px solid #1f1f2e;"><span style="animation:spin 1s linear infinite;display:inline-block;">🔍</span> Ищем семантически через AI...</div>';
  box.innerHTML = existingContent + smartRow;
  box.style.display = 'block';

  try {
    const r = await fetch('/smart-search?q=' + encodeURIComponent(q));
    const data = await r.json();
    
    // Удаляем строку загрузки
    const smartRowEl = document.getElementById('smart-search-row');
    if (smartRowEl) smartRowEl.remove();

    // Убираем старый баннер если есть
    const oldBanner = document.getElementById('smart-banner');
    if (oldBanner) oldBanner.remove();
    window._smartSearchDone = true;

    if (data.niche) {
      // Показываем умный результат
      const smartResult = document.createElement('div');
      smartResult.id = 'smart-banner';
      smartResult.style.cssText = 'padding:12px 16px;background:#0f0f1a;border-top:1px solid #6c63ff44;cursor:pointer;';
      smartResult.innerHTML = 
        '<div style="font-size:10px;color:#6c63ff;letter-spacing:1px;margin-bottom:4px;">🧠 AI НАШЁЛ СЕМАНТИЧЕСКИ</div>' +
        '<div style="display:flex;justify-content:space-between;align-items:center;">' +
          '<div>' +
            '<span style="color:#a78bfa;font-size:13px;font-weight:600;">' + data.niche_display + '</span>' +
            '<div style="font-size:11px;color:#555;margin-top:2px;">' + data.explanation + '</div>' +
          '</div>' +
          '<span style="color:#555;font-size:12px;">' + fmt(data.revenue) + '</span>' +
        '</div>';
      smartResult.onclick = () => {
        setQuery(data.niche);
        hideSuggestions();
      };
      box.appendChild(smartResult);
    }
  } catch(e) {
    const smartRowEl = document.getElementById('smart-search-row');
    if (smartRowEl) smartRowEl.remove();
  }
}

function showSuggestions(items) {
  let box = document.getElementById('suggestions');
  if (!box) {
    box = document.createElement('div');
    box.id = 'suggestions';
    box.style.cssText = 'position:absolute;top:100%;left:0;right:0;background:#1a1a24;border:1px solid #2a2a3a;border-radius:10px;margin-top:4px;overflow:hidden;z-index:100;';
    document.querySelector('.search-row').style.position = 'relative';
    document.querySelector('.search-row').appendChild(box);
  }
  if (!items.length) { hideSuggestions(); return; }
  box.innerHTML = items.map(i => `
    <div onclick="setQuery('${i.full}')" style="padding:12px 16px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1f1f2e;background:#0f0f13;" 
      <span style="color:#ddd;font-size:14px">${i.name}</span>
      <span style="color:#555;font-size:12px">${fmt(i.revenue)}</span>
    </div>
  `).join('');
  box.style.display = 'block';
}

function hideSuggestions() {
  const box = document.getElementById('suggestions');
  if (box) box.style.display = 'none';
}

async function analyze() {
  hideAll();
  const q = document.getElementById('query').value.trim();
  if (!q) return;
  history.pushState({page: 'niche', query: q}, '', '?q=' + encodeURIComponent(q));
  document.getElementById('loading').style.display = 'block';
  const errEl = document.getElementById('error');
  if(errEl) errEl.style.display = 'none';
  try {
    const r = await fetch('/analyze?q=' + encodeURIComponent(q));
    const data = await r.json();
    document.getElementById('loading').style.display = 'none';
    if (data.error) {
      if (data.not_found) {
        // Запускаем умный поиск автоматически
        const errEl = document.getElementById('error');
        if (errEl) {
          errEl.style.display = 'block';
          errEl.style.color = '#a78bfa';
          errEl.style.background = '#1a1a2e';
          errEl.style.border = '1px solid #6c63ff44';
          errEl.innerHTML = '🧠 Ниша не найдена напрямую — ищем семантически через AI...';
        }
        try {
          const sr = await fetch('/smart-search?q=' + encodeURIComponent(q));
          const sd = await sr.json();
          if (sd.niche) {
            if (errEl) {
              window._smartNiche = sd.niche;
              errEl.innerHTML = '🧠 AI нашёл: <strong style="color:#a78bfa;">' + sd.niche_display + '</strong> — ' + sd.explanation + '. <span style="color:#6c63ff;cursor:pointer;text-decoration:underline;" onclick="setQuery(window._smartNiche);analyze();">Открыть нишу →</span>';
            }
            // Автоматически открываем найденную нишу
            setQuery(sd.niche);
            analyze();
          } else {
            if (errEl) {
              errEl.style.color = '#ef4444';
              errEl.style.background = '';
              errEl.style.border = '';
              errEl.textContent = 'Ниша "' + q + '" не найдена. Попробуйте другой запрос.';
            }
          }
        } catch(se) {
          if (errEl) errEl.textContent = data.error;
        }
        return;
      }
      document.getElementById('error').style.display = 'block';
      document.getElementById('error').textContent = data.error;
      return;
    }
    renderResult(data);
  } catch(e) {
    document.getElementById('loading').style.display = 'none';
    document.getElementById('error').style.display = 'block';
    document.getElementById('error').textContent = 'Ошибка соединения с сервером';
  }
}
const SEASONAL_KEYWORDS = [
  'купальн', 'плавк', 'лыж', 'сноуборд', 'коньк',
  'пуховик', 'шуб', 'дублёнк', 'угги', 'унт',
  'шапк', 'варежк', 'перчатк', 'шарф', 'гетр',
  'свитер', 'термобельё', 'парк', 'плащ', 'капри',
  'новогод', 'ёлочн', 'гирлянд', 'маскарад',
  'зонт', 'дождевик', 'резиновые сапог',
  'садовый', 'огород', 'мангал', 'барбекю',
  'туристическ', 'палатк', 'спальный мешок',
  'парео', 'сарафан', 'шорты пляжн', 'кепи',
  'пасхальн', 'валентин', 'карнавал'
];

function isSeasonal(name) {
  const lower = name.toLowerCase();
  return SEASONAL_KEYWORDS.some(k => lower.includes(k));
}function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(1) + ' млрд ₽';
  if (n >= 1e6) return (n/1e6).toFixed(1) + ' млн ₽';
  if (n >= 1e3) return (n/1e3).toFixed(0) + ' тыс ₽';
  return n + ' ₽';
}

function renderResult(d) {
  window.lastResult = d;
  window.currentNiche = d;
  // Скрываем блоки агентов при новой нише
  var ab = document.getElementById('adBlock');
  if (ab) ab.style.display = 'none';
  var wb = document.getElementById('warehouseBlock');
  if (wb) wb.style.display = 'none';
  const verdictMap = {BUY: 'Рекомендуем входить', TEST: 'Тестовая закупка', SKIP: 'Не рекомендуем'};
  const insights = d.insights.map((t,i) => `<div class="insight-item"><div class="insight-num">${i+1}</div><div class="insight-text">${t}</div></div>`).join('');
  const hyps = d.hypotheses.map(t => `<div class="hyp-item">${t}</div>`).join('');
  document.getElementById('result').innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
        <div class="niche-name" style="margin-bottom:0;">${d.name}</div>
        <span style="background:${d.score>=65?'#22c55e22':d.score>=40?'#eab30822':'#ef444422'};border:1px solid ${d.score>=65?'#22c55e':d.score>=40?'#eab308':'#ef4444'};border-radius:6px;padding:3px 10px;font-size:12px;font-weight:700;color:${d.score>=65?'#22c55e':d.score>=40?'#eab308':'#ef4444'};">${d.verdict} ${d.score}/100</span>
        ${isSeasonal(d.name) ? '<span style="font-size:12px;color:#eab308;">&#127810; сезонный</span>' : ''}
        ${d.data_warning ? '<span style="font-size:11px;color:#ef4444;background:#ef444422;border-radius:4px;padding:2px 6px;">&#9888; неточные данные</span>' : ''}
      </div>
    </div>
    </div>

    <!-- ЗОНА 1: Метрики -->
    <div class="metrics-grid">
      <div class="metric-card"><div class="metric-label">Выручка ниши</div><div class="metric-tooltip">Оценочная выручка всех продавцов за 12 месяцев (данные за ~2 года из БД, делённые на 2). Показывает размер рынка.</div><div class="metric-value">${fmtCurrency(d.revenue_annual || d.revenue / 2)}</div><div class="metric-sub">за 12 мес</div></div>
      <div class="metric-card"><div class="metric-label">Заказов в месяц</div><div class="metric-tooltip">Среднемесячное количество заказов в нише. Чем больше — тем активнее спрос.</div><div class="metric-value">${d.orders.toLocaleString('ru')}</div><div class="metric-sub">${(d.orders/30).toFixed(0)} в день</div></div>
      <div class="metric-card"><div class="metric-label">Продавцов</div><div class="metric-tooltip">Общее число продавцов и тех кто реально продаёт. Низкий % активных = высокая конкуренция среди немногих.</div><div class="metric-value">${d.sellers.toLocaleString('ru')}</div><div class="metric-sub">${d.sellers_with_sales} с продажами</div></div>
      <div class="metric-card"><div class="metric-label">Выкуп</div><div class="metric-tooltip">Процент заказов которые покупатель не вернул. Низкий выкуп = высокие затраты на логистику возвратов.</div><div class="metric-value">${(d.buyout_pct*100).toFixed(0)}%</div><div class="metric-sub">${d.buyout_pct >= 0.8 ? 'отличный' : d.buyout_pct >= 0.6 ? 'хороший' : 'низкий'}</div></div>
      <div class="metric-card"><div class="metric-label">Оборачиваемость (реальная)</div><div class="metric-tooltip">Среднее время продажи партии товара по данным MPStats (остаток / среднедневные продажи). Значение 1-2 дня = товар продаётся очень быстро. Чем меньше — тем быстрее оборот капитала.</div><div class="metric-value">${(() => { const real = d.buyout_pct > 0 ? Math.round(d.turnover / d.buyout_pct) : Math.round(d.turnover); return real > 365 ? "365+" : real; })()} дн</div><div class="metric-sub">${(() => { const real = d.buyout_pct > 0 ? Math.round(d.turnover / d.buyout_pct) : Math.round(d.turnover); return real <= 45 ? '<span class="turn-fast">🟢 быстро</span>' : real <= 90 ? '<span class="turn-seasonal">🟡 умеренно</span>' : '<span class="turn-slow">🔴 медленно</span>'; })()} <span style="font-size:10px;color:#444;">MPStats: ${Math.round(d.turnover)} дн</span></div></div>
      <div class="metric-card"><div class="metric-label">Маржинальность</div><div class="metric-tooltip">Доля прибыли после вычета комиссии WB и логистики, но ДО себестоимости товара.</div><div class="metric-value">${(d.profit_pct*100).toFixed(0)}%</div><div class="metric-sub">${d.profit_pct >= 0.35 ? 'высокая' : d.profit_pct >= 0.2 ? 'средняя' : 'низкая'} <span style="font-size:10px;color:#444;">до себест.</span></div></div>
      <div class="metric-card" id="metric-cpm"><div class="metric-label">Средний CPM</div><div class="metric-tooltip">Стоимость 1000 показов рекламы в нише. Чем ниже — тем дешевле реклама.</div><div class="metric-value" id="metric-cpm-val">—</div><div class="metric-sub" id="metric-cpm-sub">загружаем...</div></div>
      <div class="metric-card" id="metric-adpct"><div class="metric-label">Товаров с рекламой</div><div class="metric-tooltip">Доля товаров которые продвигаются платной рекламой. Высокий % = высокая рекламная конкуренция.</div><div class="metric-value" id="metric-adpct-val">—</div><div class="metric-sub" id="metric-adpct-sub">загружаем...</div></div>
      <div class="metric-card"><div class="metric-label">Упущенная выручка</div><div class="metric-tooltip">Выручка которую ниша теряет из-за дефицита товаров. Высокий % = возможность войти и занять долю рынка.</div><div class="metric-value" style="color:${(d.lost_revenue_pct||0) > 0.3 ? '#ef4444' : (d.lost_revenue_pct||0) > 0.15 ? '#fbbf24' : '#4ade80'};">${d.lost_revenue_pct !== undefined ? (d.lost_revenue_pct*100).toFixed(0) + '%' : '—'}</div><div class="metric-sub">${(d.lost_revenue_pct||0) > 0.3 ? 'высокий потенциал' : (d.lost_revenue_pct||0) > 0.15 ? 'умеренный' : 'низкий'}</div></div>
    </div>

    <!-- ЗОНА 3: Главный широкий график -->
    <div class="chart-card" style="margin-bottom:16px;" onclick="openChartModal('📈 Динамика выручки — топ 100 товаров', 'bar', window._chartData.labels, window._chartData.revenue, '#38bdf8', false)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
        <div class="chart-title" style="margin:0;">📈 Динамика выручки и продаж — топ 100 товаров</div>
        <div id="chart-loading" style="font-size:12px;color:#555;">⏳ Загружаем данные...</div>
        <div style="font-size:11px;color:#555;">🔍 нажмите для увеличения</div>
      </div>
      <canvas id="revenueChart" height="80"></canvas>
    </div>

    <!-- ЗОНА 4: Два графика в ряд -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;">
      <div class="chart-card" style="height:320px;" onclick="openChartModal('📦 Сезонность заказов', 'line', window._chartData.labels, window._chartData.sales, '#4ade80', false)">
        <div class="chart-title">📦 Сезонность заказов <span style="font-size:10px;color:#555;">(шт)</span> <span style="font-size:10px;color:#555;">🔍</span></div>
        <canvas id="salesChart" height="140"></canvas>
      </div>
      <div class="chart-card" style="cursor:default;">
        <div class="chart-title">📊 Ключевые показатели</div>
        <div style="margin-top:8px;">
          <div class="metric-row"><div class="metric-row-label">Продавцов активных</div><div class="metric-row-value">${Math.round(d.sellers_with_sales/d.sellers*100)}%</div></div>
          <div class="metric-row-bar" style="margin-bottom:12px;"><div class="metric-row-fill" style="width:${Math.round(d.sellers_with_sales/d.sellers*100)}%;background:#00d4ff"></div></div>
          <div class="metric-row"><div class="metric-row-label">Выкуп заказов</div><div class="metric-row-value">${Math.round(d.buyout_pct*100)}%</div></div>
          <div class="metric-row-bar" style="margin-bottom:12px;"><div class="metric-row-fill" style="width:${Math.round(d.buyout_pct*100)}%;background:#22c55e"></div></div>
          <div class="metric-row"><div class="metric-row-label">Прибыльность</div><div class="metric-row-value">${Math.round(d.profit_pct*100)}%</div></div>
          <div class="metric-row-bar" style="margin-bottom:12px;"><div class="metric-row-fill" style="width:${Math.round(d.profit_pct*100)}%;background:#f59e0b"></div></div>
          <div class="metric-row"><div class="metric-row-label">Рейтинг товаров</div><div class="metric-row-value">⭐ ${d.avg_rating.toFixed(1)}</div></div>
          <div class="metric-row"><div class="metric-row-label">Средняя цена</div><div class="metric-row-value">${fmtCurrency(d.avg_price)}</div></div>
          <div class="metric-row"><div class="metric-row-label">Комиссия WB</div><div class="metric-row-value">${d.commission}%</div></div>
        </div>
      </div>
    </div>

    <!-- ЗОНА 4б: Распределение цен + Тренд -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;">
      <div class="chart-card" style="height:320px;" onclick="openChartModal('💰 Распределение цен', 'price', window._chartData.price_labels, window._chartData.price_data, '#fbbf24', false)">
        <div class="chart-title">💰 Распределение цен <span style="font-size:10px;color:#555;">(% товаров по цене) 🔍</span></div>
        <canvas id="priceChart" height="110"></canvas>
      </div>
      <div class="chart-card" id="trend-card" style="height:320px;" onclick="openChartModal(document.getElementById('trend-title').textContent, 'line', window._chartData.labels, window._chartData.revenue, window._trendColor||'#22c55e', false)">
        <div class="chart-title" id="trend-title">📉 Тренд ниши <span style="font-size:10px;color:#555;">🔍</span></div>
        <canvas id="trendChart" height="110"></canvas>
      </div>
    </div>

    <!-- ЗОНА 4в+4г: Топ продавцы + Прогноз -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;align-items:stretch;">
    <div class="chart-card" style="display:flex;flex-direction:column;height:320px;" onclick="openChartModal('🏆 Топ продавцы ниши', 'doughnut', window._chartData.seller_labels, window._chartData.seller_pct || window._chartData.seller_data, '#06b6d4', false)">
      <div class="chart-title">🏆 Топ продавцы ниши (доля рынка) <span style="font-size:10px;color:#555;">🔍</span></div>
      <div style="display:flex;gap:8px;align-items:center;height:260px;">
        <div style="height:250px;width:250px;position:relative;flex-shrink:0;"><canvas id="sellersChart"></canvas></div>
        <div id="sellersStats" style="width:260px;flex-shrink:0;margin-left:auto;"></div>
      </div>
    </div>

    <!-- ЗОНА 4г: Прогноз продаж -->
    <div class="chart-card" style="height:320px;margin-bottom:16px;" onclick="openChartModal('🔮 Прогноз выручки на 3 месяца', 'forecast', window._chartData.labels, window._chartData.revenue, '#38bdf8', false)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
        <div class="chart-title" style="margin:0;">🔮 Прогноз выручки на 3 месяца <span style="font-size:10px;color:#555;">🔍</span></div>
        <div style="font-size:11px;color:#555;">на основе сезонности + тренда</div>
      </div>
      <canvas id="forecastChart" height="110"></canvas>
    </div>
    </div>

    <!-- ЗОНА 4д: Реклама WB -->
    <div class="chart-card" id="adBlock" style="margin-bottom:16px;display:none;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
        <div class="chart-title" style="margin:0;">📢 Анализ рекламы WB</div>
        <button id="adStrategyBtn" onclick="runAdAnalysis()" style="background:transparent;color:#6c63ff;border:1px solid #6c63ff44;border-radius:6px;padding:5px 10px;font-size:11px;cursor:pointer;opacity:0.7;">🎯 стратегия</button>
      </div>
      <div id="adContent" style="margin-top:12px;"></div>
      <div id="adStrategyContent" style="margin-top:16px;"></div>
      <div id="adMonitorContent" style="margin-top:12px;"></div>
    </div>

    <!-- ЗОНА 4е: Топ товаров ниши -->
    <div class="chart-card" id="topItemsBlock" style="margin-bottom:16px;">
      <div class="chart-title">🏅 Топ товаров ниши <span style="font-size:10px;color:#555;">по выручке за период</span></div>
      <div id="topItemsContent" style="margin-top:12px;"></div>
    </div>

    <!-- ЗОНА 4ж: Агент географии складов -->
    <div class="chart-card" id="warehouseBlock" style="margin-bottom:16px;display:none;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
        <div class="chart-title" style="margin:0;">🏭 Стратегия поставок</div>
        <button id="warehouseBtn" onclick="runWarehouseAnalysis()" style="background:transparent;color:#38bdf8;border:1px solid #38bdf844;border-radius:6px;padding:5px 10px;font-size:11px;cursor:pointer;opacity:0.7;">📦 анализ</button>
      </div>
      <div id="warehouseMetrics" style="margin-top:12px;"></div>
      <div id="warehouseStrategy" style="margin-top:16px;"></div>
    </div>

    <!-- ЗОНА 5: AI Инсайты -->
    <div class="ai-card">
      <div class="ai-header"><div class="ai-dot"></div><div class="ai-title">AI Инсайты</div></div>
      <div class="section-title">Ключевые наблюдения</div>
      ${insights}
      ${hyps ? `<div class="section-title" style="margin-top:20px">Гипотезы для проверки</div>${hyps}` : ''}
    </div>
    <!-- ЗОНА 6: Глубокий анализ -->
    <div id="deep-analysis-block" style="display:none;margin-top:24px;">
      <div class="ai-card">
        <div class="ai-header">
          <div class="ai-dot" style="background:#22c55e;"></div>
          <div class="ai-title">🔍 Глубокий анализ</div>
          <div id="deep-loading" style="display:none;margin-left:12px;font-size:12px;color:#555;">Анализируем нишу...</div>
        </div>
        <div id="deep-content"></div>
      </div>
    </div>

    <!-- ЗОНА 7: Юнит-экономика -->
    <div id="unit-economy-block" style="display:none;margin-top:24px;">
      <div class="ai-card">
        <div class="ai-header">
          <div class="ai-dot" style="background:#f59e0b;"></div>
          <div class="ai-title">🧮 Юнит-экономика</div>
          <div id="unit-loading" style="display:none;margin-left:12px;font-size:12px;color:#555;">Рассчитываем...</div>
        </div>
        <div id="unit-input-block" style="margin-top:16px;"></div>
        <div id="unit-result-block" style="margin-top:16px;"></div>
      </div>
    </div>

    <!-- ЗОНА 9: Поставщики -->
    <div id="supplier-block" style="display:none;margin-top:24px;">
      <div class="ai-card">
        <div class="ai-header">
          <div class="ai-dot" style="background:#34d399;"></div>
          <div class="ai-title">&#127981; Поиск поставщиков и цены закупки</div>
          <div id="supplier-loading" style="display:none;margin-left:12px;font-size:12px;color:#555;">Ищем...</div>
        </div>
        <div id="supplier-content" style="margin-top:16px;"></div>
      </div>
    </div>

    <!-- ЗОНА 8: Документы и сертификация -->
    <div id="docs-block" style="display:none;margin-top:24px;">
      <div class="ai-card">
        <div class="ai-header">
          <div class="ai-dot" style="background:#06b6d4;"></div>
          <div class="ai-title">📋 Документы и сертификация</div>
          <div id="docs-loading" style="display:none;margin-left:12px;font-size:12px;color:#555;">Анализируем...</div>
        </div>
        <div id="docs-content" style="margin-top:16px;"></div>
      </div>
    </div>
  `;
  document.getElementById('result').style.display = 'block';
  loadCharts(d.name);
  addToRecent(d.name);
  var sp = document.getElementById('sticky-agents');
  if (sp) sp.style.display = 'block';
  var swb = document.getElementById('sticky-wl-btn');
  if (swb) {
    var inWl = isInWatchlist(d.full||d.name);
    swb.textContent = inWl ? '📌 В работе' : '🔖 В работе';
    swb.style.borderColor = inWl ? '#6c63ff' : '#2a2a3a';
    swb.style.color = inWl ? '#a78bfa' : '#888';
  }
}

function toggleStickyWL(btn) {
  var n = window.currentNiche;
  if (!n) return;
  toggleWatchlist(n.full||n.name, n.name, n.revenue||0);
  var inWl = isInWatchlist(n.full||n.name);
  btn.textContent = inWl ? '📌 В работе' : '🔖 В работе';
  btn.style.borderColor = inWl ? '#6c63ff' : '#2a2a3a';
  btn.style.color = inWl ? '#a78bfa' : '#888';
}


async function runSupplierAnalysis() {
  const d = window.currentNiche;
  if (!d) return;
  const block = document.getElementById('supplier-block');
  const container = document.getElementById('supplier-content');
  const loading = document.getElementById('supplier-loading');
  if (block) { block.style.display = 'block'; block.scrollIntoView({behavior:'smooth'}); }
  if (loading) loading.style.display = 'block';
  container.innerHTML = '<div style="padding:20px;text-align:center;color:#555;font-size:13px;">&#127981; Ищем поставщиков на Alibaba, 1688, Taobao...</div>';
  try {
    const resp = await fetch('/supplier-analysis', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({niche_name: d.name, display_name: d.display_name||d.name, avg_price: d.avg_price||0, currency: currentCurrency, rate: rates[currentCurrency], symbol: symbols[currentCurrency]})
    });
    const result = await resp.json();
    if (result.error) throw new Error(result.error);
    if (loading) loading.style.display = 'none';
    renderSupplierResult(result);
  } catch(e) {
    if (loading) loading.style.display = 'none';
    container.innerHTML = '<div style="color:#ef4444;padding:12px;background:#1a0a0a;border-radius:8px;">&#10060; ' + e.message + '</div>';
  }
}

function renderSupplierResult(data) {
  const container = document.getElementById('supplier-content');
  const sym = symbols[currentCurrency];
  const rate = rates[currentCurrency];
  var html = '';
  html += '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;border-left:3px solid #34d399;">';
  html += '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:8px;">ДИАПАЗОН ЦЕН ЗАКУПКИ</div>';
  html += '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px;">';
  html += '<div style="text-align:center;background:#1a1a24;border-radius:8px;padding:10px;"><div style="font-size:10px;color:#555;margin-bottom:4px;">TAOBAO/1688</div><div style="font-size:16px;font-weight:700;color:#34d399;">$' + (data.price_taobao_usd||'—') + '</div><div style="font-size:10px;color:#555;">самая низкая</div></div>';
  html += '<div style="text-align:center;background:#1a1a24;border-radius:8px;padding:10px;"><div style="font-size:10px;color:#555;margin-bottom:4px;">ALIBABA</div><div style="font-size:16px;font-weight:700;color:#fbbf24;">$' + (data.price_alibaba_usd||'—') + '</div><div style="font-size:10px;color:#555;">оптовая</div></div>';
  html += '<div style="text-align:center;background:#1a1a24;border-radius:8px;padding:10px;"><div style="font-size:10px;color:#555;margin-bottom:4px;">MOQ</div><div style="font-size:16px;font-weight:700;color:#a78bfa;">' + (data.moq||'—') + ' шт</div><div style="font-size:10px;color:#555;">мин. партия</div></div>';
  html += '</div>';
  html += '<div style="font-size:12px;color:#aaa;line-height:1.6;">' + (data.summary||'') + '</div>';
  html += '</div>';
  if (data.search_links && data.search_links.length > 0) {
    html += '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;">';
    html += '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:10px;">ССЫЛКИ ДЛЯ ПОИСКА</div>';
    data.search_links.forEach(function(link) {
      html += '<a href="' + link.url + '" target="_blank" style="display:flex;align-items:center;gap:10px;background:#1a1a24;border-radius:8px;padding:10px;text-decoration:none;margin-bottom:8px;">';
      html += '<div style="font-size:18px;">' + link.icon + '</div>';
      html += '<div><div style="font-size:12px;color:#fff;font-weight:600;">' + link.platform + '</div><div style="font-size:11px;color:#555;">' + link.description + '</div></div>';
      html += '<div style="margin-left:auto;font-size:11px;color:#34d399;">&#8594; открыть</div></a>';
    });
    html += '</div>';
  }
  if (data.real_margin_pct) {
    var mc = data.real_margin_pct >= 30 ? '#4ade80' : data.real_margin_pct >= 15 ? '#fbbf24' : '#ef4444';
    html += '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;">';
    html += '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:10px;">РЕАЛЬНАЯ МАРЖИНАЛЬНОСТЬ (после себестоимости)</div>';
    html += '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;">';
    html += '<div style="text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">МАРЖА</div><div style="font-size:20px;font-weight:700;color:' + mc + ';">' + data.real_margin_pct + '%</div></div>';
    html += '<div style="text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">ROI</div><div style="font-size:20px;font-weight:700;color:' + mc + ';">' + (data.roi_pct||'—') + '%</div></div>';
    html += '<div style="text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">ПРИБЫЛЬ/ЕД</div><div style="font-size:20px;font-weight:700;color:' + mc + ';">' + Math.round((data.profit_per_unit_rub||0)*rate) + ' ' + sym + '</div></div>';
    html += '</div></div>';
  }
  // Блок ручного ввода и применения цен
  html += '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-top:12px;">';
  html += '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:10px;">ВВЕСТИ РЕАЛЬНУЮ ЦЕНУ ЗАКУПКИ</div>';
  html += '<div style="font-size:11px;color:#555;margin-bottom:12px;">Укажите фактическую цену после переговоров с поставщиком — все расчёты пересчитаются автоматически</div>';
  html += '<div style="display:grid;grid-template-columns:1fr 1fr auto;gap:8px;align-items:end;">';
  html += '<div><div style="font-size:10px;color:#555;margin-bottom:4px;">ЦЕНА ЗАКУПКИ ($/шт)</div><input id="real-price-usd" type="number" step="0.1" placeholder="например 3.5" value="' + (data.price_taobao_usd||'') + '" style="width:100%;background:#1a1a24;border:1px solid #2a2a3a;border-radius:6px;padding:8px;color:#fff;font-size:13px;box-sizing:border-box;"></div>';
  html += '<div><div style="font-size:10px;color:#555;margin-bottom:4px;">КОЛ-ВО В ПАРТИИ (шт)</div><input id="real-batch-qty" type="number" placeholder="например 100" value="100" style="width:100%;background:#1a1a24;border:1px solid #2a2a3a;border-radius:6px;padding:8px;color:#fff;font-size:13px;box-sizing:border-box;"></div>';
  html += '<button onclick="applyRealPrice()" style="background:#34d399;border:none;border-radius:6px;padding:9px 16px;color:#000;cursor:pointer;font-size:12px;font-weight:700;white-space:nowrap;">Применить</button>';
  html += '</div>';
  html += '<div id="real-price-result" style="margin-top:12px;"></div>';
  html += '</div>';
  html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px;">';
  html += '<button onclick="applySupplierPrice(' + (data.price_taobao_usd||0) + ',' + (data.price_alibaba_usd||0) + ')" style="background:#0a1a0f;border:1px solid #34d39944;border-radius:8px;padding:10px;color:#34d399;cursor:pointer;font-size:12px;">&#10003; Применить цены агента</button>';
  html += '<button onclick="hideSupplierBlock()" style="background:#1a1a24;border:1px solid #2a2a3a;border-radius:8px;padding:10px;color:#555;cursor:pointer;font-size:12px;">Скрыть</button>';
  html += '</div>';
  container.innerHTML = html;
}

function hideSupplierBlock() {
  var el = document.getElementById('supplier-block');
  if (el) el.style.display = 'none';
}

function applySupplierPrice(taobaoUsd, alibabaUsd) {
  var priceUsd = taobaoUsd || alibabaUsd;
  window._supplierPriceUsd = priceUsd;
  window._supplierPriceRub = priceUsd * 90;
  window._supplierPriceApplied = true;
  var d = window.currentNiche;
  if (d) { d.supplier_price_usd = priceUsd; d.supplier_price_rub = priceUsd * 90; }
  showAppliedPriceResult(priceUsd, 100);
}

function applyRealPrice() {
  var priceUsd = parseFloat(document.getElementById('real-price-usd').value) || 0;
  var batchQty = parseInt(document.getElementById('real-batch-qty').value) || 100;
  if (!priceUsd) { alert('Введите цену закупки'); return; }
  window._supplierPriceUsd = priceUsd;
  window._supplierPriceRub = priceUsd * 90;
  window._supplierBatchQty = batchQty;
  window._supplierPriceApplied = true;
  var d = window.currentNiche;
  if (d) { d.supplier_price_usd = priceUsd; d.supplier_price_rub = priceUsd * 90; }
  showAppliedPriceResult(priceUsd, batchQty);
}

function showAppliedPriceResult(priceUsd, batchQty) {
  var d = window.currentNiche;
  if (!d) return;
  var sym = symbols[currentCurrency];
  var rate = rates[currentCurrency];
  var usdRate = 90;
  var priceRub = priceUsd * usdRate;
  var avgPrice = d.avg_price || 0;

  var commission = (d.commission > 1 ? d.commission / 100 : d.commission) || 0.25;
  var wbComm = avgPrice * commission;
  var wbLog = avgPrice < 1000 ? 75 : avgPrice < 5000 ? 120 : 200;
  var profitPerUnit = avgPrice - priceRub - wbComm - wbLog;
  var marginPct = avgPrice > 0 ? Math.round(profitPerUnit / avgPrice * 100) : 0;
  var roi = priceRub > 0 ? Math.round(profitPerUnit / priceRub * 100) : 0;
  var batchCost = priceRub * batchQty;
  var batchProfit = profitPerUnit * batchQty * (d.buyout_pct || 0.8);
  var marginColor = marginPct >= 30 ? '#4ade80' : marginPct >= 15 ? '#fbbf24' : '#ef4444';
  var container = document.getElementById('real-price-result');
  if (!container) return;
  var html = '<div style="background:#1a1a24;border-radius:8px;padding:12px;border-left:3px solid ' + marginColor + ';">';
  html += '<div style="font-size:10px;color:#555;margin-bottom:8px;letter-spacing:1px;">РАСЧЁТ С РЕАЛЬНОЙ ЦЕНОЙ ЗАКУПКИ</div>';
  html += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;">';
  html += '<div style="text-align:center;"><div style="font-size:9px;color:#555;">ЗАКУПКА/ШТ</div><div style="font-size:14px;font-weight:700;color:#fff;">$' + priceUsd.toFixed(1) + '</div><div style="font-size:10px;color:#555;">' + Math.round(priceRub) + ' ₽</div></div>';
  html += '<div style="text-align:center;"><div style="font-size:9px;color:#555;">МАРЖА</div><div style="font-size:14px;font-weight:700;color:' + marginColor + ';">' + marginPct + '%</div></div>';
  html += '<div style="text-align:center;"><div style="font-size:9px;color:#555;">ROI</div><div style="font-size:14px;font-weight:700;color:' + marginColor + ';">' + roi + '%</div></div>';
  html += '<div style="text-align:center;"><div style="font-size:9px;color:#555;">ПРИБЫЛЬ/ПАРТИЯ</div><div style="font-size:14px;font-weight:700;color:' + marginColor + ';">' + Math.round(batchProfit * rate / 1000) + 'K ' + sym + '</div></div>';
  html += '</div>';
  html += '<div style="margin-top:8px;font-size:11px;color:#555;">Партия ' + batchQty + ' шт · Закупка ' + Math.round(batchCost * rate) + ' ' + sym + ' · Прибыль/ед ' + Math.round(profitPerUnit * rate) + ' ' + sym + '</div>';
  html += '</div>';
  container.innerHTML = html;
}

function convertDocsCost(rubStr) {
  // Конвертируем строку с рублями в BYN (1 BYN = 27.7 RUB)
  if (!rubStr) return '—';
  var bynRate = 27.7;
  // Извлекаем числа из строки
  var numbers = rubStr.match(/\d[\d\s]*/g);
  if (!numbers) return rubStr;
  var result = rubStr;
  numbers.forEach(function(numStr) {
    var num = parseInt(numStr.replace(/\s/g,''));
    if (num > 1000) {
      var byn = Math.round(num / bynRate);
      var bynFormatted = byn.toLocaleString('ru');
      result = result.replace(numStr.trim(), bynFormatted);
    }
  });
  return result + ' Br';
}

function openNicheFromPortfolio(btn) {
  var full = btn.getAttribute('data-full');
  if (full) { setQuery(full); analyze(); }
}

function resetPortfolioForm() {
  window.portfolioAnswers = {};
  var div = document.getElementById('portfolio');
  if (div) renderPortfolioQuestionnaire(div);
}

function showPortfolioStub() {
  hideAll();
  setActiveMenu(event.target);
  var div = document.createElement('div');
  div.style.cssText = 'padding:60px 40px;text-align:center;';
  div.innerHTML = '<div style="font-size:48px;margin-bottom:16px;">📦</div><div style="font-size:20px;font-weight:700;color:#fff;margin-bottom:8px;">Товарный портфель</div><div style="font-size:14px;color:#555;">Здесь будет отображаться ваш реальный товарный портфель — товары которые вы закупаете и продаёте на WB.</div><div style="margin-top:24px;background:#6c63ff22;border:1px solid #6c63ff44;border-radius:8px;padding:12px 20px;display:inline-block;font-size:13px;color:#a78bfa;">🚧 В разработке</div>';
  document.getElementById('catalog').style.display = 'block';
  document.getElementById('catalog').innerHTML = div.outerHTML;
}

async function runDocsAnalysis() {
  const d = window.currentNiche;
  if (!d) return;
  const block = document.getElementById('docs-block');
  const container = document.getElementById('docs-content');
  const loading = document.getElementById('docs-loading');
  if (block) { block.style.display = 'block'; block.scrollIntoView({behavior:'smooth'}); }
  if (loading) loading.style.display = 'block';
  container.innerHTML = '<div style="padding:20px;text-align:center;color:#555;font-size:13px;">&#128203; Claude анализирует требования к документам...</div>';
  try {
    const resp = await fetch('/docs-analysis', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        niche_name: d.name,
        display_name: d.display_name || d.name,
        avg_price: d.avg_price || 0,
        commission: d.commission || 0
      })
    });
    const result = await resp.json();
    if (result.error) throw new Error(result.error);
    if (loading) loading.style.display = 'none';
    renderDocsResult(result);
  } catch(e) {
    if (loading) loading.style.display = 'none';
    container.innerHTML = '<div style="color:#ef4444;padding:12px;background:#1a0a0a;border-radius:8px;">❌ ' + e.message + '</div>';
  }
}

function renderDocsResult(data) {
  const container = document.getElementById('docs-content');
  const r = data;
  const complexityColor = {low:'#4ade80', medium:'#fbbf24', high:'#ef4444'};
  const complexityLabel = {low:'🟢 Простая', medium:'🟡 Умеренная', high:'🔴 Сложная'};

  let html = '';

  // Общая сложность
  html += '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;border-left:3px solid ' + (complexityColor[r.complexity]||'#fbbf24') + ';">' +
    '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:6px;">СЛОЖНОСТЬ ОФОРМЛЕНИЯ</div>' +
    '<div style="font-size:16px;font-weight:700;color:' + (complexityColor[r.complexity]||'#fbbf24') + ';margin-bottom:8px;">' + (complexityLabel[r.complexity]||'🟡 Умеренная') + '</div>' +
    '<div style="font-size:13px;color:#ccc;line-height:1.6;">' + (r.summary||'') + '</div>' +
  '</div>';

  // Документы для WB
  if (r.wb_docs && r.wb_docs.length > 0) {
    html += '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;">' +
      '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:10px;">ДОКУМЕНТЫ ДЛЯ WB</div>';
    r.wb_docs.forEach(function(doc) {
      const reqColor = doc.required ? '#ef4444' : '#fbbf24';
      html += '<div style="display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;">' +
        '<div style="min-width:60px;background:' + reqColor + '22;border-radius:4px;padding:2px 6px;font-size:10px;color:' + reqColor + ';text-align:center;white-space:nowrap;">' + (doc.required ? 'Обязательно' : 'Желательно') + '</div>' +
        '<div>' +
          '<div style="font-size:12px;color:#fff;font-weight:600;">' + doc.name + '</div>' +
          '<div style="font-size:11px;color:#555;margin-top:2px;">' + doc.description + '</div>' +
          (doc.cost && doc.cost != '0' ? '<div style="font-size:11px;color:#a78bfa;margin-top:2px;">&#128176; ' + doc.cost + ' ₽</div>' : '') +
          (doc.duration ? '<div style="font-size:11px;color:#38bdf8;margin-top:2px;">&#9201; ' + doc.duration + '</div>' : '') +
          (doc.where_rb ? '<div style="font-size:11px;color:#4ade80;margin-top:4px;">&#127463;&#127486; РБ: ' + doc.where_rb + '</div>' : '') +
          (doc.where_rf ? '<div style="font-size:11px;color:#f59e0b;margin-top:2px;">&#127479;&#127482; РФ: ' + doc.where_rf + '</div>' : '') +
          (doc.better_in ? '<div style="font-size:10px;color:#555;margin-top:2px;">Рекомендуем: ' + doc.better_in + '</div>' : '') +
        '</div>' +
      '</div>';
    });
    html += '</div>';
  }

  // Таможенные документы РБ
  if (r.customs_docs && r.customs_docs.length > 0) {
    html += '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;">' +
      '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:10px;">ТАМОЖНЯ И ВВОЗ В РБ</div>';
    r.customs_docs.forEach(function(doc) {
      html += '<div style="display:flex;gap:8px;margin-bottom:8px;align-items:flex-start;">' +
        '<div style="color:#38bdf8;font-size:14px;">&#128230;</div>' +
        '<div style="font-size:12px;color:#aaa;line-height:1.5;">' + doc + '</div>' +
      '</div>';
    });
    html += '</div>';
  }

  // Документы от китайского поставщика
  if (r.supplier_docs && r.supplier_docs.length > 0) {
    html += '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;">' +
      '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:10px;">ЗАПРОСИТЬ У КИТАЙСКОГО ПОСТАВЩИКА</div>';
    r.supplier_docs.forEach(function(doc) {
      var acceptColor = doc.accepted_in_eaes ? '#4ade80' : '#fbbf24';
      var acceptLabel = doc.accepted_in_eaes ? '✓ Принимается в ЕАЭС' : '⚠ Требует переоформления';
      html += '<div style="margin-bottom:10px;padding:10px;background:#1a1a24;border-radius:8px;">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">' +
          '<div style="font-size:12px;color:#fff;font-weight:600;">' + doc.name + '</div>' +
          '<div style="font-size:10px;color:' + acceptColor + ';">' + acceptLabel + '</div>' +
        '</div>' +
        '<div style="font-size:11px;color:#555;">' + doc.description + '</div>' +
      '</div>';
    });
    html += '</div>';
  }

  // Риски и особенности
  if (r.risks && r.risks.length > 0) {
    html += '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;">' +
      '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:10px;">РИСКИ И ОСОБЕННОСТИ</div>';
    r.risks.forEach(function(risk) {
      html += '<div style="display:flex;gap:8px;margin-bottom:8px;align-items:flex-start;">' +
        '<div style="color:#fbbf24;font-size:14px;">&#9888;</div>' +
        '<div style="font-size:12px;color:#aaa;line-height:1.5;">' + risk + '</div>' +
      '</div>';
    });
    html += '</div>';
  }

  // Итоговые затраты
  html += '<div style="background:#0f0f13;border-radius:10px;padding:16px;">' +
    '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:10px;">ИТОГОВЫЕ ЗАТРАТЫ НА ДОКУМЕНТЫ</div>' +
    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">' +
      '<div style="text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">СТОИМОСТЬ</div><div style="font-size:14px;font-weight:700;color:#84cc16;">'+(r.total_cost_byn||convertDocsCost(r.total_cost||''))+' Br</div><div style="font-size:10px;color:#555;margin-top:2px;">'+(r.total_cost||'')+'</div></div>' +
      '<div style="text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">СРОК</div><div style="font-size:16px;font-weight:700;color:#84cc16;">' + (r.total_duration||'2-4 недели') + '</div></div>' +
    '</div>' +
  '</div>';

  container.innerHTML = html;
}

function clearMonitor() { document.getElementById('adMonitorContent').innerHTML=''; }

// ===== АГЕНТ ЮНИТ-ЭКОНОМИКИ =====
function showUnitEconomy() {
  const block = document.getElementById('unit-economy-block');
  if (!block) return;
  block.style.display = 'block';
  block.scrollIntoView({behavior:'smooth', block:'start'});
  
  const d = window.currentNiche;
  if (!d) return;
  
  // Получаем средние габариты из MPStats если есть
  const avgLen = window._nichePackageData?.avg_length || '';
  const avgWid = window._nichePackageData?.avg_width || '';
  const avgHei = window._nichePackageData?.avg_height || '';
  const avgWgt = window._nichePackageData?.avg_weight || '';

  const sym = symbols[currentCurrency];
  const rate = rates[currentCurrency];

  document.getElementById('unit-input-block').innerHTML = `
    <div style="margin-bottom:20px;">
      <!-- Валюта закупки -->
      <div style="display:flex;gap:8px;margin-bottom:16px;align-items:center;">
        <div style="font-size:12px;color:#555;margin-right:4px;">Валюта закупки:</div>
        <button onclick="setUnitCurrency('cny')" id="ucur-cny" style="background:#1a1a2e;border:1px solid #6c63ff;border-radius:6px;padding:6px 12px;color:#a78bfa;font-size:12px;cursor:pointer;">¥ CNY</button>
        <button onclick="setUnitCurrency('usd')" id="ucur-usd" style="background:#0f0f13;border:1px solid #333;border-radius:6px;padding:6px 12px;color:#555;font-size:12px;cursor:pointer;">$ USD</button>
        <button onclick="setUnitCurrency('rub')" id="ucur-rub" style="background:#0f0f13;border:1px solid #333;border-radius:6px;padding:6px 12px;color:#555;font-size:12px;cursor:pointer;">₽ RUB</button>
        <button onclick="setUnitCurrency('byn')" id="ucur-byn" style="background:#0f0f13;border:1px solid #333;border-radius:6px;padding:6px 12px;color:#555;font-size:12px;cursor:pointer;">Br BYN</button>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:16px;">
        <div>
          <div style="font-size:11px;color:#555;margin-bottom:4px;">ЦЕНА ПРОДАЖИ НА WB (₽)</div>
          <input id="ue-price" type="number" value="${Math.round(d.avg_price||0)}" style="width:100%;background:#0f0f13;border:1px solid #2a2a3a;border-radius:6px;padding:8px;color:#fff;font-size:13px;box-sizing:border-box;">
        </div>
        <div>
          <div style="font-size:11px;color:#555;margin-bottom:4px;">ЗАКУПОЧНАЯ ЦЕНА (<span id="ucur-label">¥</span>)</div>
          <input id="ue-cost" type="number" placeholder="цена закупки" style="width:100%;background:#0f0f13;border:1px solid #2a2a3a;border-radius:6px;padding:8px;color:#fff;font-size:13px;box-sizing:border-box;">
        </div>
        <div>
          <div style="font-size:11px;color:#555;margin-bottom:4px;">КОМИССИЯ WB (%)</div>
          <input id="ue-commission" type="number" value="${Math.round(d.commission||15)}" style="width:100%;background:#0f0f13;border:1px solid #2a2a3a;border-radius:6px;padding:8px;color:#fff;font-size:13px;box-sizing:border-box;">
        </div>
      </div>

      <!-- Габариты -->
      <div style="font-size:11px;color:#555;letter-spacing:1px;margin-bottom:4px;">ГАБАРИТЫ И ВЕС ЕДИНИЦЫ ТОВАРА</div>
      <div style="font-size:10px;color:#444;margin-bottom:8px;">Система рассчитает короб автоматически исходя из количества единиц</div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr 1fr;gap:10px;margin-bottom:16px;">
        <div>
          <div style="font-size:10px;color:#555;margin-bottom:4px;">ДЛИНА (см)</div>
          <input id="ue-length" type="number" value="${avgLen}" placeholder="см" style="width:100%;background:#0f0f13;border:1px solid #2a2a3a;border-radius:6px;padding:8px;color:#fff;font-size:13px;box-sizing:border-box;">
        </div>
        <div>
          <div style="font-size:10px;color:#555;margin-bottom:4px;">ШИРИНА (см)</div>
          <input id="ue-width" type="number" value="${avgWid}" placeholder="см" style="width:100%;background:#0f0f13;border:1px solid #2a2a3a;border-radius:6px;padding:8px;color:#fff;font-size:13px;box-sizing:border-box;">
        </div>
        <div>
          <div style="font-size:10px;color:#555;margin-bottom:4px;">ВЫСОТА (см)</div>
          <input id="ue-height" type="number" value="${avgHei}" placeholder="см" style="width:100%;background:#0f0f13;border:1px solid #2a2a3a;border-radius:6px;padding:8px;color:#fff;font-size:13px;box-sizing:border-box;">
        </div>
        <div>
          <div style="font-size:10px;color:#555;margin-bottom:4px;">ВЕС (кг)</div>
          <input id="ue-weight" type="number" value="${avgWgt}" placeholder="кг" style="width:100%;background:#0f0f13;border:1px solid #2a2a3a;border-radius:6px;padding:8px;color:#fff;font-size:13px;box-sizing:border-box;">
        </div>
        <div>
          <div style="font-size:10px;color:#555;margin-bottom:4px;">ЕД. В КОРОБЕ</div>
          <input id="ue-units" type="number" value="20" placeholder="шт" style="width:100%;background:#0f0f13;border:1px solid #2a2a3a;border-radius:6px;padding:8px;color:#fff;font-size:13px;box-sizing:border-box;">
        </div>
      </div>

      <!-- Способ доставки -->
      <div style="font-size:11px;color:#555;letter-spacing:1px;margin-bottom:8px;">СПОСОБ ДОСТАВКИ ИЗ КИТАЯ</div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:16px;">
        <button onclick="setDelivery('sea')" id="udel-sea" style="background:#1a1a2e;border:1px solid #38bdf8;border-radius:6px;padding:10px;color:#38bdf8;font-size:12px;cursor:pointer;text-align:left;">
          🚢 Море<div style="font-size:10px;color:#555;margin-top:2px;">45-60 дней · $1.5-2.5/кг</div>
        </button>
        <button onclick="setDelivery('rail')" id="udel-rail" style="background:#0f0f13;border:1px solid #333;border-radius:6px;padding:10px;color:#555;font-size:12px;cursor:pointer;text-align:left;">
          🚂 ЖД<div style="font-size:10px;color:#444;margin-top:2px;">18-25 дней · $2-3.5/кг</div>
        </button>
        <button onclick="setDelivery('truck')" id="udel-truck" style="background:#0f0f13;border:1px solid #333;border-radius:6px;padding:10px;color:#555;font-size:12px;cursor:pointer;text-align:left;">
          🚛 Авто<div style="font-size:10px;color:#444;margin-top:2px;">20-30 дней · $3-5/кг</div>
        </button>
      </div>

      <!-- Налогообложение -->
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:20px;">
        <div>
          <div style="font-size:11px;color:#555;margin-bottom:4px;">ТАМОЖЕННАЯ ПОШЛИНА (%)</div>
          <input id="ue-customs" type="number" value="10" style="width:100%;background:#0f0f13;border:1px solid #2a2a3a;border-radius:6px;padding:8px;color:#fff;font-size:13px;box-sizing:border-box;">
        </div>
        <div>
          <div style="font-size:11px;color:#555;margin-bottom:4px;">НДС ИМПОРТ (%)</div>
          <input id="ue-vat" type="number" value="20" style="width:100%;background:#0f0f13;border:1px solid #2a2a3a;border-radius:6px;padding:8px;color:#fff;font-size:13px;box-sizing:border-box;">
        </div>
        <div>
          <div style="font-size:11px;color:#555;margin-bottom:4px;">НАЛОГ УСН (%)</div>
          <input id="ue-tax" type="number" value="6" style="width:100%;background:#0f0f13;border:1px solid #2a2a3a;border-radius:6px;padding:8px;color:#fff;font-size:13px;box-sizing:border-box;">
        </div>
      </div>

      <button onclick="runUnitEconomy()" style="width:100%;background:linear-gradient(135deg,#f59e0b,#d97706);color:#000;border:none;border-radius:8px;padding:12px;font-size:14px;font-weight:700;cursor:pointer;">🧮 Рассчитать все 3 сценария</button>
    </div>`;

  // Устанавливаем дефолты
  window._unitCurrency = 'cny';
  window._unitDelivery = 'sea';
}

function setUnitCurrency(cur) {
  window._unitCurrency = cur;
  const labels = {cny:'¥', usd:'$', rub:'₽', byn:'Br'};
  document.getElementById('ucur-label').textContent = labels[cur] || '¥';
  ['cny','usd','rub','byn'].forEach(c => {
    const btn = document.getElementById('ucur-' + c);
    if (!btn) return;
    if (c === cur) {
      btn.style.background = '#1a1a2e';
      btn.style.borderColor = '#6c63ff';
      btn.style.color = '#a78bfa';
    } else {
      btn.style.background = '#0f0f13';
      btn.style.borderColor = '#333';
      btn.style.color = '#555';
    }
  });
}

function setDelivery(mode) {
  window._unitDelivery = mode;
  ['sea','rail','truck'].forEach(m => {
    const btn = document.getElementById('udel-' + m);
    if (!btn) return;
    if (m === mode) {
      btn.style.background = '#1a1a2e';
      btn.style.borderColor = '#38bdf8';
      btn.style.color = '#38bdf8';
    } else {
      btn.style.background = '#0f0f13';
      btn.style.borderColor = '#333';
      btn.style.color = '#555';
    }
  });
}

async function runUnitEconomy() {
  const d = window.currentNiche;
  if (!d) return;

  const resultBlock = document.getElementById('unit-result-block');
  const loading = document.getElementById('unit-loading');
  if (loading) loading.style.display = 'block';
  resultBlock.innerHTML = '<div style="padding:20px;text-align:center;color:#555;font-size:13px;">⏳ Claude рассчитывает 3 сценария...</div>';

  // Курсы для конвертации закупочной цены в рубли
  const curRates = {cny: 12.5, usd: rates.usd > 0 ? 1/rates.usd : 90, rub: 1, byn: 28};
  const costLocal = parseFloat(document.getElementById('ue-cost')?.value) || 0;
  const costRub = costLocal * (curRates[window._unitCurrency] || 1);

  const payload = {
    niche_name: d.name,
    display_name: d.display_name || d.name,
    price_rub: parseFloat(document.getElementById('ue-price')?.value) || d.avg_price || 0,
    cost_rub: costRub,
    cost_local: costLocal,
    cost_currency: window._unitCurrency,
    commission_pct: parseFloat(document.getElementById('ue-commission')?.value) || (d.commission||15),
    buyout_pct: (d.buyout_pct || 0.7) * 100,
    length_cm: parseFloat(document.getElementById('ue-length')?.value) || 0,
    width_cm: parseFloat(document.getElementById('ue-width')?.value) || 0,
    height_cm: parseFloat(document.getElementById('ue-height')?.value) || 0,
    weight_kg: parseFloat(document.getElementById('ue-weight')?.value) || 0,
    units_per_box: parseFloat(document.getElementById('ue-units')?.value) || 10,
    delivery_mode: window._unitDelivery || 'sea',
    customs_pct: parseFloat(document.getElementById('ue-customs')?.value) || 10,
    vat_pct: parseFloat(document.getElementById('ue-vat')?.value) || 20,
    tax_pct: parseFloat(document.getElementById('ue-tax')?.value) || 6,
    revenue: d.revenue || 0,
    sellers_with_sales: d.sellers_with_sales || 0,
    currency: currentCurrency,
    rate: rates[currentCurrency],
    symbol: symbols[currentCurrency]
  };

  try {
    const resp = await fetch('/unit-economy', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const result = await resp.json();
    if (result.error) throw new Error(result.error);
    if (loading) loading.style.display = 'none';
    renderUnitEconomy(result, symbols[currentCurrency], rates[currentCurrency]);
  } catch(e) {
    if (loading) loading.style.display = 'none';
    resultBlock.innerHTML = '<div style="color:#ef4444;padding:12px;background:#1a0a0a;border-radius:8px;font-size:13px;">❌ ' + e.message + '</div>';
  }
}

function renderUnitEconomy(data, sym, rate) {
  const container = document.getElementById('unit-result-block');
  const scenarios = data.scenarios;
  if (!scenarios) return;

  const fmtM = (rub) => {
    if (!rub && rub !== 0) return '—';
    const v = Math.round(rub * rate);
    return v.toLocaleString('ru-RU') + ' ' + sym;
  };

  const sc_colors = {'s1':'#38bdf8', 's2':'#a78bfa', 's3':'#4ade80'};
  const verdict_colors = {'profit':'#4ade80', 'marginal':'#fbbf24', 'loss':'#ef4444'};
  const verdict_labels = {'profit':'✅ Прибыльно', 'marginal':'⚠️ На грани', 'loss':'❌ Убыток'};

  let html = '<div style="border-top:1px solid #1a1a2e;padding-top:20px;">';

  // Рекомендация Claude
  html += '<div style="background:linear-gradient(135deg,#1a1a2e,#0f0f1a);border-radius:10px;padding:16px;margin-bottom:20px;border:1px solid #f59e0b44;">' +
    '<div style="font-size:10px;color:#f59e0b;letter-spacing:1px;margin-bottom:6px;">🏆 РЕКОМЕНДАЦИЯ AI</div>' +
    '<div style="font-size:15px;font-weight:700;color:#fff;margin-bottom:8px;">' + (data.recommendation?.title||'') + '</div>' +
    '<div style="font-size:13px;color:#ccc;line-height:1.6;">' + (data.recommendation?.detail||'') + '</div>' +
  '</div>';

  // 3 сценария
  html += '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:20px;">';
  ['s1','s2','s3'].forEach(key => {
    const s = scenarios[key];
    if (!s) return;
    const vc = verdict_colors[s.verdict] || '#fbbf24';
    const vl = verdict_labels[s.verdict] || '⚠️';
    html +=
      '<div style="background:#0f0f13;border-radius:10px;padding:16px;border-top:3px solid ' + sc_colors[key] + ';">' +
        '<div style="font-size:11px;color:' + sc_colors[key] + ';font-weight:600;margin-bottom:8px;">' + (s.title||'') + '</div>' +
        '<div style="font-size:12px;color:' + vc + ';font-weight:600;margin-bottom:12px;">' + vl + '</div>' +
        '<div style="display:flex;flex-direction:column;gap:6px;">' +
          '<div style="display:flex;justify-content:space-between;font-size:11px;"><span style="color:#555;">Себест. с логистикой</span><span style="color:#fff;">' + fmtM(s.total_cost_rub) + '</span></div>' +
          '<div style="display:flex;justify-content:space-between;font-size:11px;"><span style="color:#555;">Комиссия WB</span><span style="color:#fff;">' + fmtM(s.wb_commission_rub) + '</span></div>' +
          '<div style="display:flex;justify-content:space-between;font-size:11px;"><span style="color:#555;">Логистика WB</span><span style="color:#fff;">' + fmtM(s.wb_logistics_rub) + '</span></div>' +
          '<div style="display:flex;justify-content:space-between;font-size:11px;border-top:1px solid #1a1a2e;padding-top:6px;margin-top:2px;"><span style="color:#aaa;">Прибыль/ед</span><span style="color:' + vc + ';font-weight:700;">' + fmtM(s.profit_per_unit_rub) + '</span></div>' +
          '<div style="display:flex;justify-content:space-between;font-size:11px;"><span style="color:#aaa;">ROI</span><span style="color:' + vc + ';font-weight:700;">' + (s.roi_pct||0) + '%</span></div>' +
          '<div style="display:flex;justify-content:space-between;font-size:11px;"><span style="color:#aaa;">Маржа</span><span style="color:' + vc + ';font-weight:700;">' + (s.margin_pct||0) + '%</span></div>' +
        '</div>' +
        '<div style="margin-top:10px;font-size:11px;color:#555;line-height:1.5;">' + (s.comment||'') + '</div>' +
      '</div>';
  });
  html += '</div>';

  // Детали расчёта
  html += '<div style="background:#0f0f13;border-radius:10px;padding:16px;">' +
    '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:10px;">ДЕТАЛИ РАСЧЁТА</div>' +
    (data.calc_details||[]).map(d =>
      '<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #1a1a2e;font-size:12px;">' +
        '<span style="color:#666;">' + d.label + '</span>' +
        '<span style="color:#aaa;">' + d.value + '</span>' +
      '</div>'
    ).join('') +
  '</div>';

  html += '</div>';
  container.innerHTML = html;
}

// ===== АГЕНТ СКЛАДОВ =====
function renderWarehouseMetrics(data) {
  const el = document.getElementById('warehouseMetrics');
  if (!el) return;
  const w = data.warehouse_stats;
  if (!w) return;
  const sc = {fbs:'#38bdf8', fbo:'#a78bfa', mixed:'#4ade80'};
  const dominant = w.fbs_pct > 60 ? 'FBS' : w.fbs_pct < 40 ? 'FBO' : 'Смешанный';
  const domColor = w.fbs_pct > 60 ? '#38bdf8' : w.fbs_pct < 40 ? '#a78bfa' : '#4ade80';
  el.innerHTML =
    '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:8px;">' +
      '<div style="background:#0f0f13;border-radius:8px;padding:12px;text-align:center;">' +
        '<div style="font-size:10px;color:#555;margin-bottom:4px;letter-spacing:1px;">МОДЕЛЬ НИШИ</div>' +
        '<div style="font-size:16px;font-weight:700;color:' + domColor + ';">' + dominant + '</div>' +
        '<div style="font-size:11px;color:#555;margin-top:4px;">FBS ' + w.fbs_pct + '% / FBO ' + w.fbo_pct + '%</div>' +
      '</div>' +
      '<div style="background:#0f0f13;border-radius:8px;padding:12px;text-align:center;">' +
        '<div style="font-size:10px;color:#555;margin-bottom:4px;letter-spacing:1px;">СКЛАДЫ У ЛИДЕРОВ</div>' +
        '<div style="font-size:16px;font-weight:700;color:#fff;">' + w.avg_wh_leaders + '</div>' +
        '<div style="font-size:11px;color:#555;margin-top:4px;">у остальных: ' + w.avg_wh_others + '</div>' +
      '</div>' +
      '<div style="background:#0f0f13;border-radius:8px;padding:12px;text-align:center;">' +
        '<div style="font-size:10px;color:#555;margin-bottom:4px;letter-spacing:1px;">ОБОРАЧИВАЕМОСТЬ</div>' +
        '<div style="font-size:16px;font-weight:700;color:#fff;">' + w.avg_turnover + ' дн</div>' +
        '<div style="font-size:11px;color:#555;margin-top:4px;">в наличии: ' + w.avg_days_stock + ' дн</div>' +
      '</div>' +
      '<div style="background:#0f0f13;border-radius:8px;padding:12px;text-align:center;">' +
        '<div style="font-size:10px;color:#555;margin-bottom:4px;letter-spacing:1px;">ЗАМОРОЗКА >10%</div>' +
        '<div style="font-size:16px;font-weight:700;color:' + (w.frozen_pct > 30 ? '#ef4444' : w.frozen_pct > 15 ? '#fbbf24' : '#4ade80') + ';">' + w.frozen_pct + '%</div>' +
        '<div style="font-size:11px;color:#555;margin-top:4px;">товаров с проблемой</div>' +
      '</div>' +
    '</div>';
}

async function runWarehouseAnalysis() {
  const btn = document.getElementById('warehouseBtn');
  const container = document.getElementById('warehouseStrategy');
  if (!window.currentNiche) return;
  btn.disabled = true;
  btn.textContent = '⏳ Анализируем...';
  btn.style.opacity = '0.6';
  var wb = document.getElementById('warehouseBlock'); if (wb) { wb.style.display = 'block'; wb.scrollIntoView({behavior:'smooth'}); }  container.innerHTML = '<div style="background:#0f0f13;border-radius:12px;padding:20px;text-align:center;color:#555;"><div style="font-size:24px;margin-bottom:8px;">🤖</div><div style="font-size:13px;">Claude анализирует стратегию поставок...</div></div>';
  try {
    const d = window.currentNiche;
    const w = window._warehouseStats;
    if (!w) throw new Error('Сначала откройте нишу для загрузки данных');
    const payload = {
      niche_name: d.name,
      avg_price: d.avg_price || 0,
      revenue: d.revenue || 0,
      turnover: d.turnover || 0,
      buyout_pct: d.buyout_pct || 0,
      profit_pct: d.profit_pct || 0,
      commission: d.commission || 0,
      warehouse_stats: w,
      currency: currentCurrency,
      rate: rates[currentCurrency],
      symbol: symbols[currentCurrency]
    };
    const resp = await fetch('/warehouse-analysis', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    if (!resp.ok) throw new Error('Ошибка сервера: ' + resp.status);
    const result = await resp.json();
    if (result.error) throw new Error(result.error);
    renderWarehouseStrategy(result.analysis, symbols[currentCurrency], rates[currentCurrency]);
    btn.textContent = '🔄 Обновить анализ';
    btn.style.opacity = '1';
    btn.disabled = false;
  } catch(e) {
    container.innerHTML = '<div style="color:#ef4444;padding:12px;background:#1a0a0a;border-radius:8px;font-size:13px;">❌ ' + e.message + '</div>';
    btn.textContent = '📦 Анализ поставок';
    btn.style.opacity = '1';
    btn.disabled = false;
  }
}

function renderWarehouseStrategy(r, sym, rate) {
  const container = document.getElementById('warehouseStrategy');
  if (!r) return;
  const fmtMoney = (rub) => !rub ? '—' : Math.round(rub * rate).toLocaleString('ru-RU') + ' ' + sym;
  const sc = {fbs:'#38bdf8', fbo:'#a78bfa', mixed:'#4ade80', low:'#4ade80', medium:'#fbbf24', high:'#ef4444'};

  container.innerHTML =
    '<div style="border-top:1px solid #1a1a2e;padding-top:20px;">' +

    '<div style="display:flex;align-items:center;gap:10px;margin-bottom:20px;">' +
      '<div style="width:36px;height:36px;background:linear-gradient(135deg,#0ea5e9,#38bdf8);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px;">🏭</div>' +
      '<div><div style="font-size:15px;font-weight:700;color:#fff;">Стратегия поставок</div><div style="font-size:11px;color:#555;">AI-анализ топ-30 SKU ниши</div></div>' +
    '</div>' +

    '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;border-left:3px solid ' + (sc[r.model_color]||'#38bdf8') + ';">' +
      '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:6px;">РЕКОМЕНДУЕМАЯ МОДЕЛЬ</div>' +
      '<div style="font-size:16px;font-weight:700;color:' + (sc[r.model_color]||'#38bdf8') + ';margin-bottom:8px;">' + (r.model||'') + '</div>' +
      '<div style="font-size:13px;color:#ccc;line-height:1.6;">' + (r.model_detail||'') + '</div>' +
    '</div>' +

    '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;border-left:3px solid #6c63ff;">' +
      '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:10px;">РЕКОМЕНДАЦИИ ПО СКЛАДАМ</div>' +
      (r.warehouse_tips||[]).map((tip,i) =>
        '<div style="display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;">' +
          '<div style="min-width:22px;height:22px;background:#6c63ff22;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;color:#a78bfa;font-weight:700;">' + (i+1) + '</div>' +
          '<div style="font-size:12px;color:#aaa;line-height:1.5;">' + tip + '</div>' +
        '</div>'
      ).join('') +
    '</div>' +

    '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;border-left:3px solid #38bdf8;">' +
      '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:10px;">ОБЪЁМ ПЕРВОЙ ПОСТАВКИ</div>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;">' +
        '<div style="text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">МИНИМУМ</div><div style="font-size:15px;font-weight:700;color:#38bdf8;">' + (r.stock?.min_units||'—') + ' шт</div><div style="font-size:11px;color:#555;">' + fmtMoney(r.stock?.min_rub) + '</div></div>' +
        '<div style="text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">ОПТИМУМ</div><div style="font-size:15px;font-weight:700;color:#4ade80;">' + (r.stock?.opt_units||'—') + ' шт</div><div style="font-size:11px;color:#555;">' + fmtMoney(r.stock?.opt_rub) + '</div></div>' +
        '<div style="text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">ЗАПАС НА</div><div style="font-size:15px;font-weight:700;color:#fff;">' + (r.stock?.days_covered||'—') + ' дней</div><div style="font-size:11px;color:#555;">без дефицита</div></div>' +
      '</div>' +
      '<div style="margin-top:12px;font-size:12px;color:#666;line-height:1.6;">' + (r.stock?.comment||'') + '</div>' +
    '</div>' +

    '<div style="background:#0f0f13;border-radius:10px;padding:16px;border-left:3px solid #4ade80;">' +
      '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:8px;">РИСКИ И ПРЕДУПРЕЖДЕНИЯ</div>' +
      (r.risks||[]).map(risk =>
        '<div style="display:flex;gap:8px;margin-bottom:8px;align-items:flex-start;">' +
          '<div style="color:#fbbf24;font-size:14px;">⚠</div>' +
          '<div style="font-size:12px;color:#aaa;line-height:1.5;">' + risk + '</div>' +
        '</div>'
      ).join('') +
    '</div>' +

    '</div>';
}

// ===== АГЕНТ РЕКЛАМЫ =====
async function runAdAnalysis() {
  const btn = document.getElementById('adStrategyBtn');
  const container = document.getElementById('adStrategyContent');
  if (!window.currentNiche) return;
  btn.disabled = true;
  btn.textContent = '⏳ Анализируем...';
  btn.style.opacity = '0.6';
  var ab = document.getElementById('adBlock'); if (ab) { ab.style.display = 'block'; ab.scrollIntoView({behavior:'smooth'}); }  container.innerHTML = '<div style="background:#0f0f13;border-radius:12px;padding:20px;text-align:center;color:#555;"><div style="font-size:24px;margin-bottom:8px;">🤖</div><div style="font-size:13px;">Claude анализирует рекламную нишу...</div><div style="font-size:11px;margin-top:6px;color:#444;">Обычно занимает 15-20 секунд</div></div>';
  try {
    const d = window.currentNiche;
    const cd = window._chartData || {};
    const rate = rates[currentCurrency];
    const sym = symbols[currentCurrency];
    const payload = {
      niche_name: d.name, display_name: d.display_name || d.name,
      avg_cpm: cd.avg_cpm || 0, ad_pct: cd.ad_pct || 0,
      cpm_status: cd.cpm_status || 'yellow', ad_verdict: cd.ad_verdict || '',
      top_ad_sellers: cd.top_ad_sellers || [],
      avg_price: d.avg_price || 0, revenue: d.revenue || 0,
      sellers_with_sales: d.sellers_with_sales || 0, profit_pct: d.profit_pct || 0,
      buyout_pct: d.buyout_pct || 0, turnover: d.turnover || 0,
      commission: d.commission || 0, currency: currentCurrency, rate: rate, symbol: sym
    };
    const resp = await fetch('/ad-analysis', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    if (!resp.ok) throw new Error('Ошибка сервера: ' + resp.status);
    const result = await resp.json();
    if (result.error) throw new Error(result.error);
    window._adForecastId = result.forecast_id;
    renderAdStrategy(result, sym, rate);
    btn.textContent = '🔄 Обновить анализ';
    btn.style.opacity = '1';
    btn.disabled = false;
  } catch(e) {
    container.innerHTML = '<div style="color:#ef4444;padding:12px;background:#1a0a0a;border-radius:8px;font-size:13px;">❌ Ошибка: ' + e.message + '</div>';
    btn.textContent = '🎯 Получить стратегию';
    btn.style.opacity = '1';
    btn.disabled = false;
  }
}

function renderAdStrategy(data, sym, rate) {
  const container = document.getElementById('adStrategyContent');
  const r = data.analysis;
  if (!r) return;
  const sc = {low:'#4ade80', medium:'#fbbf24', high:'#ef4444'};
  const sl = {low:'🟢 Низкая', medium:'🟡 Умеренная', high:'🔴 Высокая'};
  const budget = r.budget || {};
  const forecast = r.forecast || {};
  const m1 = forecast.month1 || {};
  const m2 = forecast.month2 || {};
  const fmtMoney = (rub) => {
    if (!rub) return '—';
    return Math.round(rub * rate).toLocaleString('ru-RU') + ' ' + sym;
  };
  container.innerHTML = '<div style="border-top:1px solid #1a1a2e;padding-top:20px;margin-top:8px;">' +
    '<div style="display:flex;align-items:center;gap:10px;margin-bottom:20px;">' +
      '<div style="width:36px;height:36px;background:linear-gradient(135deg,#6c63ff,#8b5cf6);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px;">🎯</div>' +
      '<div><div style="font-size:15px;font-weight:700;color:#fff;">Рекламная стратегия</div><div style="font-size:11px;color:#555;">AI-анализ на основе данных ниши</div></div>' +
    '</div>' +
    '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;border-left:3px solid ' + (sc[r.load_level]||'#fbbf24') + ';">' +
      '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:6px;">РЕКЛАМНАЯ НАГРУЗКА</div>' +
      '<div style="font-size:16px;font-weight:700;color:' + (sc[r.load_level]||'#fbbf24') + ';margin-bottom:8px;">' + (sl[r.load_level]||'🟡 Умеренная') + '</div>' +
      '<div style="font-size:13px;color:#ccc;line-height:1.6;">' + (r.load_analysis||'') + '</div>' +
    '</div>' +
    '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;border-left:3px solid #6c63ff;">' +
      '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:6px;">СТРАТЕГИЯ ВХОДА</div>' +
      '<div style="font-size:14px;font-weight:700;color:#a78bfa;margin-bottom:8px;">' + (r.strategy_type||'') + '</div>' +
      '<div style="font-size:13px;color:#ccc;line-height:1.6;">' + (r.strategy_detail||'') + '</div>' +
      (r.strategy_steps ? '<div style="margin-top:12px;">' + r.strategy_steps.map((s,i) => '<div style="display:flex;gap:10px;margin-bottom:8px;align-items:flex-start;"><div style="min-width:22px;height:22px;background:#6c63ff22;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;color:#a78bfa;font-weight:700;">' + (i+1) + '</div><div style="font-size:12px;color:#aaa;line-height:1.5;">' + s + '</div></div>').join('') + '</div>' : '') +
    '</div>' +
    '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;border-left:3px solid #38bdf8;">' +
      '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:10px;">РЕКОМЕНДУЕМЫЙ БЮДЖЕТ</div>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;">' +
        '<div style="text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">СТАРТ (1-й мес)</div><div style="font-size:16px;font-weight:700;color:#38bdf8;">' + fmtMoney(budget.start_rub) + '</div></div>' +
        '<div style="text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">РОСТ (2-й мес)</div><div style="font-size:16px;font-weight:700;color:#38bdf8;">' + fmtMoney(budget.growth_rub) + '</div></div>' +
        '<div style="text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">УДЕРЖАНИЕ</div><div style="font-size:16px;font-weight:700;color:#38bdf8;">' + fmtMoney(budget.sustain_rub) + '</div></div>' +
      '</div>' +
      '<div style="margin-top:12px;font-size:12px;color:#666;line-height:1.6;">' + (budget.comment||'') + '</div>' +
    '</div>' +
    '<div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:16px;border-left:3px solid #4ade80;">' +
      '<div style="font-size:10px;color:#555;letter-spacing:1px;margin-bottom:10px;">ПРОГНОЗ РЕЗУЛЬТАТОВ</div>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px;">' +
        '<div style="background:#0a0a0f;border-radius:8px;padding:10px;text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">CPM СТАРТ (новичок)</div><div style="font-size:15px;font-weight:700;color:#fbbf24;">' + fmtMoney(r.cpm_forecast && r.cpm_forecast.start_rub) + '</div></div>' +
        '<div style="background:#0a0a0f;border-radius:8px;padding:10px;text-align:center;"><div style="font-size:10px;color:#555;margin-bottom:4px;">CPM ЧЕРЕЗ 2 МЕС</div><div style="font-size:15px;font-weight:700;color:#4ade80;">' + fmtMoney(r.cpm_forecast && r.cpm_forecast.month2_rub) + '</div></div>' +
      '</div>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">' +
        '<div style="background:#0a0a0f;border-radius:8px;padding:12px;"><div style="font-size:11px;color:#a78bfa;font-weight:600;margin-bottom:8px;">📅 Месяц 1 — ожидать:</div>' + (m1.metrics||[]).map(m => '<div style="font-size:11px;color:#aaa;margin-bottom:4px;">• ' + m + '</div>').join('') + '</div>' +
        '<div style="background:#0a0a0f;border-radius:8px;padding:12px;"><div style="font-size:11px;color:#4ade80;font-weight:600;margin-bottom:8px;">📅 Месяц 2 — ожидать:</div>' + (m2.metrics||[]).map(m => '<div style="font-size:11px;color:#aaa;margin-bottom:4px;">• ' + m + '</div>').join('') + '</div>' +
      '</div>' +
      '<div style="margin-top:12px;font-size:12px;color:#666;line-height:1.6;">' + ((r.cpm_forecast && r.cpm_forecast.comment)||'') + '</div>' +
    '</div>' +
    (window._adForecastId ? '<div style="border-top:1px solid #1a1a2e;padding-top:16px;"><div style="font-size:12px;color:#555;margin-bottom:10px;">📊 Введите реальные показатели через месяц для получения диагностики:</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;"><button onclick="showMonitorForm(1)" style="background:#1a1a2e;color:#a78bfa;border:1px solid #6c63ff44;border-radius:8px;padding:10px;font-size:12px;cursor:pointer;">📊 Факт — месяц 1</button><button onclick="showMonitorForm(2)" style="background:#1a1a2e;color:#4ade80;border:1px solid #22c55e44;border-radius:8px;padding:10px;font-size:12px;cursor:pointer;">📊 Факт — месяц 2</button></div></div>' : '') +
  '</div>';
}

function showMonitorForm(month) {
  const container = document.getElementById('adMonitorContent');
  const fields = [
    ['m'+month+'_cpm','Фактический CPM (₽)','например 250'],
    ['m'+month+'_ctr','CTR %','например 1.5'],
    ['m'+month+'_spend','Расход на рекламу (₽)','например 15000'],
    ['m'+month+'_orders','Заказов с рекламы','например 45'],
    ['m'+month+'_pos','Позиция в поиске (средняя)','например 15'],
    ['m'+month+'_revenue','Выручка за месяц (₽)','например 120000']
  ];
  container.innerHTML = '<div style="background:#0f0f13;border-radius:10px;padding:16px;border:1px solid #6c63ff44;">' +
    '<div style="font-size:13px;font-weight:600;color:#fff;margin-bottom:14px;">📊 Реальные показатели — Месяц ' + month + '</div>' +
    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px;">' +
    fields.map(f => '<div><div style="font-size:11px;color:#555;margin-bottom:4px;">' + f[1] + '</div><input id="' + f[0] + '" type="number" step="0.01" placeholder="' + f[2] + '" style="width:100%;background:#0a0a0f;border:1px solid #1a1a2e;border-radius:6px;padding:8px;color:#fff;font-size:13px;box-sizing:border-box;"></div>').join('') +
    '</div>' +
    '<div style="display:flex;gap:8px;">' +
      '<button onclick="submitMonitor(' + month + ')" style="background:linear-gradient(135deg,#6c63ff,#8b5cf6);color:#fff;border:none;border-radius:8px;padding:10px 20px;font-size:12px;font-weight:600;cursor:pointer;flex:1;">🔍 Получить диагностику</button>' +
      '<button onclick="clearMonitor()" style="background:#1a1a2e;color:#555;border:none;border-radius:8px;padding:10px 16px;font-size:12px;cursor:pointer;">✕</button>' +
    '</div></div>';
}

async function submitMonitor(month) {
  const forecastId = window._adForecastId;
  if (!forecastId) return;
  const p = 'm' + month + '_';
  const actual = {
    cpm: parseFloat(document.getElementById(p+'cpm')?.value)||0,
    ctr: parseFloat(document.getElementById(p+'ctr')?.value)||0,
    spend: parseFloat(document.getElementById(p+'spend')?.value)||0,
    orders: parseFloat(document.getElementById(p+'orders')?.value)||0,
    position: parseFloat(document.getElementById(p+'pos')?.value)||0,
    revenue: parseFloat(document.getElementById(p+'revenue')?.value)||0
  };
  const container = document.getElementById('adMonitorContent');
  container.innerHTML = '<div style="padding:16px;text-align:center;color:#555;font-size:13px;">⏳ Claude анализирует ваши показатели...</div>';
  try {
    const resp = await fetch('/ad-monitor', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({forecast_id:forecastId, month:month, actual:actual})});
    const result = await resp.json();
    if (result.error) throw new Error(result.error);
    const diag = result.diagnosis;
    const sc = {good:'#4ade80', warning:'#fbbf24', bad:'#ef4444'};
    const se = {good:'✅', warning:'⚠️', bad:'🚨'};
    container.innerHTML = '<div style="background:#0f0f13;border-radius:10px;padding:16px;border:1px solid ' + (sc[diag.status]||'#fbbf24') + '44;">' +
      '<div style="font-size:13px;font-weight:700;color:' + (sc[diag.status]||'#fbbf24') + ';margin-bottom:10px;">' + (se[diag.status]||'⚠️') + ' Диагностика месяц ' + month + '</div>' +
      '<div style="margin-bottom:14px;">' +
      (diag.comparison||[]).map(c => '<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid #1a1a2e;"><div style="font-size:12px;color:#aaa;">' + c.metric + '</div><div style="display:flex;gap:16px;font-size:12px;"><span style="color:#555;">План: ' + c.plan + '</span><span style="color:' + (sc[c.status]||'#aaa') + ';">Факт: ' + c.actual + '</span></div></div>').join('') +
      '</div>' +
      '<div style="font-size:13px;color:#ccc;line-height:1.6;margin-bottom:12px;">' + (diag.summary||'') + '</div>' +
      ((diag.recommendations||[]).length > 0 ? '<div style="margin-top:10px;"><div style="font-size:11px;color:#555;letter-spacing:1px;margin-bottom:8px;">РЕКОМЕНДАЦИИ:</div>' + diag.recommendations.map(rec => '<div style="display:flex;gap:8px;margin-bottom:6px;align-items:flex-start;"><div style="color:#6c63ff;font-size:14px;">→</div><div style="font-size:12px;color:#aaa;line-height:1.5;">' + rec + '</div></div>').join('') + '</div>' : '') +
    '</div>';
  } catch(e) {
    container.innerHTML = '<div style="color:#ef4444;padding:12px;font-size:13px;">❌ ' + e.message + '</div>';
  }
}
</script>
</div>
</div>
</div>
  <div id="sticky-agents" style="display:none;position:fixed;bottom:0;left:220px;right:0;background:#0d0d14;border-top:1px solid #1a1a2e;padding:8px 24px;z-index:1000;box-shadow:0 -4px 20px rgba(0,0,0,0.5);">
    <div style="display:flex;align-items:center;gap:8px;">
      <span style="font-size:10px;color:#333;margin-right:4px;white-space:nowrap;">AI:</span>
      <button onclick="deepAnalysis(window.currentNiche);setTimeout(function(){var el=document.getElementById('deep-analysis-block');if(el){el.style.display='block';el.scrollIntoView({behavior:'smooth'});}},500)" style="background:#0f1a0f;border:1px solid #22c55e44;border-radius:7px;padding:6px 12px;cursor:pointer;color:#22c55e;font-size:11px;white-space:nowrap;">&#128269; Глубокий анализ</button>
      <button onclick="showUnitEconomy();setTimeout(function(){var el=document.getElementById('unit-economy-block');if(el){el.style.display='block';el.scrollIntoView({behavior:'smooth'});}},300)" style="background:#1a150a;border:1px solid #f59e0b44;border-radius:7px;padding:6px 12px;cursor:pointer;color:#f59e0b;font-size:11px;white-space:nowrap;">&#129518; Юнит-экономика</button>
      <button onclick="runAdAnalysis();setTimeout(function(){var el=document.getElementById('adBlock');if(el)el.scrollIntoView({behavior:'smooth'});},500)" style="background:#0f0f1a;border:1px solid #6c63ff44;border-radius:7px;padding:6px 12px;cursor:pointer;color:#a78bfa;font-size:11px;white-space:nowrap;">&#127919; Реклама</button>
      <button onclick="runWarehouseAnalysis();setTimeout(function(){var el=document.getElementById('warehouseBlock');if(el)el.scrollIntoView({behavior:'smooth'});},500)" style="background:#0a1520;border:1px solid #38bdf844;border-radius:7px;padding:6px 12px;cursor:pointer;color:#38bdf8;font-size:11px;white-space:nowrap;">&#128230; Поставки</button>
      <button onclick="runDocsAnalysis()" style="background:#1a120a;border:1px solid #d9770644;border-radius:7px;padding:6px 12px;cursor:pointer;color:#d97706;font-size:11px;white-space:nowrap;">&#128203; Документы</button>
      <button onclick="runSupplierAnalysis()" style="background:#0a1a0f;border:1px solid #34d39944;border-radius:7px;padding:6px 12px;cursor:pointer;color:#34d399;font-size:11px;white-space:nowrap;">&#127981; Поставщики</button>
      <div style="flex:1;"></div>
      <button id="sticky-wl-btn" onclick="toggleStickyWL(this)" style="background:#1a1a24;border:1px solid #2a2a3a;border-radius:7px;padding:6px 14px;cursor:pointer;color:#888;font-size:11px;white-space:nowrap;">&#128278; В работе</button>
    </div>
  </div>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML.encode('utf-8'))

        elif self.path.startswith('/top-niches'):
            try:
                import random, datetime
                from urllib.parse import parse_qs, urlparse
                offset = int(parse_qs(urlparse(self.path).query).get('offset', ['0'])[0])
                conn = psycopg2.connect(DB)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT name, revenue, sellers, sellers_with_sales,
                           buyout_pct, turnover, profit_pct, lost_revenue_pct,
                           products, products_with_sales, avg_rating,
                           COALESCE(display_name, name) as display_name,
                           mpstats_path
                    FROM niches
                    WHERE revenue IS NOT NULL
                    AND profit_pct > 0.1
                    AND buyout_pct > 0.5
                    AND sellers > 10
                    AND (mpstats_path IS NULL OR path_verified = TRUE OR EXISTS (
                        SELECT 1 FROM unnest(string_to_array(LOWER(name), ' ')) AS nw
                        JOIN unnest(string_to_array(LOWER(REPLACE(mpstats_path, '/', ' ')), ' ')) AS pw ON LEFT(nw, 5) = LEFT(pw, 5)
                        WHERE length(nw) > 3
                    ))
                    ORDER BY revenue DESC
                    LIMIT 500
                """)
                rows = cursor.fetchall()
                cursor.close()
                conn.close()
                results = []
                for r in rows:
                    name, revenue, sellers, sellers_with_sales, buyout_pct, \
                    turnover, profit_pct, lost_revenue_pct, products, \
                    products_with_sales, avg_rating, display_name, mpstats_path = r
                    score = calculate_score((
                        name, products, products_with_sales,
                        sellers, sellers_with_sales, revenue,
                        None, None, lost_revenue_pct, None,
                        buyout_pct, turnover, profit_pct,
                        avg_rating, None, None, None
                    ))
                    cat = mpstats_path.split('/')[0] if mpstats_path else 'Другое'
                    results.append({
                        'name': display_name,
                        'full': name,
                        'revenue': float(revenue or 0),
                        'revenue_annual': float(revenue or 0) / 2,
                        'score': score,
                        'category': cat,
                    })
                results.sort(key=lambda x: x['score'], reverse=True)
                today = datetime.date.today().toordinal()
                rng = random.Random(today + 1 + offset)
                by_cat = {}
                for n in results:
                    cat = n['category']
                    if cat not in by_cat:
                        by_cat[cat] = []
                    by_cat[cat].append(n)
                final = []
                cats = list(by_cat.keys())
                rng.shuffle(cats)
                for cat in cats:
                    final.append(rng.choice(by_cat[cat]))
                    if len(final) >= 21:
                        break
                self.send_response(200)
                self.send_header('Content-type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(final, ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())


        elif self.path == '/top-chips':
            import random, datetime
            conn = psycopg2.connect(DB)
            cur = conn.cursor()
            # Топ ниши по score: прибыльность + выкуп + оборачиваемость
            cur.execute('''
                SELECT name, mpstats_path,
                    (COALESCE(profit_pct,0) * 0.4 + 
                     COALESCE(buyout_pct,0) * 0.3 + 
                     CASE WHEN COALESCE(turnover,999) < 60 THEN 0.3
                          WHEN COALESCE(turnover,999) < 90 THEN 0.2
                          WHEN COALESCE(turnover,999) < 180 THEN 0.1
                          ELSE 0 END) as score
                FROM niches 
                WHERE revenue IS NOT NULL 
                AND profit_pct > 0.1 
                AND buyout_pct > 0.5
                AND sellers > 10
                AND LENGTH(REGEXP_REPLACE(name, '^[0-9]+ ', '')) > 5
                AND mpstats_path IS NOT NULL
                ORDER BY score DESC
                LIMIT 50
            ''')
            rows = cur.fetchall()
            conn.close()
            # Группируем по категории — берём по 1 из каждой
            by_cat = {}
            for r in rows:
                cat = r[1].split('/')[0] if r[1] else 'Другое'
                if cat not in by_cat:
                    by_cat[cat] = []
                by_cat[cat].append(r[0])
            # Ежедневная ротация — seed = дата
            today = datetime.date.today().toordinal()
            rng = random.Random(today)
            result_names = []
            cats = list(by_cat.keys())
            rng.shuffle(cats)
            for cat in cats:
                names = by_cat[cat]
                result_names.append(rng.choice(names))
                if len(result_names) >= 9:
                    break
            result = [{'name': n} for n in result_names]
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))

        elif self.path == '/catalog':
            try:
                conn = psycopg2.connect(DB)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT name, revenue, orders, sellers, sellers_with_sales,
                           buyout_pct, profit_pct, turnover,
                           COALESCE(display_name, name) as display_name,
                           mpstats_path
                    FROM niches
                    WHERE revenue IS NOT NULL
                    ORDER BY revenue DESC
                """)
                rows = cursor.fetchall()
                cursor.close()
                conn.close()
                niches = []
                for r in rows:
                    niches.append({
                        'name': r[8],
                        'category': r[9].split('/')[0] if r[9] else 'Другое',
                        'full': r[0],
                        'revenue': float(r[1] or 0),
                        'orders': int(r[2] or 0),
                        'sellers': int(r[3] or 0),
                        'sellers_with_sales': int(r[4] or 0),
                        'buyout_pct': float(r[5] or 0),
                        'profit_pct': float(r[6] or 0),
                        'turnover': float(r[7] or 0),
                    })
                self.send_response(200)
                self.send_header('Content-type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(niches, ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())



        elif self.path.startswith('/portfolio'):
            try:
                from urllib.parse import parse_qs, urlparse
                offset = int(parse_qs(urlparse(self.path).query).get('offset', ['0'])[0])
                import random, datetime
                conn = psycopg2.connect(DB)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT name, revenue, orders, sellers, sellers_with_sales,
                           buyout_pct, profit_pct, turnover, lost_revenue_pct,
                           COALESCE(display_name, name) as display_name,
                           avg_price, commission
                    FROM niches
                    WHERE buyout_pct > 0.70
                    AND turnover BETWEEN 5 AND 45
                    AND profit_pct > 0.30
                    AND revenue > 5000000
                    AND sellers_with_sales > 10
                    AND (mpstats_path IS NULL OR path_verified = TRUE OR EXISTS (
                        SELECT 1 FROM unnest(string_to_array(LOWER(name), ' ')) AS nw
                        JOIN unnest(string_to_array(LOWER(REPLACE(mpstats_path, '/', ' ')), ' ')) AS pw ON LEFT(nw, 5) = LEFT(pw, 5)
                        WHERE length(nw) > 3
                    ))
                    ORDER BY (buyout_pct * 0.35 + profit_pct * 0.35 + (45.0 - LEAST(turnover,45))/45.0 * 0.30) DESC
                    LIMIT 50
                """)
                rows = cursor.fetchall()
                cursor.close()
                conn.close()
                results = []
                for r in rows:
                    name, revenue, orders, sellers, sellers_with_sales,                     buyout_pct, profit_pct, turnover, lost_revenue_pct,                     display_name, avg_price, commission = r
                    score = calculate_score((
                        name, None, None, sellers, sellers_with_sales,
                        revenue, None, None, lost_revenue_pct, orders,
                        buyout_pct, turnover, profit_pct, None, None, commission, avg_price
                    ))
                    results.append({
                        'name': display_name,
                        'full': name,
                        'revenue': float(revenue or 0),
                        'revenue_annual': float(revenue or 0) / 2,
                        'orders': int(orders or 0),
                        'buyout_pct': float(buyout_pct or 0),
                        'profit_pct': float(profit_pct or 0),
                        'turnover': float(turnover or 0),
                        'score': score,
                        'avg_price': float(avg_price or 0),
                    })
                results.sort(key=lambda x: x['score'], reverse=True)
                results = results[offset:offset+15]
                self.send_response(200)
                self.send_header('Content-type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(results, ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())

        elif self.path.startswith('/deep-analysis'):
            from urllib.parse import parse_qs, urlparse, unquote
            try:
                params = parse_qs(urlparse(self.path).query)
                name = unquote(params.get('name', [''])[0])
                revenue = float(params.get('revenue', [0])[0])
                avg_price = float(params.get('avg_price', [0])[0])
                commission = float(params.get('commission', [0])[0])
                buyout_pct = float(params.get('buyout_pct', [0])[0])
                profit_pct = float(params.get('profit_pct', [0])[0])
                turnover = float(params.get('turnover', [0])[0])
                sellers = int(params.get('sellers', [0])[0])
                sellers_with_sales = int(params.get('sellers_with_sales', [0])[0])
                currency = params.get('currency', ['rub'])[0]
                rates = {'rub': 1, 'usd': 0.011, 'eur': 0.010, 'byn': 0.036}
                symbols = {'rub': '₽', 'usd': '$', 'eur': '€', 'byn': 'Br'}
                rate = rates.get(currency, 1)
                sym = symbols.get(currency, '₽')

                # Считаем базовые показатели для контекста
                real_turnover = round(turnover / buyout_pct) if buyout_pct > 0 else round(turnover)
                avg_rev_per_seller = revenue / sellers_with_sales if sellers_with_sales > 0 else 0

                # Claude AI глубокий анализ
                import anthropic as anthr
                client = anthr.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
                prompt = f"""Ты эксперт по торговле на Wildberries. Сделай глубокий анализ ниши для селлера.

Ниша: {name}
Валюта отображения: {sym}
Курс к рублю: {rate}

ДАННЫЕ НИШИ:
- Выручка за период (~2 года): {revenue:,.0f} ₽ (~{revenue/2:,.0f} ₽/год)
- Заказов: {int(revenue/avg_price) if avg_price > 0 else 0}/мес
- Продавцов всего: {sellers}
- Продавцов с продажами: {sellers_with_sales} ({round(sellers_with_sales/sellers*100) if sellers > 0 else 0}%)
- Средняя цена: {avg_price:,.0f} ₽
- Комиссия WB: {commission:.0f}%
- Выкуп: {buyout_pct*100:.0f}%
- Оборачиваемость реальная: {real_turnover} дней
- Маржинальность: {profit_pct*100:.0f}%
- Средняя выручка продавца: {avg_rev_per_seller:,.0f} ₽/мес

Дай анализ в формате JSON (все суммы в {sym}, умножай рубли на {rate}):
{{
  "verdict": "ВХОДИТЬ" или "ТЕСТИРОВАТЬ" или "НЕ ВХОДИТЬ",
  "verdict_color": "#22c55e" или "#eab308" или "#ef4444",
  "verdict_desc": "краткое обоснование вердикта 1 предложение",
  "entry_budget": число (минимальный бюджет на вход = партия 30 шт * цена * курс),
  "ad_budget": число (бюджет на рекламу первый месяц в {sym}),
  "breakeven": число (точка безубыточности в {sym}),
  "roi_forecast": "прогноз ROI через 3 месяца например 25-40%",
  "financial_plan": "2-3 предложения о финансовом плане входа",
  "competitive_analysis": "2-3 предложения о конкурентной ситуации и слабых местах топ игроков",
  "free_segments": "какие ценовые или продуктовые сегменты свободны",
  "recommendation": "конкретная рекомендация что делать 2-3 предложения"
}}
Только JSON без markdown."""

                msg = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=1000,
                    messages=[{"role": "user", "content": prompt}]
                )
                ai = json.loads(msg.content[0].text.strip().replace("```json","").replace("```","").strip())

                html = f'''
                <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:24px;">
                  <div style="background:#0f0f13;border-radius:10px;padding:16px;text-align:center;">
                    <div style="font-size:11px;color:#555;margin-bottom:8px;">ВЕРДИКТ</div>
                    <div style="font-size:22px;font-weight:700;color:{ai["verdict_color"]};">{ai["verdict"]}</div>
                    <div style="font-size:11px;color:#555;margin-top:6px;">{ai["verdict_desc"]}</div>
                  </div>
                  <div style="background:#0f0f13;border-radius:10px;padding:16px;text-align:center;">
                    <div style="font-size:11px;color:#555;margin-bottom:8px;">БЮДЖЕТ НА ВХОД</div>
                    <div style="font-size:22px;font-weight:700;color:#fff;">{ai["entry_budget"]:,.0f} {sym}</div>
                    <div style="font-size:11px;color:#555;margin-top:6px;">минимальная партия 30 шт</div>
                  </div>
                  <div style="background:#0f0f13;border-radius:10px;padding:16px;text-align:center;">
                    <div style="font-size:11px;color:#555;margin-bottom:8px;">БЮДЖЕТ НА РЕКЛАМУ</div>
                    <div style="font-size:22px;font-weight:700;color:#fff;">{ai["ad_budget"]:,.0f} {sym}</div>
                    <div style="font-size:11px;color:#555;margin-top:6px;">первый месяц</div>
                  </div>
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;">
                  <div style="background:#0f0f13;border-radius:10px;padding:16px;">
                    <div style="font-size:11px;color:#555;margin-bottom:4px;">ТОЧКА БЕЗУБЫТОЧНОСТИ</div>
                    <div style="font-size:18px;font-weight:700;color:#fff;">{ai["breakeven"]:,.0f} {sym}</div>
                  </div>
                  <div style="background:#0f0f13;border-radius:10px;padding:16px;">
                    <div style="font-size:11px;color:#555;margin-bottom:4px;">ПРОГНОЗ ROI (3 мес)</div>
                    <div style="font-size:18px;font-weight:700;color:#22c55e;">{ai["roi_forecast"]}</div>
                  </div>
                </div>
                <div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;">
                  <div style="font-size:13px;font-weight:600;color:#fff;margin-bottom:8px;">💰 Финансовый план</div>
                  <div style="font-size:12px;color:#aaa;line-height:1.6;">{ai["financial_plan"]}</div>
                </div>
                <div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;">
                  <div style="font-size:13px;font-weight:600;color:#fff;margin-bottom:8px;">🏆 Конкурентный анализ</div>
                  <div style="font-size:12px;color:#aaa;line-height:1.6;">{ai["competitive_analysis"]}</div>
                </div>
                <div style="background:#0f0f13;border-radius:10px;padding:16px;margin-bottom:12px;">
                  <div style="font-size:13px;font-weight:600;color:#fff;margin-bottom:8px;">🎯 Свободные сегменты</div>
                  <div style="font-size:12px;color:#aaa;line-height:1.6;">{ai["free_segments"]}</div>
                </div>
                <div style="background:#22c55e11;border:1px solid #22c55e33;border-radius:10px;padding:16px;">
                  <div style="font-size:13px;font-weight:600;color:#22c55e;margin-bottom:8px;">✅ Рекомендация</div>
                  <div style="font-size:12px;color:#aaa;line-height:1.6;">{ai["recommendation"]}</div>
                </div>'''

                self.send_response(200)
                self.send_header('Content-type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({'html': html}, ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())

        elif self.path.startswith('/charts'):
            from urllib.parse import unquote
            query = self.path.split('?q=')[-1]
            query = unquote(query)
            try:
                from datetime import date, timedelta
                conn = psycopg2.connect(DB)
                cur = conn.cursor()
                cur.execute("SELECT mpstats_path, name, COALESCE(path_verified, FALSE) FROM niches WHERE name ILIKE %s ORDER BY CASE WHEN LOWER(name)=LOWER(%s) THEN 0 ELSE 1 END LIMIT 1", (f'%{query}%', query))
                row = cur.fetchone()
                conn.close()
                data_warning = False
                if row and row[0]:
                    niche_name = row[1]
                    path_verified = row[2]
                    path_last = row[0].split('/')[-1].lower()
                    name_words = set(niche_name.lower().split())
                    path_words = set(row[0].lower().replace('/', ' ').split())
                    common_words = name_words & path_words
                    # Показываем маркер если путь не верифицирован и нет общих слов
                    if not path_verified and len(common_words) == 0:
                        data_warning = True
                
                if not row or not row[0]:
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(json.dumps({'error': 'no_path'}).encode())
                else:
                    mpstats_path = row[0]
                    headers = {'X-Mpstats-TOKEN': MPSTATS_TOKEN, 'Content-Type': 'application/json'}
                    r = mpstats_req.post(
                        'https://mpstats.io/api/wb/get/category',
                        headers=headers,
                        params={'d1': '2024-04-01', 'd2': '2026-04-14', 'path': mpstats_path},
                        json={'startRow': 0, 'endRow': 100, 'sortModel': [{'colId': 'revenue', 'sort': 'desc'}]},
                        timeout=30
                    )
                    print(f'MPStats status: {r.status_code}, path: {mpstats_path}')
                    data = r.json()
                    print(f'MPStats total: {data.get("total")}, items: {len(data.get("data", []))}')
                    items = data.get('data', [])
                    
                    start = date(2024, 4, 1)
                    months_revenue = {}
                    months_sales = {}
                    
                    for item in items:
                        rg = item.get('revenue_graph', [])
                        sg = item.get('sales_graph', [])
                        for i, val in enumerate(rg):
                            d = start + timedelta(days=i)
                            key = f'{d.year}-{d.month:02d}'
                            months_revenue[key] = months_revenue.get(key, 0) + (val or 0)
                        for i, val in enumerate(sg):
                            d = start + timedelta(days=i)
                            key = f'{d.year}-{d.month:02d}'
                            months_sales[key] = months_sales.get(key, 0) + (val or 0)
                    
                    labels = sorted(months_revenue.keys())
                    month_names = {'01':'Янв','02':'Фев','03':'Мар','04':'Апр','05':'Май',
                                   '06':'Июн','07':'Июл','08':'Авг','09':'Сен','10':'Окт',
                                   '11':'Ноя','12':'Дек'}
                    
                    # Динамические ценовые сегменты на основе средней цены
                    prices_list = [item.get('final_price', 0) or 0 for item in items if item.get('final_price', 0)]
                    avg_p = sum(prices_list) / len(prices_list) if prices_list else 1000
                    # Создаём 6 диапазонов вокруг средней цены
                    p1 = round(avg_p * 0.2, -1) or 100
                    p2 = round(avg_p * 0.5, -1) or 250
                    p3 = round(avg_p * 0.8, -1) or 500
                    p4 = round(avg_p * 1.2, -1) or 800
                    p5 = round(avg_p * 2.0, -1) or 1500
                    def fmt_price(v):
                        return f"{int(v):,}".replace(",", " ")
                    price_segments = {
                        f"до {fmt_price(p1)}": 0,
                        f"{fmt_price(p1)}-{fmt_price(p2)}": 0,
                        f"{fmt_price(p2)}-{fmt_price(p3)}": 0,
                        f"{fmt_price(p3)}-{fmt_price(p4)}": 0,
                        f"{fmt_price(p4)}-{fmt_price(p5)}": 0,
                        f"свыше {fmt_price(p5)}": 0
                    }
                    seg_bounds = [p1, p2, p3, p4, p5]
                    seg_keys = list(price_segments.keys())
                    sellers_revenue = {}
                    for item in items:
                        price = item.get('final_price', 0) or 0
                        idx = len(seg_bounds)
                        for j, bound in enumerate(seg_bounds):
                            if price < bound:
                                idx = j
                                break
                        price_segments[seg_keys[idx]] += 1
                        seller = item.get('seller', 'Неизвестно')
                        rev = item.get('revenue', 0) or 0
                        sellers_revenue[seller] = sellers_revenue.get(seller, 0) + rev

                    top_sellers = sorted(sellers_revenue.items(), key=lambda x: x[1], reverse=True)[:8]

                    # Рекламные данные
                    cpm_list = []
                    ad_count = 0
                    ext_ad_count = 0
                    ad_sellers = {}
                    for item in items:
                        cpm = item.get('search_cpm_avg', 0) or 0
                        if cpm > 0:
                            cpm_list.append(cpm)
                        if item.get('ext_advertising', 0) or (item.get('search_ad_position_avg', 0) or 0) > 0:
                            ad_count += 1
                            seller = item.get('seller', 'Неизвестно')
                            ad_sellers[seller] = ad_sellers.get(seller, 0) + 1
                    avg_cpm = round(sum(cpm_list) / len(cpm_list), 0) if cpm_list else 0
                    ad_pct = round(ad_count / len(items) * 100, 1) if items else 0
                    top_ad_sellers = sorted(ad_sellers.items(), key=lambda x: x[1], reverse=True)[:5]

                    # CPM светофор
                    if avg_cpm < 150:
                        cpm_status = 'green'
                        cpm_label = 'Выгодно'
                    elif avg_cpm < 400:
                        cpm_status = 'yellow'
                        cpm_label = 'Умеренно'
                    else:
                        cpm_status = 'red'
                        cpm_label = 'Дорого'

                    # % рекламируемых светофор
                    if ad_pct < 30:
                        ad_pct_status = 'green'
                    elif ad_pct < 60:
                        ad_pct_status = 'yellow'
                    else:
                        ad_pct_status = 'red'

                    # Итоговый вердикт
                    if cpm_status == 'green' and ad_pct_status == 'green':
                        ad_verdict = 'Выгодно входить'
                        ad_verdict_color = '#4ade80'
                    elif cpm_status == 'red' or ad_pct_status == 'red':
                        ad_verdict = 'Реклама съест маржу'
                        ad_verdict_color = '#ef4444'
                    else:
                        ad_verdict = 'Умеренные затраты'
                        ad_verdict_color = '#fbbf24'

                    # Доли продавцов для круговой диаграммы
                    conn2 = psycopg2.connect(DB)
                    cur2 = conn2.cursor()
                    cur2.execute("SELECT revenue FROM niches WHERE name ILIKE %s LIMIT 1", (query,))
                    niche_row = cur2.fetchone()
                    total_niche_revenue = float(niche_row[0]) if niche_row else 0
                    cur2.close()
                    conn2.close()
                    top8_revenue = sum(s[1] for s in top_sellers)
                    # Общая выручка всех товаров из выборки MPStats
                    total_items_revenue = sum(item.get('revenue', 0) or 0 for item in items)
                    total_for_pct = total_items_revenue if total_items_revenue > 0 else top8_revenue if top8_revenue > 0 else 1
                    others_revenue = max(0, total_for_pct - top8_revenue)
                    seller_pct = []
                    for s in top_sellers:
                        seller_pct.append(round(s[1] / total_for_pct * 100, 1))
                    seller_pct.append(round(others_revenue / total_for_pct * 100, 1))

                    # Умный прогноз на 3 месяца вперёд
                    rev_list = [round(months_revenue.get(k, 0) / 1000000, 1) for k in sorted(months_revenue.keys())]
                    forecast_labels = []
                    forecast_data = []
                    if len(rev_list) >= 12:
                        # Коэффициент тренда: последние 3 мес vs те же 3 мес год назад
                        last3 = sum(rev_list[-3:]) / 3
                        year_ago3 = sum(rev_list[-15:-12]) / 3 if len(rev_list) >= 15 else sum(rev_list[:3]) / 3
                        trend_k = last3 / year_ago3 if year_ago3 > 0 else 1.0
                        trend_k = max(0.5, min(2.0, trend_k))  # ограничиваем от 0.5 до 2.0
                        # Прогноз = тот же месяц год назад * тренд
                        from datetime import date
                        last_month_keys = sorted(months_revenue.keys())
                        last_key = last_month_keys[-1]
                        last_year, last_mon = int(last_key.split('-')[0]), int(last_key.split('-')[1])
                        for i in range(1, 4):
                            next_mon = last_mon + i
                            next_year = last_year
                            if next_mon > 12:
                                next_mon -= 12
                                next_year += 1
                            # Тот же месяц год назад
                            prev_key = f'{next_year-1}-{next_mon:02d}'
                            prev_val = months_revenue.get(prev_key, 0) / 1000000
                            forecast_val = round(prev_val * trend_k, 1)
                            mn = {'01':'Янв','02':'Фев','03':'Мар','04':'Апр','05':'Май','06':'Июн','07':'Июл','08':'Авг','09':'Сен','10':'Окт','11':'Ноя','12':'Дек'}
                            forecast_labels.append(mn[f'{next_mon:02d}'] + ' ' + str(next_year)[2:] + ' ▶')
                            forecast_data.append(forecast_val)

                    # Топ-20 товаров ниши
                    top_items = []
                    items_sorted = sorted(items, key=lambda x: x.get('revenue', 0) or 0, reverse=True)[:20]
                    for item in items_sorted:
                        top_items.append({
                            'id': item.get('id', ''),
                            'name': item.get('name', 'Неизвестно')[:60],
                            'seller': item.get('seller', 'Неизвестно')[:30],
                            'price': item.get('final_price', 0) or 0,
                            'revenue': round((item.get('revenue', 0) or 0) / 1000, 1),
                            'sales': item.get('sales', 0) or 0,
                            'rating': item.get('rating', 0) or 0,
                            'url': f"https://www.wildberries.ru/catalog/{item.get('id', '')}/detail.aspx" if item.get('id') else '',
                        })

                    # Агрегация данных складов по топ-30 SKU
                    items_with_sales = [i for i in items if (i.get('sales') or 0) > 0][:30]
                    if items_with_sales:
                        fbs_count = sum(1 for i in items_with_sales if i.get('is_fbs'))
                        fbo_count = len(items_with_sales) - fbs_count
                        fbs_pct = round(fbs_count / len(items_with_sales) * 100)
                        leaders = items_with_sales[:10]
                        others = items_with_sales[10:]
                        avg_wh_leaders = round(sum(i.get('warehouses_count',0) for i in leaders) / len(leaders), 1)
                        avg_wh_others = round(sum(i.get('warehouses_count',0) for i in others) / len(others), 1) if others else 0
                        avg_turnover = round(sum(i.get('turnover_days',0) for i in items_with_sales) / len(items_with_sales), 1)
                        avg_days_stock = round(sum(i.get('days_in_stock',0) for i in items_with_sales) / len(items_with_sales), 1)
                        frozen_count = sum(1 for i in items_with_sales if (i.get('frozen_stocks_percent',0) or 0) > 10)
                        frozen_pct = round(frozen_count / len(items_with_sales) * 100)
                        avg_balance = round(sum(i.get('balance',0) for i in items_with_sales) / len(items_with_sales))
                        avg_balance_fbs = round(sum(i.get('balance_fbs',0) for i in items_with_sales) / len(items_with_sales))
                        warehouse_stats = {
                            'fbs_count': fbs_count, 'fbo_count': fbo_count,
                            'fbs_pct': fbs_pct, 'fbo_pct': 100 - fbs_pct,
                            'avg_wh_leaders': avg_wh_leaders, 'avg_wh_others': avg_wh_others,
                            'avg_turnover': avg_turnover, 'avg_days_stock': avg_days_stock,
                            'frozen_pct': frozen_pct, 'frozen_count': frozen_count,
                            'avg_balance': avg_balance, 'avg_balance_fbs': avg_balance_fbs,
                            'total_skus': len(items_with_sales)
                        }
                    else:
                        warehouse_stats = None

                    result = {
                        'labels': [month_names[k.split('-')[1]] + ' ' + k.split('-')[0][2:] for k in labels],
                        'revenue': [round(months_revenue.get(k, 0) / 1000000, 1) for k in labels],
                        'sales': [months_sales.get(k, 0) for k in labels],
                        'price_labels': list(price_segments.keys()),
                        'price_data': list(price_segments.values()),
                        'seller_labels': [s[0][:25] for s in top_sellers] + ['Остальные продавцы'],
                        'seller_data': [round(s[1]/1000000, 1) for s in top_sellers] + [round(others_revenue/1000000, 1)],
                        'seller_pct': seller_pct,
                        'forecast_labels': forecast_labels,
                        'forecast_data': forecast_data,
                        'items_count': len(items),
                        'avg_cpm': avg_cpm,
                        'ad_pct': ad_pct,
                        'cpm_status': cpm_status,
                        'cpm_label': cpm_label,
                        'ad_pct_status': ad_pct_status,
                        'ad_verdict': ad_verdict,
                        'ad_verdict_color': ad_verdict_color,
                        'top_ad_sellers': [{'name': s[0][:25], 'count': s[1]} for s in top_ad_sellers],
                        'top_items': top_items,
                        'warehouse_stats': warehouse_stats,
                        'package_data': {
                            'avg_length': round(sum(i.get('package_length',0) or 0 for i in items_with_sales) / len(items_with_sales), 1) if items_with_sales else 0,
                            'avg_width': round(sum(i.get('package_width',0) or 0 for i in items_with_sales) / len(items_with_sales), 1) if items_with_sales else 0,
                            'avg_height': round(sum(i.get('package_height',0) or 0 for i in items_with_sales) / len(items_with_sales), 1) if items_with_sales else 0,
                            'avg_weight': round(sum(i.get('package_weight',0) or 0 for i in items_with_sales) / len(items_with_sales), 2) if items_with_sales else 0,
                        }
                    }
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())

        elif self.path.startswith('/smart-search'):
            from urllib.parse import parse_qs, urlparse, unquote
            import anthropic
            try:
                query = unquote(self.path.split('?q=')[-1])
                
                # Берём все ниши — сначала текстовый поиск по корням слов
                conn = psycopg2.connect(DB)
                cursor = conn.cursor()
                
                # Шаг 1: быстрый поиск по похожим словам (первые 5 букв)
                words = [w[:5] for w in query.lower().split() if len(w) >= 3]
                if words:
                    like_conditions = ' OR '.join([f"LOWER(name) LIKE %s OR LOWER(COALESCE(display_name,name)) LIKE %s" for _ in words])
                    like_params = [p for w in words for p in (f'%{w}%', f'%{w}%')]
                    cursor.execute(f"SELECT name, COALESCE(display_name,name), revenue FROM niches WHERE revenue IS NOT NULL AND ({like_conditions}) ORDER BY revenue DESC LIMIT 50", like_params)
                    candidates = cursor.fetchall()
                else:
                    candidates = []
                
                # Шаг 2: если мало кандидатов — берём все названия для Claude
                cursor.execute("SELECT name, COALESCE(display_name,name), revenue FROM niches WHERE revenue IS NOT NULL ORDER BY revenue DESC")
                all_niches = cursor.fetchall()
                cursor.close()
                conn.close()
                
                # Передаём Claude только названия всех ниш (компактно)
                all_names = [r[1] for r in all_niches]
                niches_text = ', '.join(all_names)
                
                prompt = f"""Пользователь ищет товар для продажи на Wildberries: "{query}"

Список всех ниш Wildberries (через запятую):
{niches_text}

Задача: найти ОДНУ наиболее точную нишу для этого товара.
Правила:
- Ищи по смысловому значению товара, а не по созвучию слов
- спиннинг = рыболовное удилище → ниша "Удилища" (НЕ "Фиджет спиннер")
- Если запрос явно о рыбалке/спорте/конкретном товаре — приоритет практическому смыслу
- Возвращай ТОЛЬКО одну самую релевантную нишу

Верни ТОЛЬКО JSON без markdown:
{{"found": true, "niche_name": "точное название из списка", "explanation": "краткое объяснение 5-8 слов"}}
или если совсем не нашёл:
{{"found": false, "niche_name": null, "explanation": "не найдено"}}"""
                
                client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
                message = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}]
                )
                raw = message.content[0].text.strip().replace('```json','').replace('```','').strip()
                ai_result = json.loads(raw)
                
                if ai_result.get('found') and ai_result.get('niche_name'):
                    matched_name = ai_result['niche_name'].strip().lower()
                    # Мягкий матчинг — ищем по вхождению в all_niches
                    matched = next((r for r in all_niches if 
                        r[1].strip().lower() == matched_name or 
                        r[0].strip().lower() == matched_name or
                        matched_name in r[1].strip().lower() or
                        matched_name in r[0].strip().lower() or
                        r[0].strip().lower() in matched_name
                    ), None)
                    if matched:
                        result = {
                            'niche': matched[0],
                            'niche_display': matched[1],
                            'revenue': float(matched[2]),
                            'explanation': ai_result.get('explanation', '')
                        }
                    else:
                        # Логируем для диагностики
                        print(f"Smart search: Claude вернул '{ai_result['niche_name']}' но не нашли в БД")
                        result = {'niche': None}
                else:
                    result = {'niche': None}
                    
                self.send_response(200)
                self.send_header('Content-type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'niche': None, 'error': str(e)}).encode('utf-8'))

        elif self.path.startswith('/suggest'):
            from urllib.parse import unquote
            query = self.path.split('?q=')[-1]
            query = unquote(query)
            suggestions = get_suggestions(query)
            self.send_response(200)
            self.send_header('Content-type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(suggestions, ensure_ascii=False).encode('utf-8'))
        elif self.path.startswith('/analyze'):
            query = self.path.split('?q=')[-1]
            from urllib.parse import unquote
            query = unquote(query)

            try:
                data_warning = False
                row = find_niche(query)
                if not row:
                    result = {'error': f'Ниша "{query}" не найдена', 'query': query, 'not_found': True}
                else:
                    insights = get_ai_insights(row)
                    name, products, products_with_sales, sellers, sellers_with_sales, \
                    revenue, potential_revenue, lost_revenue, lost_revenue_pct, orders, \
                    buyout_pct, turnover, profit_pct, avg_rating, rank, commission, avg_price = row
                    sellers_pct = (sellers_with_sales or 0) / (sellers or 1)
                    score = calculate_score(row)
                    verdict = get_verdict(score)

                    try:
                        conn2 = psycopg2.connect(DB)
                        cur2 = conn2.cursor()
                        cur2.execute("""
                            INSERT INTO product_decisions
                            (category, score, verdict, monthly_revenue,
                             avg_orders_per_day, active_sellers, median_price,
                             ai_analysis)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        """, (
                            clean_name(name),
                            score,
                            verdict,
                            float(revenue or 0),
                            float(orders or 0) / 30,
                            int(sellers or 0),
                            float(avg_price or 0),
                            insights['analysis'],
                        ))
                        conn2.commit()
                        cur2.close()
                        conn2.close()
                    except Exception as log_err:
                        print(f"Логирование не удалось: {log_err}")

                    result = {
                        'name': clean_name(name),
                        'data_warning': data_warning,
                        'category': get_category(name),
                        'revenue': float(revenue or 0),
                        'revenue_annual': float(revenue or 0) / 2,
                        'orders': int(orders or 0),
                        'sellers': int(sellers or 0),
                        'sellers_with_sales': int(sellers_with_sales or 0),
                        'score': score,
                        'verdict': verdict,
                        'insights': insights['insights'],
                        'hypotheses': insights['hypotheses'],
                        'analysis': insights['analysis'],
                        'buyout_pct': float(buyout_pct or 0),
                        'turnover': float(turnover or 0),
                        'profit_pct': float(profit_pct or 0),
                        'avg_rating': float(avg_rating or 0),
                        'avg_price': float(avg_price or 0),
                        'commission': float(commission or 0),
                    }

                self.send_response(200)
                self.send_header('Content-type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))

            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())

    def do_POST(self):
        if self.path == '/portfolio-ai':
            try:
                import anthropic
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length))
                answers = body.get('answers', {})
                q1 = answers.get('q1', 'mid')
                q2 = answers.get('q2', 'medium')
                q3 = answers.get('q3', 'no')
                q4 = answers.get('q4', 'mid')
                q5 = answers.get('q5', [])
                usd_rate = 90
                budget_map = {'micro':(0,200),'low':(200,500),'mid':(500,1500),'high':(1500,3000),'premium':(3000,7000)}
                cycle_map = {'fast':(0,45),'medium':(45,90),'slow':(90,180)}
                comp_map = {'low':(0,50),'mid':(50,300),'high':(300,9999)}
                budget_range = budget_map.get(q1,(500,2000))
                cycle_range = cycle_map.get(q2,(45,90))
                comp_range = comp_map.get(q4,(50,300))
                # Смягчённые ценовые диапазоны для большего охвата
                # Цена продажи = бюджет / партия / cost_pct
                # Перекрывающиеся диапазоны чтобы не было пробелов
                batch_map = {'micro':200,'low':150,'mid':100,'high':50,'premium':20}
                cpct_map = {'micro':0.28,'low':0.30,'mid':0.32,'high':0.35,'premium':0.38}
                b = batch_map.get(q1, 100)
                cp = cpct_map.get(q1, 0.32)
                # Минимальная цена — от предыдущего сегмента (перекрытие)
                prev_max_map = {'micro':0,'low':0,'mid':320,'high':1000,'premium':4000}
                price_min = max(0, prev_max_map.get(q1, 0))
                price_max = budget_range[1] * usd_rate / b / cp * 1.2  # +20% перекрытие вверх
                exclude_map = {
                    'Еда и напитки': ['еда','напиток','чай','кофе','шоколад','конфет','продукт','бакалея','сухое','сливк','молок','мука','крупа','сахар','соль','масло подсолн','майонез','соус','уксус','специ','приправ','паста кунжут','закваск','йогурт','кефир','творог','сыр','колбас','мясо','рыб','морепрод','овощ','фрукт','ягод','орех','семен','злак','хлеб','печень','конфет','мармелад','пастил','зефир','варень','джем','мед натур'],
                    'Животные': ['животн','собак','кошк','питомц','ветерин'],
                    'Медицина': ['медицин','лекарств','витамин','бад','аптек'],
                    'Крупногабарит (мебель)': ['мебель','диван','кровать','шкаф','матрас','стол','стул','комод'],
                    'Ювелирка': ['золот','серебр','ювелир','брилиант'],
                    'Автотовары': ['автомобил','шин','масл моторн','автозапч'],
                    'Детское питание': ['детское питание','смесь молочн','пюре детск'],
                    'Строительные материалы': ['строительн','цемент','кирпич','краск','штукатур','ламинат'],
                    'Электроника': ['смартфон','ноутбук','планшет','телевизор','холодильник','стиральн'],
                    'Спортивный инвентарь': ['тренажер','велосипед','самокат','скейт','ролик'],
                    'Садовый инвентарь': ['садов','газонокос','культиват','теплиц'],
                    'Музыкальные инструменты': ['гитар','пианин','скрипк','барабан'],
                    'Антиквариат': ['антиквар','винтаж','коллекцион'],
                    'Пиротехника': ['пиротехник','фейерверк','петард'],
                }
                # q5 теперь — приоритетные направления (не исключения)
                direction_map = {
                    'Здоровье и медицина': ['здоровь','медицин','бандаж','корсет','тонометр','ортопед','компрессион','стельк','небулайз','ингалятор','пульсоксим','глюкометр','термометр'],
                    'Красота и уход': ['красот','косметик','уход','крем','шампун','маск','сыворотк','тушь','помад','тональн','скраб','лосьон','духи','парфюм'],
                    'Одежда и аксессуары': ['одежд','платье','блузк','джемпер','свитер','брюки','джинс','юбк','куртк','пальто','шарф','перчатк','сумк','рюкзак','кошелек'],
                    'Дом и интерьер': ['дом','интерьер','декор','подушк','плед','штор','ковер','органайзер','вешалк','зеркал','свечи','фоторамк'],
                    'Канцелярия и офис': ['канцеляр','тетрадь','ручк','карандаш','папк','файл','степлер','ножниц','маркер','блокнот','ленты чеков'],
                    'Детские товары': ['детск','игрушк','пупс','конструктор','кубик','пазл','пеленальн','стерилизатор','весы детск','слюнявчик'],
                    'Спорт и активный отдых': ['спорт','турник','гантел','коврик для йог','эспандер','скакалк','фитнес','бокс перчатк'],
                    'Инструменты и хозтовары': ['инструмент','отвертк','молоток','пассатиж','уборк','швабр','щетк','губк','перчатки хоз'],
                    'Электроника и гаджеты': ['гаджет','наушник','зарядк','кабель','чехол','повербанк','умн','смарт','bluetooth'],
                    'Автотовары': ['автомобил','авто аксессуар','держатель телефон','видеорегистратор','ароматизатор авто'],
                }
                
                exclude_words = []
                direction_filter = []
                
                if q5:  # Если выбраны направления — ищем только в них
                    for direction in q5:
                        if direction in direction_map:
                            direction_filter.extend(direction_map[direction])
                # Если направления не выбраны — ищем везде, но исключаем проблемные категории
                default_exclude = {
                    'еда': ['молок','сливк','кефир','творог','йогурт','сыр','масло подсолн','мука','крупа','сахар','соль','чай листов','кофе зерн','какао порош','шоколад','конфет','печень слад','пастил','мармелад','варень','джем','мед натур','закваск','паста кунжут','соус','майонез','уксус'],
                }
                if not q5:  # Без выбора направлений — исключаем еду по умолчанию
                    for words in default_exclude.values():
                        exclude_words.extend(words)
                conn = psycopg2.connect(DB)
                cur = conn.cursor()
                sql = 'SELECT name, COALESCE(display_name,name), revenue, avg_price, buyout_pct, turnover, profit_pct, sellers_with_sales, commission, lost_revenue_pct FROM niches WHERE revenue IS NOT NULL AND revenue > 5000000 AND avg_price BETWEEN %s AND %s AND turnover BETWEEN %s AND %s AND sellers_with_sales BETWEEN %s AND %s AND buyout_pct > 0.55 AND profit_pct > 0.20 AND path_verified = TRUE'
                if q3 == 'no':
                    sql += ' AND turnover <= 90'
                for w in exclude_words:
                    sql += ' AND LOWER(name) NOT LIKE %s'
                if direction_filter:
                    dir_conditions = ' OR '.join(['LOWER(name) LIKE %s' for _ in direction_filter])
                    sql += ' AND (' + dir_conditions + ')'
                sql += ' ORDER BY (buyout_pct*0.3+profit_pct*0.3+(1.0/GREATEST(turnover,1))*100*0.4) DESC LIMIT 100'
                params = [price_min, price_max, cycle_range[0], cycle_range[1], comp_range[0], comp_range[1]]
                params += ['%'+w+'%' for w in exclude_words]
                if direction_filter:
                    params += ['%'+w+'%' for w in direction_filter]
                cur.execute(sql, params)
                rows = cur.fetchall()
                if not rows:
                    cur.execute('SELECT name, COALESCE(display_name,name), revenue, avg_price, buyout_pct, turnover, profit_pct, sellers_with_sales, commission, lost_revenue_pct FROM niches WHERE revenue IS NOT NULL AND revenue > 10000000 AND buyout_pct > 0.6 AND profit_pct > 0.25 AND turnover < 90 AND path_verified = TRUE ORDER BY buyout_pct DESC LIMIT 50')
                    rows = cur.fetchall()
                cur.close(); conn.close()
                niches_data = []
                for row in rows[:30]:
                    nm,dn,rev,ap,bp,tv,pp,sw,cm,lrp = row
                    ap=float(ap or 0); bp=float(bp or 0); tv=float(tv or 30)
                    pp=float(pp or 0); cm=float(cm or 0.15)
                    # Размер партии и закупочная цена зависят от бюджетного сегмента
                    if q1 == 'micro':    # до $200
                        batch = 200; cost_pct = 0.28
                    elif q1 == 'low':    # $200-500
                        batch = 150; cost_pct = 0.30
                    elif q1 == 'mid':    # $500-1500
                        batch = 100; cost_pct = 0.32
                    elif q1 == 'high':   # $1500-3000
                        batch = 50; cost_pct = 0.35
                    else:                # $3000-7000
                        batch = 20; cost_pct = 0.38
                    # Закупочная цена зависит от сегмента
                    pp_cost = ap * cost_pct

                    # Вес единицы товара зависит от цены (грубая оценка)
                    if ap < 500:       weight_kg = 0.1   # мелочь
                    elif ap < 1500:    weight_kg = 0.3   # лёгкий товар
                    elif ap < 5000:    weight_kg = 0.8   # средний товар
                    elif ap < 15000:   weight_kg = 2.0   # тяжёлый товар
                    else:              weight_kg = 5.0   # крупный товар

                    # Стоимость карго доставки из Китая в РБ
                    # Карго: $2.5-4/кг авиа или $1.5-2/кг ж/д
                    cargo_rate_usd = 3.0  # средний карго тариф $/кг
                    delivery = batch * weight_kg * cargo_rate_usd * usd_rate

                    # Таможня РБ: пошлина 10% + НДС 20%
                    purchase = pp_cost * batch
                    customs = purchase * 0.10
                    vat = (purchase + customs) * 0.20

                    # Реклама: 10-15% от выручки первого месяца
                    monthly_orders = max(1, int(30 / max(tv, 1) * batch * float(bp)))
                    monthly_revenue = monthly_orders * ap
                    ad = monthly_revenue * 0.12

                    # Оформление карточки (фото, инфографика, SEO)
                    card_cost = 15000

                    # Полная стоимость входа
                    entry = purchase + delivery + customs + vat + card_cost

                    # Логистика WB зависит от веса/объёма
                    if weight_kg <= 0.5:    wb_log = 75
                    elif weight_kg <= 1.5:  wb_log = 120
                    elif weight_kg <= 5:    wb_log = 200
                    else:                   wb_log = 350

                    # Прибыль за цикл (с учётом выкупа и возвратов)
                    wb_commission = ap * float(cm)
                    profit_u = (ap - pp_cost - wb_commission - wb_log) * float(bp)
                    profit_c = profit_u * batch
                    niches_data.append({'name':dn,'full':nm,'avg_price':round(ap),'turnover':round(tv),'buyout_pct':round(bp*100),'profit_pct':round(pp*100),'entry_cost_rub':round(entry),'ad_budget_rub':round(ad),'profit_cycle_rub':round(profit_c)})
                lines = []
                for i,n in enumerate(niches_data):
                    seasonal_note = ' | СЕЗОННЫЙ: ' + n.get('seasonal_info','') if n.get('seasonal_info') else ''
                    lines.append(str(i+1)+'. '+n['name']+' | цена '+str(n['avg_price'])+'руб | оборот '+str(n['turnover'])+'дн | выкуп '+str(n['buyout_pct'])+'% | маржа '+str(n['profit_pct'])+'% | вход '+str(n['entry_cost_rub'])+'руб | прибыль '+str(n['profit_cycle_rub'])+'руб'+seasonal_note)
                nt = chr(10).join(lines)
                bl={'micro':'до $200','low':'$200-500','mid':'$500-1500','high':'$1500-3000','premium':'$3000-7000'}
                cl={'fast':'8-12 циклов/год','medium':'4-6 циклов/год','slow':'2-4 цикла/год'}
                sez = 'Да' if q3=='yes' else 'Нет'
                excl = ', '.join(q5) if q5 else 'нет'
                prompt = 'Ты эксперт WB. Отбери 8-10 ниш для стартового портфеля торговой компании.' + chr(10)
                prompt += 'ПАРАМЕТРЫ:' + chr(10)
                batch_info = {'micro':'200 шт по ~$0.5-1/шт','low':'150 шт по ~$1.3-3.3/шт','mid':'100 шт по ~$5-15/шт','high':'50 шт по ~$30-60/шт','premium':'20 шт по ~$150-350/шт'}
                prompt += 'Бюджет на 1 SKU: ' + bl.get(q1,q1) + ' (пробная партия: ' + batch_info.get(q1,'50 шт') + ')' + chr(10)
                prompt += 'Оборот: ' + cl.get(q2,q2) + chr(10)
                prompt += 'Сезонные товары: ' + sez + chr(10)
                prompt += 'Конкуренция: ' + q4 + chr(10)
                prompt += 'Исключены: ' + excl + chr(10)
                prompt += 'Доставка из Китая: 45 дней карго' + chr(10) + chr(10)
                prompt += 'КАНДИДАТЫ:' + chr(10) + nt + chr(10) + chr(10)
                # Сезонный календарь — когда заказывать товар
                seasonal_calendar = {
                    # (месяц начала сезона, месяц конца сезона, когда заказывать)
                    'купальн': (5, 8, 3, 'Заказать в марте, пик июнь-июль'),
                    'плавк': (5, 8, 3, 'Заказать в марте, пик июнь-август'),
                    'пляж': (5, 8, 3, 'Заказать в марте-апреле'),
                    'сарафан': (4, 8, 2, 'Заказать в феврале, пик май-июль'),
                    'шорты': (4, 8, 2, 'Заказать в феврале-марте'),
                    'зонт': (4, 9, 2, 'Заказать в феврале, актуален весь сезон'),
                    'дождевик': (3, 10, 1, 'Заказать в январе, пик апрель-май'),
                    'купальник': (5, 8, 3, 'Заказать в марте'),
                    'лыж': (11, 3, 9, 'Заказать в сентябре, пик декабрь-февраль'),
                    'сноуборд': (11, 3, 9, 'Заказать в сентябре'),
                    'коньк': (11, 3, 9, 'Заказать в сентябре-октябре'),
                    'санк': (11, 2, 9, 'Заказать в сентябре, пик декабрь-январь'),
                    'пуховик': (10, 2, 8, 'Заказать в августе, пик октябрь-январь'),
                    'шуб': (10, 2, 8, 'Заказать в августе'),
                    'шапк': (10, 2, 8, 'Заказать в августе-сентябре'),
                    'варежк': (10, 2, 8, 'Заказать в августе'),
                    'перчатк': (10, 2, 8, 'Заказать в августе'),
                    'термобель': (10, 3, 8, 'Заказать в августе'),
                    'новогод': (11, 1, 9, 'Заказать в сентябре, пик декабрь'),
                    'ёлочн': (11, 1, 9, 'Заказать в сентябре'),
                    'гирлянд': (11, 1, 9, 'Заказать в сентябре'),
                    'мангал': (4, 8, 2, 'Заказать в феврале, пик май-июль'),
                    'барбекю': (4, 8, 2, 'Заказать в феврале'),
                    'садов': (3, 9, 1, 'Заказать в январе, пик апрель-август'),
                    'огород': (3, 7, 1, 'Заказать в январе'),
                    'палатк': (4, 9, 2, 'Заказать в феврале, пик май-август'),
                    'спальный мешок': (4, 9, 2, 'Заказать в феврале'),
                    'велосипед': (3, 9, 1, 'Заказать в январе, пик апрель-июль'),
                    'самокат': (3, 9, 1, 'Заказать в январе-феврале'),
                    'роллер': (4, 9, 2, 'Заказать в феврале'),
                    'кепи': (4, 9, 2, 'Заказать в феврале'),
                    'панам': (4, 9, 2, 'Заказать в феврале'),
                    'солнцезащитн': (4, 9, 2, 'Заказать в феврале'),
                    'крем солнц': (4, 9, 2, 'Заказать в феврале'),
                    'репеллент': (4, 9, 2, 'Заказать в феврале'),
                    'пасхальн': (3, 4, 1, 'Заказать в январе, пик март-апрель'),
                    'валентин': (1, 2, 11, 'Заказать в ноябре, пик февраль'),
                    'школьн': (7, 9, 5, 'Заказать в мае, пик август'),
                    'рюкзак школ': (7, 9, 5, 'Заказать в мае'),
                }

                # Определяем сезонность каждой ниши
                import datetime
                current_month = datetime.datetime.now().month
                for n in niches_data:
                    name_lower = n['name'].lower()
                    seasonal_info = None
                    for keyword, (start_m, end_m, order_m, advice) in seasonal_calendar.items():
                        if keyword in name_lower:
                            # Считаем сколько месяцев до нужно заказать
                            months_until_order = (order_m - current_month) % 12
                            if months_until_order == 0:
                                urgency = 'СРОЧНО — время заказывать СЕЙЧАС'
                            elif months_until_order <= 1:
                                urgency = f'Заказать через {months_until_order} мес'
                            elif months_until_order <= 2:
                                urgency = f'Заказать через {months_until_order} мес'
                            else:
                                urgency = f'Заказать через {months_until_order} мес'
                            seasonal_info = f'{advice}. {urgency}.'
                            break
                    n['seasonal_info'] = seasonal_info

                prompt += 'ОБЯЗАТЕЛЬНО выбери ровно 10-12 ниш. Диверсификация по категориям. Баланс: быстрые(оборот<45дн) + маржинальные(маржа>60%) + стабильные.' + chr(10)
                prompt += 'Верни ТОЛЬКО валидный JSON без markdown:' + chr(10)
                prompt += '{"summary":{"title":"название","description":"3-4 предложения","total_budget_rub":0,"monthly_potential_rub":0,"payback_months":0},"niches":[{"name":"название","full":"полное","priority":"high|medium|low","turnover_days":0,"margin_pct":0,"buyout_pct":0,"entry_cost_rub":0,"ad_budget_rub":0,"profit_per_cycle_rub":0,"reason":"2-3 предложения","seasonal_warning":null}]}'
                client=anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
                msg=client.messages.create(model='claude-sonnet-4-5',max_tokens=3000,messages=[{'role':'user','content':prompt}])
                raw=msg.content[0].text.strip().replace('```json','').replace('```','').strip()
                result=json.loads(raw)
                self.send_response(200)
                self.send_header('Content-type','application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(result,ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type','application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error':str(e)}).encode('utf-8'))
        elif self.path == '/ad-analysis':
            try:
                import anthropic
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length))
                niche_name = body.get('niche_name', '')
                avg_cpm = body.get('avg_cpm', 0)
                ad_pct = body.get('ad_pct', 0)
                cpm_status = body.get('cpm_status', 'yellow')
                ad_verdict = body.get('ad_verdict', '')
                top_ad_sellers = body.get('top_ad_sellers', [])
                avg_price = body.get('avg_price', 0)
                revenue = body.get('revenue', 0)
                sellers_with_sales = body.get('sellers_with_sales', 0)
                profit_pct = body.get('profit_pct', 0)
                buyout_pct = body.get('buyout_pct', 0)
                turnover = body.get('turnover', 0)
                commission = body.get('commission', 0)
                top_sellers_str = ', '.join([f"{s['name']} ({s['count']} тов.)" for s in top_ad_sellers[:5]]) if top_ad_sellers else 'нет данных'
                prompt = f"""Ты профессиональный рекламный аналитик Wildberries с опытом 7+ лет. Проведи глубокий анализ и разработай детальную стратегию.

ДАННЫЕ НИШИ: {niche_name}
- Средний CPM: {avg_cpm} руб (статус: {cpm_status})
- Товаров с рекламой: {ad_pct}%
- Вердикт нагрузки: {ad_verdict}
- Топ рекламодатели: {top_sellers_str}
- Средняя цена: {avg_price} руб
- Выручка ниши: {revenue:,.0f} руб
- Продавцов с продажами: {sellers_with_sales}
- Маржинальность до себест.: {profit_pct*100:.1f}%
- Процент выкупа: {buyout_pct*100:.1f}%
- Оборачиваемость: {turnover} дней
- Комиссия WB: {commission*100:.1f}%

Верни ТОЛЬКО валидный JSON без markdown и пояснений:
{{
  "load_level": "low|medium|high",
  "load_analysis": "детальный анализ рекламной нагрузки 3-4 предложения с конкретными выводами",
  "strategy_type": "название стратегии (например: Автореклама + Поиск)",
  "strategy_detail": "объяснение почему именно эта стратегия 3-4 предложения",
  "strategy_steps": ["Шаг 1 с цифрами","Шаг 2 с цифрами","Шаг 3 с цифрами","Шаг 4 с цифрами","Шаг 5 с цифрами"],
  "budget": {{
    "start_rub": число,
    "growth_rub": число,
    "sustain_rub": число,
    "comment": "логика бюджетов с учётом CPM и маржи 2-3 предложения"
  }},
  "cpm_forecast": {{
    "start_rub": число,
    "month2_rub": число,
    "comment": "почему такой CPM и как снизить 2-3 предложения"
  }},
  "forecast": {{
    "month1": {{
      "metrics": ["CTR: X.X%","CR в заказ: X.X%","ДРР: XX%","Позиция: топ XX","Заказов: ~XX шт"]
    }},
    "month2": {{
      "metrics": ["CTR: X.X%","CR в заказ: X.X%","ДРР: XX%","Позиция: топ XX","Заказов: ~XX шт"]
    }}
  }}
}}
Все бюджеты в рублях. ДРР не должен превышать маржинальность. Будь максимально конкретным."""
                client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
                message = client.messages.create(model="claude-sonnet-4-5", max_tokens=2000, messages=[{"role":"user","content":prompt}])
                raw = message.content[0].text.strip().replace('```json','').replace('```','').strip()
                analysis = json.loads(raw)
                conn2 = psycopg2.connect(DB)
                cur2 = conn2.cursor()
                cur2.execute("""INSERT INTO ad_forecasts (niche_name,avg_cpm,ad_pct,avg_price,revenue,sellers_with_sales,profit_pct,forecast_data) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (niche_name,avg_cpm,ad_pct,avg_price,revenue,sellers_with_sales,profit_pct,json.dumps(analysis,ensure_ascii=False)))
                forecast_id = cur2.fetchone()[0]
                conn2.commit(); cur2.close(); conn2.close()
                self.send_response(200)
                self.send_header('Content-type','application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({'analysis':analysis,'forecast_id':forecast_id},ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type','application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error':str(e)}).encode('utf-8'))

        elif self.path == '/ad-monitor':
            try:
                import anthropic
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length))
                forecast_id = body.get('forecast_id')
                month = body.get('month', 1)
                actual = body.get('actual', {})
                conn2 = psycopg2.connect(DB)
                cur2 = conn2.cursor()
                cur2.execute("SELECT niche_name, forecast_data, avg_cpm, avg_price, profit_pct FROM ad_forecasts WHERE id = %s", (forecast_id,))
                row = cur2.fetchone()
                if not row:
                    raise Exception('Прогноз не найден')
                niche_name, forecast_data, avg_cpm, avg_price, profit_pct = row
                forecast = forecast_data if isinstance(forecast_data, dict) else json.loads(forecast_data)
                month_key = f'month{month}'
                planned_metrics = forecast.get('forecast',{}).get(month_key,{}).get('metrics',[])
                planned_budget = forecast.get('budget',{}).get('start_rub' if month==1 else 'growth_rub', 0)
                planned_cpm = forecast.get('cpm_forecast',{}).get('start_rub' if month==1 else 'month2_rub', 0)
                prompt = f"""Ты аналитик рекламы Wildberries. Сравни плановые и фактические показатели за месяц {month} и дай честную диагностику.

НИША: {niche_name}
ПЛАН:
{chr(10).join(['- '+m for m in planned_metrics])}
- Плановый бюджет: {planned_budget:,.0f} руб
- Плановый CPM: {planned_cpm:,.0f} руб

ФАКТ:
- CPM: {actual.get('cpm',0):,.0f} руб
- CTR: {actual.get('ctr',0)}%
- Расход: {actual.get('spend',0):,.0f} руб
- Заказов с рекламы: {actual.get('orders',0)} шт
- Средняя позиция: {actual.get('position',0)}
- Выручка за месяц: {actual.get('revenue',0):,.0f} руб

КОНТЕКСТ: CPM ниши={avg_cpm} руб, цена={avg_price} руб, маржа={float(profit_pct or 0)*100:.1f}%

Верни ТОЛЬКО валидный JSON:
{{
  "status": "good|warning|bad",
  "comparison": [
    {{"metric":"CPM","plan":"X руб","actual":"Y руб","status":"good|warning|bad"}},
    {{"metric":"CTR","plan":"X%","actual":"Y%","status":"good|warning|bad"}},
    {{"metric":"Расход","plan":"X руб","actual":"Y руб","status":"good|warning|bad"}},
    {{"metric":"Заказы","plan":"X шт","actual":"Y шт","status":"good|warning|bad"}},
    {{"metric":"Позиция","plan":"топ X","actual":"топ Y","status":"good|warning|bad"}}
  ],
  "summary": "честный анализ 3-4 предложения что работает что нет и почему с учётом специфики ниши",
  "recommendations": ["рекомендация 1 с цифрами","рекомендация 2 с цифрами","рекомендация 3 с цифрами","рекомендация 4 с цифрами"]
}}"""
                client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
                message = client.messages.create(model="claude-sonnet-4-5", max_tokens=1500, messages=[{"role":"user","content":prompt}])
                raw = message.content[0].text.strip().replace('```json','').replace('```','').strip()
                diagnosis = json.loads(raw)
                cur2.execute(f"""UPDATE ad_forecasts SET month{month}_actual=%s, month{month}_diagnosis=%s, month{month}_checked_at=NOW() WHERE id=%s""",
                    (json.dumps(actual,ensure_ascii=False), json.dumps(diagnosis,ensure_ascii=False), forecast_id))
                conn2.commit(); cur2.close(); conn2.close()
                self.send_response(200)
                self.send_header('Content-type','application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({'diagnosis':diagnosis},ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type','application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error':str(e)}).encode('utf-8'))

        elif self.path == '/unit-economy':
            try:
                import anthropic
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length))

                niche_name = body.get('niche_name', '')
                price_rub = body.get('price_rub', 0)
                cost_rub = body.get('cost_rub', 0)
                cost_local = body.get('cost_local', 0)
                cost_currency = body.get('cost_currency', 'cny')
                commission_pct = body.get('commission_pct', 15)
                buyout_pct = body.get('buyout_pct', 70)
                length_cm = body.get('length_cm', 20)
                width_cm = body.get('width_cm', 15)
                height_cm = body.get('height_cm', 10)
                weight_kg = body.get('weight_kg', 0.5)
                units_per_box = body.get('units_per_box', 10)
                delivery_mode = body.get('delivery_mode', 'sea')
                customs_pct = body.get('customs_pct', 10)
                vat_pct = body.get('vat_pct', 20)
                tax_pct = body.get('tax_pct', 6)
                revenue = body.get('revenue', 0)
                sym = body.get('symbol', '₽')

                # Расчёт объёма и веса единицы товара
                # length/width/height — габариты ЕДИНИЦЫ товара
                volume_unit_m3 = (length_cm * width_cm * height_cm) / 1000000
                weight_unit_kg = weight_kg  # вес единицы товара

                # Тарифы доставки Китай→Беларусь/Россия
                delivery_rates = {
                    'sea': {'usd_per_kg': 2.0, 'days': '45-60', 'name': 'Море'},
                    'rail': {'usd_per_kg': 2.8, 'days': '18-25', 'name': 'ЖД'},
                    'truck': {'usd_per_kg': 4.0, 'days': '20-30', 'name': 'Авто'}
                }
                del_rate = delivery_rates.get(delivery_mode, delivery_rates['sea'])
                usd_rate = 90  # RUB/USD
                cny_rate = 12.5  # RUB/CNY

                # Объёмный вес (используем максимум из фактического и объёмного)
                # Стандарт: объёмный вес = объём(м3) * 250 кг/м3
                volumetric_weight = volume_unit_m3 * 250
                chargeable_weight = max(weight_unit_kg, volumetric_weight)

                # Стоимость доставки на единицу товара
                delivery_cost_usd = del_rate['usd_per_kg'] * chargeable_weight
                delivery_cost_rub = delivery_cost_usd * usd_rate

                # Объём короба для информации
                box_weight = weight_unit_kg * units_per_box
                volume_m3 = volume_unit_m3 * units_per_box

                # Таможня и НДС от себестоимости
                customs_rub = cost_rub * (customs_pct / 100)
                vat_rub = (cost_rub + customs_rub) * (vat_pct / 100)

                # Логистика WB (зависит от объёма единицы)
                volume_dm3 = volume_unit_m3 * 1000  # литры на единицу
                if volume_dm3 <= 1:
                    wb_logistics = 75
                elif volume_dm3 <= 5:
                    wb_logistics = 120
                elif volume_dm3 <= 20:
                    wb_logistics = 200
                else:
                    wb_logistics = 350

                # Комиссия WB
                wb_commission = price_rub * (commission_pct / 100)

                # Налог УСН
                tax_rub = price_rub * (tax_pct / 100)

                # Стоимость возвратов (учитываем выкуп)
                return_cost = wb_logistics * (1 - buyout_pct/100) * 0.5

                # СЦЕНАРИЙ 1: Китай → WB Беларусь (FBO)
                # Себестоимость + доставка + таможня + НДС + хранение WB
                storage_fbo = price_rub * 0.02  # ~2% от цены
                s1_total_cost = cost_rub + delivery_cost_rub + customs_rub + vat_rub + storage_fbo
                s1_wb_cost = wb_commission + wb_logistics + return_cost + tax_rub
                s1_profit = price_rub - s1_total_cost - s1_wb_cost
                s1_roi = round(s1_profit / s1_total_cost * 100, 1) if s1_total_cost > 0 else 0
                s1_margin = round(s1_profit / price_rub * 100, 1) if price_rub > 0 else 0

                # СЦЕНАРИЙ 2: Китай → свой склад РБ → WB (FBS)
                # Добавляем аренду склада и упаковку
                warehouse_rent = price_rub * 0.015  # ~1.5% от цены
                packing_cost = 30  # руб упаковка
                wb_logistics_fbs = wb_logistics * 1.3  # FBS дороже
                s2_total_cost = cost_rub + delivery_cost_rub + customs_rub + vat_rub + warehouse_rent + packing_cost
                s2_wb_cost = wb_commission + wb_logistics_fbs + return_cost * 0.7 + tax_rub
                s2_profit = price_rub - s2_total_cost - s2_wb_cost
                s2_roi = round(s2_profit / s2_total_cost * 100, 1) if s2_total_cost > 0 else 0
                s2_margin = round(s2_profit / price_rub * 100, 1) if price_rub > 0 else 0

                # СЦЕНАРИЙ 3: Китай → WB Россия (FBO РФ)
                # Доставка в Москву дороже + таможня РФ
                delivery_cost_rub_rf = delivery_cost_rub * 1.15  # +15% до РФ
                customs_rub_rf = cost_rub * (customs_pct / 100)
                vat_rub_rf = (cost_rub + customs_rub_rf) * 0.20
                s3_total_cost = cost_rub + delivery_cost_rub_rf + customs_rub_rf + vat_rub_rf + storage_fbo
                s3_wb_cost = wb_commission + wb_logistics + return_cost + tax_rub
                s3_profit = price_rub - s3_total_cost - s3_wb_cost
                s3_roi = round(s3_profit / s3_total_cost * 100, 1) if s3_total_cost > 0 else 0
                s3_margin = round(s3_profit / price_rub * 100, 1) if price_rub > 0 else 0

                def get_verdict(profit):
                    if profit > price_rub * 0.15: return 'profit'
                    if profit > 0: return 'marginal'
                    return 'loss'

                scenarios = {
                    's1': {
                        'title': '🇧🇾 Китай → WB Беларусь (FBO)',
                        'total_cost_rub': round(s1_total_cost),
                        'wb_commission_rub': round(wb_commission),
                        'wb_logistics_rub': round(wb_logistics),
                        'profit_per_unit_rub': round(s1_profit),
                        'roi_pct': s1_roi,
                        'margin_pct': s1_margin,
                        'verdict': get_verdict(s1_profit),
                        'comment': f'Доставка {del_rate["name"]} {del_rate["days"]} дней'
                    },
                    's2': {
                        'title': '🏭 Китай → склад РБ → WB (FBS)',
                        'total_cost_rub': round(s2_total_cost),
                        'wb_commission_rub': round(wb_commission),
                        'wb_logistics_rub': round(wb_logistics_fbs),
                        'profit_per_unit_rub': round(s2_profit),
                        'roi_pct': s2_roi,
                        'margin_pct': s2_margin,
                        'verdict': get_verdict(s2_profit),
                        'comment': 'Аренда склада + упаковка своими силами'
                    },
                    's3': {
                        'title': '🇷🇺 Китай → WB Россия (FBO)',
                        'total_cost_rub': round(s3_total_cost),
                        'wb_commission_rub': round(wb_commission),
                        'wb_logistics_rub': round(wb_logistics),
                        'profit_per_unit_rub': round(s3_profit),
                        'roi_pct': s3_roi,
                        'margin_pct': s3_margin,
                        'verdict': get_verdict(s3_profit),
                        'comment': 'Доставка в РФ +15% к стоимости логистики'
                    }
                }

                calc_details = [
                    {'label': 'Цена продажи', 'value': f'{round(price_rub):,} ₽'},
                    {'label': f'Закупка ({cost_currency.upper()})', 'value': f'{cost_local} → {round(cost_rub):,} ₽'},
                    {'label': f'Доставка ({del_rate["name"]})', 'value': f'{round(delivery_cost_rub):,} ₽/ед'},
                    {'label': f'Таможня {customs_pct}%', 'value': f'{round(customs_rub):,} ₽'},
                    {'label': f'НДС {vat_pct}%', 'value': f'{round(vat_rub):,} ₽'},
                    {'label': f'Комиссия WB {commission_pct}%', 'value': f'{round(wb_commission):,} ₽'},
                    {'label': 'Логистика WB', 'value': f'{round(wb_logistics):,} ₽'},
                    {'label': f'Налог УСН {tax_pct}%', 'value': f'{round(tax_rub):,} ₽'},
                    {'label': 'Стоимость возвратов', 'value': f'{round(return_cost):,} ₽'},
                    {'label': 'Объём короба', 'value': f'{round(volume_unit_m3*1000, 1)} л/ед / {round(chargeable_weight, 2)} кг (расч.)'},
                ]

                # Claude даёт рекомендацию
                prompt = f"""Ты эксперт по юнит-экономике Wildberries. Проанализируй расчёты и дай рекомендацию.

НИША: {niche_name}
ЦЕНА ПРОДАЖИ: {price_rub} руб
ЗАКУПКА: {cost_local} {cost_currency.upper()} = {round(cost_rub)} руб

РЕЗУЛЬТАТЫ 3 СЦЕНАРИЕВ:
1. Китай→WB Беларусь (FBO): прибыль {round(s1_profit)} руб, ROI {s1_roi}%, маржа {s1_margin}%
2. Китай→склад РБ→WB (FBS): прибыль {round(s2_profit)} руб, ROI {s2_roi}%, маржа {s2_margin}%
3. Китай→WB Россия (FBO): прибыль {round(s3_profit)} руб, ROI {s3_roi}%, маржа {s3_margin}%

Верни ТОЛЬКО JSON:
{{
  "title": "Сценарий X — название (лучший вариант)",
  "detail": "3-4 предложения: почему этот сценарий лучше, конкретные цифры, что нужно сделать для старта, риски"
}}"""

                client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
                message = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=500,
                    messages=[{"role": "user", "content": prompt}]
                )
                raw = message.content[0].text.strip().replace('```json','').replace('```','').strip()
                recommendation = json.loads(raw)

                self.send_response(200)
                self.send_header('Content-type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'scenarios': scenarios,
                    'calc_details': calc_details,
                    'recommendation': recommendation
                }, ensure_ascii=False).encode('utf-8'))

            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))

        elif self.path == '/supplier-analysis':
            try:
                import anthropic
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length))
                niche_name = body.get('niche_name', '')
                display_name = body.get('display_name', niche_name)
                avg_price = float(body.get('avg_price', 0))
                avg_price_usd = round(avg_price / 90, 1)
                top_items = body.get('top_items', [])
                top_str = ''
                for i, item in enumerate(top_items[:3]):
                    nm = str(item.get('name', ''))
                    pr = str(item.get('price', 0))
                    sl = str(item.get('sales', 0))
                    top_str += str(i+1) + '. ' + nm + ' | ' + pr + ' руб | ' + sl + ' прод/мес' + chr(10)
                p = []
                p.append('Ты эксперт по закупкам в Китае. Найди закупочные цены для конкретных товаров.')
                p.append('НИША: ' + display_name)
                p.append('Средняя цена WB: ' + str(avg_price) + ' руб ($' + str(avg_price_usd) + ')')
                if top_str:
                    p.append('ТОП ТОВАРЫ НА WB (найди их аналоги в Китае):')
                    p.append(top_str)
                p.append('Задача: найти эти товары или аналоги на Taobao/1688/Alibaba и указать реальные цены.')
                p.append('Учти WB комиссию ~25%, логистику ~120 руб/шт для расчёта маржи.')
                p.append('')
                p.append('Верни ТОЛЬКО JSON без markdown:')
                json_tmpl = '{"price_taobao_usd": 0, "price_alibaba_usd": 0, "moq": 0, "summary": "текст", "search_links": [{"platform": "Alibaba", "url": "https://www.alibaba.com/trade/search?SearchText=QUERY_EN", "icon": "строка", "description": "строка"}, {"platform": "Made-in-China", "url": "https://www.made-in-china.com/multi-search/QUERY_EN/F1/0.html", "icon": "строка", "description": "строка"}, {"platform": "AliExpress", "url": "https://www.aliexpress.com/wholesale?SearchText=QUERY_EN", "icon": "строка", "description": "розничные цены для оценки"}, {"platform": "1688 (нужна регистрация)", "url": "https://s.1688.com/selloffer/offer_search.htm?keywords=QUERY_CN", "icon": "строка", "description": "самые низкие оптовые цены"}, {"platform": "Taobao (нужна регистрация)", "url": "https://s.taobao.com/search?q=QUERY_CN", "icon": "строка", "description": "розница и мелкий опт"}], "real_margin_pct": 0, "roi_pct": 0, "profit_per_unit_rub": 0}'
                p.append(json_tmpl)
                prompt = chr(10).join(p)
                client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
                msg = client.messages.create(model='claude-sonnet-4-5', max_tokens=2000,
                    messages=[{'role': 'user', 'content': prompt}])
                raw = msg.content[0].text.strip().replace('```json', '').replace('```', '').strip()
                result = json.loads(raw)
                self.send_response(200)
                self.send_header('Content-type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))

        elif self.path == '/docs-analysis':
            try:
                import anthropic
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length))
                niche_name = body.get('niche_name', '')
                display_name = body.get('display_name', niche_name)
                avg_price = body.get('avg_price', 0)

                prompt = f"""Ты эксперт по сертификации и документообороту для торговли на Wildberries.
Торговая компания зарегистрирована в Республике Беларусь (РБ), закупает товары в Китае и продаёт на WB в РФ и РБ.

НИША: {display_name}
Средняя цена товара: {avg_price} руб

Дай детальный анализ по 5 блокам:
1. ДОКУМЕНТЫ ДЛЯ WB — что требует маркетплейс
2. СЕРТИФИКАЦИЯ В ЕАЭС — декларации и сертификаты ТР ТС
3. ГДЕ ПОЛУЧАТЬ — конкретные организации в РБ (БелГИСС, БГЦА) и РФ (Ростест, Роспотребнадзор), где выгоднее
4. ЧТО ЗАПРОСИТЬ У КИТАЙСКОГО ПОСТАВЩИКА — CoA, ISO, CE, Test reports, что принимается в ЕАЭС
5. ТАМОЖНЯ РБ — документы для ввоза из Китая

Верни ТОЛЬКО валидный JSON без markdown:
{{
  "complexity": "low|medium|high",
  "summary": "3-4 предложения об общей сложности для белорусской компании",
  "wb_docs": [
    {{
      "name": "название",
      "required": true,
      "description": "что это и зачем",
      "cost": "стоимость в рублях РФ",
      "duration": "срок получения",
      "where_rb": "где получить в РБ",
      "where_rf": "где получить в РФ",
      "better_in": "РБ или РФ"
    }}
  ],
  "supplier_docs": [
    {{
      "name": "документ от поставщика",
      "description": "что даёт",
      "accepted_in_eaes": true
    }}
  ],
  "customs_docs": ["таможенное требование"],
  "risks": ["риск"],
  "total_cost": "общая стоимость в руб РФ",
  "total_cost_byn": "общая стоимость в белорусских рублях",
  "total_duration": "общий срок"
}}"""

                client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
                message = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=4000,
                    messages=[{"role": "user", "content": prompt}]
                )
                raw = message.content[0].text.strip().replace('```json','').replace('```','').strip()
                result = json.loads(raw)

                self.send_response(200)
                self.send_header('Content-type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))

            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))

        elif self.path == '/warehouse-analysis':
            try:
                import anthropic
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length))
                niche_name = body.get('niche_name', '')
                avg_price = body.get('avg_price', 0)
                revenue = body.get('revenue', 0)
                turnover = body.get('turnover', 0)
                buyout_pct = body.get('buyout_pct', 0)
                profit_pct = body.get('profit_pct', 0)
                commission = body.get('commission', 0)
                w = body.get('warehouse_stats', {})

                prompt = f"""Ты эксперт по логистике и поставкам на Wildberries. Проанализируй данные топ-30 SKU ниши и дай конкретные рекомендации по стратегии поставок для новичка.

НИША: {niche_name}
ДАННЫЕ НИШИ:
- Средняя цена: {avg_price} руб
- Выручка ниши: {revenue:,.0f} руб
- Оборачиваемость: {turnover} дней
- Выкуп: {buyout_pct*100:.1f}%
- Маржинальность: {profit_pct*100:.1f}%
- Комиссия WB: {commission*100:.1f}%

ДАННЫЕ ТОП-30 SKU:
- FBS товаров: {w.get('fbs_count',0)} ({w.get('fbs_pct',0)}%)
- FBO товаров: {w.get('fbo_count',0)} ({w.get('fbo_pct',0)}%)
- Среднее складов у лидеров топ-10: {w.get('avg_wh_leaders',0)}
- Среднее складов у остальных: {w.get('avg_wh_others',0)}
- Средняя оборачиваемость: {w.get('avg_turnover',0)} дней
- Среднее дней в наличии: {w.get('avg_days_stock',0)} дней
- Товаров с заморозкой >10%: {w.get('frozen_pct',0)}%
- Средний остаток FBO: {w.get('avg_balance',0)} шт
- Средний остаток FBS: {w.get('avg_balance_fbs',0)} шт

Верни ТОЛЬКО валидный JSON без markdown:
{{
  "model": "FBS / FBO / Смешанная FBS+FBO",
  "model_color": "fbs|fbo|mixed",
  "model_detail": "детальное объяснение почему именно эта модель для данной ниши 3-4 предложения с конкретными цифрами",
  "warehouse_tips": [
    "конкретный совет 1 с цифрами",
    "конкретный совет 2 с цифрами",
    "конкретный совет 3 с цифрами",
    "конкретный совет 4 с цифрами"
  ],
  "stock": {{
    "min_units": число (минимальная первая поставка в штуках),
    "opt_units": число (оптимальная первая поставка),
    "min_rub": число (стоимость минимальной поставки в рублях по себестоимости ~40% от цены),
    "opt_rub": число (стоимость оптимальной поставки),
    "days_covered": число (на сколько дней хватит оптимального запаса),
    "comment": "логика расчёта объёма поставки 2-3 предложения"
  }},
  "risks": [
    "риск 1 специфичный для данной ниши",
    "риск 2 специфичный для данной ниши",
    "риск 3 специфичный для данной ниши"
  ]
}}
Все суммы в рублях. Будь конкретным — никаких размытых диапазонов."""

                client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
                message = client.messages.create(model="claude-sonnet-4-5", max_tokens=2000, messages=[{"role":"user","content":prompt}])
                raw = message.content[0].text.strip().replace('```json','').replace('```','').strip()
                analysis = json.loads(raw)
                self.send_response(200)
                self.send_header('Content-type','application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({'analysis':analysis},ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type','application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error':str(e)}).encode('utf-8'))

        else:
            self.send_response(404)
            self.end_headers()


if __name__ == '__main__':
    print("🚀 Сервер запущен: http://localhost:8080")
    HTTPServer(('', 8080), Handler).serve_forever()
