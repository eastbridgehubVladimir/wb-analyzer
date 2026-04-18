from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import asyncio
import psycopg2
import datetime
import sys
import requests as mpstats_req
import os
from dotenv import load_dotenv
load_dotenv()

os.chdir(os.path.dirname(os.path.abspath(__file__)))

DB = os.getenv("DATABASE_URL", "postgresql://user@localhost:5432/wb_saas")
MPSTATS_TOKEN = os.getenv("MPSTATS_TOKEN", "")

def clean_name(name):
    """Убирает префикс категории из названия ниши."""
    if not name:
        return name
    parts = name.split(' ', 1)
    if len(parts) > 1:
        return parts[1]
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
        WHERE LOWER(name) LIKE LOWER(%s) AND revenue IS NOT NULL
        ORDER BY revenue DESC LIMIT 1
    """, (f"%{query}%",))
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
    """Умный расчёт score на основе всех доступных метрик."""
    name, products, products_with_sales, sellers, sellers_with_sales, \
    revenue, potential_revenue, lost_revenue, lost_revenue_pct, orders, \
    buyout_pct, turnover, profit_pct, avg_rating, rank, commission, avg_price = row

    score = 0

    # 1. Активность продавцов (макс 20 очков)
    sellers_activity = (sellers_with_sales or 0) / (sellers or 1)
    if sellers_activity >= 0.7:
        score += 20
    elif sellers_activity >= 0.5:
        score += 12
    elif sellers_activity >= 0.3:
        score += 6
    else:
        score += 0

    # 2. Упущенная выручка (макс 20 очков)
    lost_pct = float(lost_revenue_pct or 0)
    if lost_pct >= 30:
        score += 20
    elif lost_pct >= 15:
        score += 14
    elif lost_pct >= 5:
        score += 8
    else:
        score += 2

    # 3. Средняя выручка на продавца (макс 20 очков)
    avg_rev = float(revenue or 0) / (sellers_with_sales or 1)
    if avg_rev >= 3_000_000:
        score += 20
    elif avg_rev >= 1_000_000:
        score += 14
    elif avg_rev >= 300_000:
        score += 8
    else:
        score += 2

    # 4. Процент выкупа (макс 15 очков)
    buyout = float(buyout_pct or 0)
    if buyout >= 0.85:
        score += 15
    elif buyout >= 0.70:
        score += 10
    elif buyout >= 0.50:
        score += 5
    else:
        score += 0

    # 5. Оборачиваемость (макс 10 очков) — чем меньше дней тем лучше
    turn = float(turnover or 0)
    if turn <= 30:
        score += 10
    elif turn <= 60:
        score += 7
    elif turn <= 90:
        score += 4
    elif turn <= 180:
        score += 1
    else:
        score += 0
    # Жёсткий штраф за оборачиваемость более 180 дней — не рекомендуем входить
    if turn > 180:
        score = min(score, 55)  # Максимум TEST, никогда не BUY

    # 6. Прибыльность ниши (макс 10 очков)
    profit = float(profit_pct or 0)
    if profit >= 0.35:
        score += 10
    elif profit >= 0.20:
        score += 7
    elif profit >= 0.10:
        score += 4
    else:
        score += 0

    # 7. Рейтинг товаров (макс 5 очков)
    rating = float(avg_rating or 0)
    if rating >= 4.7:
        score += 5
    elif rating >= 4.3:
        score += 3
    else:
        score += 1

    return min(score, 100)


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

    avg_price = float(revenue or 0) / (orders or 1)
    sellers_activity = (sellers_with_sales or 0) / (sellers or 1)

    if sellers > 500 and sellers_activity < 0.5:
        competition = "HIGH"
    elif sellers > 500 and sellers_activity >= 0.5:
        competition = "MEDIUM"
    elif sellers > 100:
        competition = "MEDIUM"
    else:
        competition = "LOW"

    metrics = MetricsSummary(
        monthly_revenue_estimate=float(revenue or 0),
        avg_orders_per_day=float(orders or 0) / 30,
        active_sellers=int(sellers or 0),
        competition_level=competition,
        median_price=float(avg_price),
        price_iqr=float(avg_price) * 0.3,
        top_20pct_revenue_share=0.6,
        top_10_revenue_share=float(sellers_activity),
    )

    loop = asyncio.new_event_loop()
    insights = loop.run_until_complete(analyze_niche(metrics))
    loop.close()
    return insights

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
.sidebar { width: 180px; background: #141418; border-right: 1px solid #2a2a3a; padding: 16px; flex-shrink: 0; }
.sidebar-label { font-size: 10px; color: #444; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 8px; }
.sidebar-item { display: flex; align-items: center; padding: 10px 12px; border-radius: 8px; cursor: pointer; font-size: 15px; color: #888; margin-bottom: 4px; transition: all 0.15s; }
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
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
</head>
<body>
<div class="header">
  <div>
    <div class="logo" onclick="goHome()" style="cursor:pointer;">WB<span>Analyzer</span></div>
    <div class="tagline">AI-платформа анализа товарных ниш</div>
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

  <div class="sidebar-item active" onclick="showTopNiches()">Топ ниши</div>
  <div class="sidebar-item" onclick="showCatalog()">Все ниши</div>
  <div class="sidebar-item" onclick="showCalc()">Калькулятор</div>
  <div class="sidebar-item" onclick="showHistory()">История</div>
  <div class="sidebar-item" id="watchlist-menu" onclick="showWatchlist()">📌 В работе <span id="watchlist-count" style="background:#6c63ff33;color:#a78bfa;border-radius:10px;padding:1px 7px;font-size:11px;margin-left:4px;"></span></div>
</div>
<div class="content-area">
<div class="main">
  <div class="search-box">
    <div class="search-row">
      <input class="search-input" id="query" autocomplete="off" placeholder="Введите нишу, например: платья, термосы, наушники..." />
      <button class="btn" onclick="analyze()">Анализировать</button>
    </div>
    <div class="examples" id="top-chips">
      <span style="font-size:11px;color:#555;align-self:center;">🔥 Топ ниши:</span>
    </div>
  </div>
  <div id="top-niches" style="display:none;margin-top:24px;"></div>
  <div id="watchlist" style="display:none;margin-top:24px;"></div><div id="catalog" style="display:none;margin-top:24px;">
    <div style="display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap;align-items:center;">
      <input id="cat-search" placeholder="Фильтр по названию..." style="background:#1a1a24;border:1px solid #2a2a3a;border-radius:8px;padding:10px 14px;color:#fff;font-size:13px;outline:none;flex:1;min-width:200px;" oninput="filterCatalog()"/>
      <select id="cat-sort" onchange="filterCatalog()" style="background:#1a1a24;border:1px solid #2a2a3a;border-radius:8px;padding:10px 14px;color:#888;font-size:13px;outline:none;">
        <option value="revenue">По выручке</option>
        <option value="orders">По заказам</option>
        <option value="profit">По прибыльности</option>
        <option value="buyout">По выкупу</option>
        <option value="turnover">По оборачиваемости</option>
      </select>
    </div>
    <div id="cat-chips" style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;"></div>
    <div style="display:none;">
    </div>
    <div id="cat-stats" style="font-size:12px;color:#555;margin-bottom:12px;"></div>
    <div id="cat-list"></div>
  </div><div id="history" style="display:none;margin-top:24px;">
    <div style="font-size:16px;font-weight:600;color:#fff;margin-bottom:20px;">История анализов</div>
    <div id="history-list"></div>
  </div><div class="calc-wrap" id="calculator">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;">
      <div style="font-size:16px;font-weight:600;color:#fff;">Калькулятор юнит-экономики</div>
      <div style="display:flex;gap:12px;align-items:center;">
        <div class="scheme-tabs">
          <button class="scheme-tab active" onclick="setScheme('fbo')">FBO</button>
          <button class="scheme-tab" onclick="setScheme('fbs')">FBS</button>
          <button class="scheme-tab" onclick="setScheme('china')">Китай</button>
        </div>
        <div class="scheme-tabs">
          <button class="scheme-tab active" id="calc-cur-rub" onclick="setCalcCurrency('rub')">₽</button>
          <button class="scheme-tab" id="calc-cur-usd" onclick="setCalcCurrency('usd')">$</button>
          <button class="scheme-tab" id="calc-cur-byn" onclick="setCalcCurrency('byn')">BYN</button>
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
    html+='<div style="font-size:13px;color:#555;margin-bottom:12px;">'+fmt(n.revenue)+'/мес</div>';
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

function setCurrency(cur) {
  currentCurrency = cur;
  document.querySelectorAll('.currency-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll(`.currency-btn[data-cur="${cur}"]`).forEach(b => b.classList.add('active'));
  if (window.lastResult) renderResult(window.lastResult);
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
async function showHistory() {
  hideAll();
  setActiveMenu(event.target);
  document.getElementById('history').style.display = 'block';
  document.getElementById('catalog').style.display = 'none';
  document.getElementById('result').style.display = 'none';
  document.getElementById('top-niches').style.display = 'none';
  if(document.getElementById('watchlist'))document.getElementById('watchlist').style.display='none';
  document.getElementById('calculator').style.display = 'none';
  document.getElementById('history').style.display = 'block';
  const r = await fetch('/history');
  const data = await r.json();
  if (data.error) {
    document.getElementById('history-list').innerHTML = '<div style="color:#555;padding:20px">Ошибка загрузки</div>';
    return;
  }
  if (data.length === 0) {
    document.getElementById('history-list').innerHTML = '<div style="color:#555;padding:20px">История пуста</div>';
    return;
  }
  document.getElementById('history-list').innerHTML = data.map(d => `
    <div onclick="setQuery('${d.category}')" style="background:#1a1a24;border:1px solid #2a2a3a;border-radius:10px;padding:16px;margin-bottom:8px;cursor:pointer;display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center;" onmouseover="this.style.borderColor='#6c63ff'" onmouseout="this.style.borderColor='#2a2a3a'">
      <div>
        <div style="font-size:15px;color:#fff;font-weight:500;margin-bottom:4px;">${d.category}</div>
        <div style="font-size:12px;color:#555;">${d.created_at} · выручка ${fmt(d.monthly_revenue)} · ${d.active_sellers} продавцов</div>
      </div>
      <div style="text-align:right;">
        <div class="verdict-badge verdict-${d.verdict}" style="padding:4px 12px;font-size:13px;">${d.verdict}</div>
        <div style="font-size:20px;color:#fff;font-weight:700;margin-top:4px;">${d.score}</div>
      </div>
    </div>
  `).join('');
}
function hideAll() {
  document.getElementById('catalog').style.display = 'none';
  document.getElementById('history').style.display = 'none';
  document.getElementById('calculator').style.display = 'none';
  document.getElementById('result').style.display = 'none';
  document.getElementById('top-niches').style.display = 'none';
  if(document.getElementById('watchlist'))document.getElementById('watchlist').style.display='none';
  document.querySelector('.search-box').style.display = 'block';
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
async function showTopNiches() {
  hideAll();
  setActiveMenu(event.target);
  document.getElementById('result').style.display = 'none';
  document.getElementById('top-niches').style.display = 'none';
  if(document.getElementById('watchlist'))document.getElementById('watchlist').style.display='none';
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
          <div style="font-size:15px;font-weight:600;color:#fff;margin-bottom:8px">${n.name.includes(' / ') ? n.name.split(' / ').slice(1).join(' / ') : n.name}</div>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="font-size:13px;color:#555">${fmt(n.revenue)}/мес</div>
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
  document.getElementById('result').style.display = 'none';
  document.getElementById('top-niches').style.display = 'none';
  if(document.getElementById('watchlist'))document.getElementById('watchlist').style.display='none';
  document.querySelector('.search-box').style.display = 'none';
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
          <span style="font-size:12px;color:#555;">${fmt(n.revenue)}/мес</span>
          <span style="font-size:12px;color:#555;">${n.sellers} продавцов</span>
          <span style="font-size:12px;color:#555;">выкуп ${Math.round(n.buyout_pct*100)}%</span>
          <span style="font-size:12px;color:#555;">прибыль ${Math.round(n.profit_pct*100)}%</span>
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
  const parts = name.split(' ');
  return parts.length > 1 ? parts.slice(1).join(' ') : name;
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
      window.priceChartInstance = new Chart(document.getElementById('priceChart'), {
        type: 'bar',
        data: {
          labels: data.price_labels,
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
              '#ec4899', '#f97316', '#fbbf24', '#4ade80',
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
      const colors = ['#ec4899','#f97316','#fbbf24','#4ade80','#38bdf8','#a78bfa','#fb7185','#34d399','#6b7280'];
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
        html += `<td style="padding:8px;color:#ddd;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${item.name}</td>`;
        html += `<td style="padding:8px;color:#888;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${item.seller}</td>`;
        html += `<td style="padding:8px;color:#fff;text-align:right;">${item.price.toLocaleString('ru')} ₽</td>`;
        html += `<td style="padding:8px;color:#38bdf8;text-align:right;">${item.revenue.toLocaleString('ru')} тыс ₽</td>`;
        html += `<td style="padding:8px;color:#4ade80;text-align:right;">${item.sales.toLocaleString('ru')}</td>`;
        html += `<td style="padding:8px;color:#fbbf24;text-align:right;">${item.rating > 0 ? '★ ' + item.rating : '—'}</td>`;
        html += `<td style="padding:8px;text-align:center;">${item.url ? '<a href="' + item.url + '" target="_blank" style="color:#6c63ff;text-decoration:none;font-size:11px;">' + item.id + '</a>' : '—'}</td>`;
        html += '</tr>';
      });
      html += '</table></div>';
      document.getElementById('topItemsContent').innerHTML = html;
    }

    // Блок рекламы
    if (document.getElementById('adContent') && data.avg_cpm !== undefined) {
      const cpmColors = { green: '#4ade80', yellow: '#fbbf24', red: '#ef4444' };
      const cpmEmoji = { green: '🟢', yellow: '🟡', red: '🔴' };
      let adHtml = `
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:16px;margin-bottom:16px;">
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
          <div style="background:#0f0f13;border-radius:8px;padding:12px;">
            <div style="font-size:10px;color:#555;margin-bottom:4px;letter-spacing:1px;">ТОП РЕКЛАМОДАТЕЛИ</div>
            ${data.top_ad_sellers.map(s => `<div style="font-size:11px;color:#aaa;margin-top:3px;">• ${s.name}</div>`).join('')}
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

      window.forecastChartInstance = new Chart(document.getElementById('forecastChart'), {
        type: 'line',
        data: {
          labels: allLabels,
          datasets: [
            {
              label: 'Факт (млн ₽)',
              data: allRevenue,
              borderColor: '#ffffff',
              backgroundColor: '#ffffff10',
              fill: true,
              tension: 0.4,
              pointRadius: 3,
              pointBackgroundColor: '#ffffff',
              borderWidth: 2
            },
            {
              label: 'Прогноз (млн ₽)',
              data: forecastFull,
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
    const doughnutColors = ['#ec4899','#f97316','#fbbf24','#4ade80','#38bdf8','#a78bfa','#fb7185','#34d399','#6b7280'];
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
}
loadTopChips();
loadCalcRates();

function setQuery(q) {
  const displayName = q.includes(" ") ? q.split(" ").slice(1).join(" ") : q;
  document.getElementById('query').value = displayName;
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
  showSuggestions(data);
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
  document.getElementById('loading').style.display = 'block';
  document.getElementById('result').style.display = 'none';
  document.getElementById('top-niches').style.display = 'none';
  if(document.getElementById('watchlist'))document.getElementById('watchlist').style.display='none';
  document.getElementById('error').style.display = 'none';
  try {
    const r = await fetch('/analyze?q=' + encodeURIComponent(q));
    const data = await r.json();
    document.getElementById('loading').style.display = 'none';
    if (data.error) {
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
  const verdictMap = {BUY: 'Рекомендуем входить', TEST: 'Тестовая закупка', SKIP: 'Не рекомендуем'};
  const insights = d.insights.map((t,i) => `<div class="insight-item"><div class="insight-num">${i+1}</div><div class="insight-text">${t}</div></div>`).join('');
  const hyps = d.hypotheses.map(t => `<div class="hyp-item">${t}</div>`).join('');
  document.getElementById('result').innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;">
      <div class="niche-name" style="margin-bottom:0;">${d.name} ${isSeasonal(d.name) ? '<span style="font-size:14px;color:#eab308;font-weight:400">🍂 сезонный товар</span>' : ''}</div>
      <button id="watchlist-btn" onclick="toggleWatchlist('${(d.full||d.name).replace(/'/g,'`')}','${d.name.replace(/'/g,'`')}',${d.revenue}); updateWatchlistBtn('${(d.full||d.name).replace(/'/g,'`')}');" style="background:#1a1a24;border:1px solid #2a2a3a;border-radius:8px;padding:8px 16px;color:#888;cursor:pointer;font-size:13px;white-space:nowrap;">${isInWatchlist(d.full||d.name) ? '📌 В работе' : '🔖 В работе'}</button>
    </div>

    <!-- ЗОНА 1: Метрики -->
    <div class="metrics-grid">
      <div class="metric-card"><div class="metric-label">Выручка ниши</div><div class="metric-value">${fmtCurrency(d.revenue)}</div><div class="metric-sub">в месяц<div class="currency-switch"><button class="currency-btn ${currentCurrency==='rub'?'active':''}" data-cur="rub" onclick="setCurrency('rub')">₽</button><button class="currency-btn ${currentCurrency==='usd'?'active':''}" data-cur="usd" onclick="setCurrency('usd')">$</button><button class="currency-btn ${currentCurrency==='eur'?'active':''}" data-cur="eur" onclick="setCurrency('eur')">€</button><button class="currency-btn ${currentCurrency==='byn'?'active':''}" data-cur="byn" onclick="setCurrency('byn')">Br</button></div></div></div>
      <div class="metric-card"><div class="metric-label">Заказов в месяц</div><div class="metric-value">${d.orders.toLocaleString('ru')}</div><div class="metric-sub">${(d.orders/30).toFixed(0)} в день</div></div>
      <div class="metric-card"><div class="metric-label">Продавцов</div><div class="metric-value">${d.sellers.toLocaleString('ru')}</div><div class="metric-sub">${d.sellers_with_sales} с продажами</div></div>
      <div class="metric-card"><div class="metric-label">Выкуп</div><div class="metric-value">${(d.buyout_pct*100).toFixed(0)}%</div><div class="metric-sub">${d.buyout_pct >= 0.8 ? 'отличный' : d.buyout_pct >= 0.6 ? 'хороший' : 'низкий'}</div></div>
      <div class="metric-card"><div class="metric-label">Оборачиваемость (реальная)</div><div class="metric-value">${(() => { const real = d.buyout_pct > 0 ? Math.round(d.turnover / d.buyout_pct) : Math.round(d.turnover); return real > 365 ? "365+" : real; })()} дн</div><div class="metric-sub">${(() => { const real = d.buyout_pct > 0 ? Math.round(d.turnover / d.buyout_pct) : Math.round(d.turnover); return real <= 45 ? '<span class="turn-fast">🟢 быстро</span>' : real <= 90 ? '<span class="turn-seasonal">🟡 умеренно</span>' : '<span class="turn-slow">🔴 медленно</span>'; })()} <span style="font-size:10px;color:#444;">MPStats: ${Math.round(d.turnover)} дн</span></div></div>
      <div class="metric-card"><div class="metric-label">Маржинальность</div><div class="metric-value">${(d.profit_pct*100).toFixed(0)}%</div><div class="metric-sub">${d.profit_pct >= 0.35 ? 'высокая' : d.profit_pct >= 0.2 ? 'средняя' : 'низкая'} <span style="font-size:10px;color:#444;">до себест.</span></div></div>
    </div>

    <!-- ЗОНА 2: Вердикт со score индикатором -->
    <div class="verdict-card" style="display:grid;grid-template-columns:auto auto 1fr auto;align-items:center;gap:20px;">
      <div class="verdict-badge verdict-${d.verdict}" style="font-size:18px;padding:10px 20px;">${d.verdict}</div>
      <div class="verdict-text"><div class="verdict-title">${verdictMap[d.verdict]}</div><div class="verdict-desc">${d.analysis}</div></div>
      <div></div>
      <svg viewBox="0 0 100 100" style="width:80px;height:80px;flex-shrink:0;">
        <circle cx="50" cy="50" r="40" fill="none" stroke="#1f1f2e" stroke-width="10"/>
        <circle cx="50" cy="50" r="40" fill="none" stroke="${d.score>=65?'#22c55e':d.score>=40?'#eab308':'#ef4444'}" stroke-width="10"
          stroke-dasharray="${d.score * 2.51} 251" stroke-dashoffset="62.75"
          stroke-linecap="round" transform="rotate(-90 50 50)"/>
        <text x="50" y="46" text-anchor="middle" fill="#fff" font-size="18" font-weight="700">${d.score}</text>
        <text x="50" y="60" text-anchor="middle" fill="#555" font-size="10">из 100</text>
      </svg>
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
          <div class="metric-row"><div class="metric-row-label">Средняя цена</div><div class="metric-row-value">${Math.round(d.avg_price).toLocaleString('ru')} ₽</div></div>
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
    <div class="chart-card" style="display:flex;flex-direction:column;height:320px;" onclick="openChartModal('🏆 Топ продавцы ниши', 'doughnut', window._chartData.seller_labels, window._chartData.seller_pct || window._chartData.seller_data, '#ec4899', false)">
      <div class="chart-title">🏆 Топ продавцы ниши (доля рынка) <span style="font-size:10px;color:#555;">🔍</span></div>
      <div style="display:flex;gap:8px;align-items:center;height:260px;">
        <div style="height:250px;width:250px;position:relative;flex-shrink:0;"><canvas id="sellersChart"></canvas></div>
        <div id="sellersStats" style="width:260px;flex-shrink:0;margin-left:auto;"></div>
      </div>
    </div>

    <!-- ЗОНА 4г: Прогноз продаж -->
    <div class="chart-card" style="height:320px;margin-bottom:16px;" onclick="openChartModal('🔮 Прогноз выручки на 3 месяца', 'forecast', window._chartData.labels, window._chartData.revenue, '#38bdf8', false)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
        <div class="chart-title" style="margin:0;">🔮 Прогноз выручки на 3 месяца <span style="font-size:10px;color:#555;">(млн ₽) 🔍</span></div>
        <div style="font-size:11px;color:#555;">на основе сезонности + тренда</div>
      </div>
      <canvas id="forecastChart" height="110"></canvas>
    </div>
    </div>

    <!-- ЗОНА 4д: Реклама WB -->
    <div class="chart-card" id="adBlock" style="margin-bottom:16px;">
      <div class="chart-title">📢 Анализ рекламы WB</div>
      <div id="adContent" style="margin-top:12px;"></div>
    </div>

    <!-- ЗОНА 4е: Топ товаров ниши -->
    <div class="chart-card" id="topItemsBlock" style="margin-bottom:16px;">
      <div class="chart-title">🏅 Топ товаров ниши <span style="font-size:10px;color:#555;">по выручке за период</span></div>
      <div id="topItemsContent" style="margin-top:12px;"></div>
    </div>

    <!-- ЗОНА 5: AI Инсайты -->
    <div class="ai-card">
      <div class="ai-header"><div class="ai-dot"></div><div class="ai-title">AI Инсайты</div></div>
      <div class="section-title">Ключевые наблюдения</div>
      ${insights}
      ${hyps ? `<div class="section-title" style="margin-top:20px">Гипотезы для проверки</div>${hyps}` : ''}
    </div>
  `;
  document.getElementById('result').style.display = 'block';
  loadCharts(d.name);
  addToRecent(d.name);
}
</script>
</div>
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

        elif self.path == '/history':
            try:
                conn = psycopg2.connect(DB)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT category, score, verdict, monthly_revenue,
                           active_sellers, created_at
                    FROM product_decisions
                    ORDER BY created_at DESC
                    LIMIT 50
                """)
                rows = cursor.fetchall()
                cursor.close()
                conn.close()
                history = []
                for r in rows:
                    history.append({
                        'category': r[0],
                        'score': r[1],
                        'verdict': r[2],
                        'monthly_revenue': float(r[3] or 0),
                        'active_sellers': int(r[4] or 0),
                        'created_at': r[5].strftime('%d.%m.%Y %H:%M') if r[5] else '',
                    })
                self.send_response(200)
                self.send_header('Content-type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(history, ensure_ascii=False).encode('utf-8'))
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

        elif self.path.startswith('/charts'):
            from urllib.parse import unquote
            query = self.path.split('?q=')[-1]
            query = unquote(query)
            try:
                from datetime import date, timedelta
                conn = psycopg2.connect(DB)
                cur = conn.cursor()
                cur.execute("SELECT mpstats_path FROM niches WHERE name ILIKE %s ORDER BY CASE WHEN LOWER(name)=LOWER(%s) THEN 0 ELSE 1 END LIMIT 1", (f'%{query}%', query))
                row = cur.fetchone()
                conn.close()
                
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
                    
                    # Ценовые сегменты
                    price_segments = {'до 500': 0, '500-1000': 0, '1000-2000': 0, '2000-5000': 0, '5000-10000': 0, 'свыше 10000': 0}
                    sellers_revenue = {}
                    for item in items:
                        price = item.get('final_price', 0) or 0
                        if price < 500: price_segments['до 500'] += 1
                        elif price < 1000: price_segments['500-1000'] += 1
                        elif price < 2000: price_segments['1000-2000'] += 1
                        elif price < 5000: price_segments['2000-5000'] += 1
                        elif price < 10000: price_segments['5000-10000'] += 1
                        else: price_segments['свыше 10000'] += 1
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
                    others_revenue = max(0, total_niche_revenue - top8_revenue)
                    seller_pct = []
                    if total_niche_revenue > 0:
                        for s in top_sellers:
                            seller_pct.append(round(s[1] / total_niche_revenue * 100, 1))
                        seller_pct.append(round(others_revenue / total_niche_revenue * 100, 1))
                    else:
                        seller_pct = [0] * (len(top_sellers) + 1)

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
                        'top_items': top_items
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
                row = find_niche(query)
                if not row:
                    result = {'error': f'Ниша "{query}" не найдена. Попробуйте: Платья, Куртки, Пижамы, Конфеты'}
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
                            insights.analysis,
                        ))
                        conn2.commit()
                        cur2.close()
                        conn2.close()
                    except Exception as log_err:
                        logger.warning("Логирование не удалось: %s", log_err)

                    result = {
                        'name': clean_name(name),
                        'category': get_category(name),
                        'revenue': float(revenue or 0),
                        'orders': int(orders or 0),
                        'sellers': int(sellers or 0),
                        'sellers_with_sales': int(sellers_with_sales or 0),
                        'score': score,
                        'verdict': verdict,
                        'insights': insights.insights,
                        'hypotheses': insights.hypotheses,
                        'analysis': insights.analysis,
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

if __name__ == '__main__':
    print("🚀 Сервер запущен: http://localhost:8080")
    HTTPServer(('', 8080), Handler).serve_forever()