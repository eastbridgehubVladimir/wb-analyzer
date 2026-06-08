"""
pdf_auto.py — Автогенерация PDF-отчётов WBAnalyzer.

При вызове generate(level, niche, chart_data):
  1. Запускает нужных AI-агентов через Claude API напрямую
  2. Собирает метрики из MPStats (через get_mpstats_cached)
  3. Генерирует красивый PDF с помощью ReportLab
  4. Возвращает bytes

Уровни:
  basic    — метрики + топ-5 + мастер-анализ + upsell
  standard — всё из basic + юнит-экономика + реклама + топ-20
  deep     — всё из standard + поставщики + склад + контент + документы + глубокий анализ
"""
import os, sys, json, re, io, time, subprocess

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_ROOT, 'backend'))

# ── Авто-установка зависимостей ────────────────────────────────────────────────
def _ensure_deps():
    pkgs = []
    try:
        import reportlab  # noqa
    except ImportError:
        pkgs.append('reportlab==4.2.5')
    try:
        import matplotlib  # noqa
    except ImportError:
        pkgs.append('matplotlib==3.9.4')
    if pkgs:
        print(f'[pdf_auto] Installing: {pkgs}')
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q'] + pkgs)
        import importlib as _il
        _il.invalidate_caches()
        print('[pdf_auto] Install done')

_ensure_deps()

# ── ReportLab ──────────────────────────────────────────────────────────────────
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch, mm
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph,
    Spacer, HRFlowable, PageBreak, KeepTogether, Image,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics.shapes import Drawing, Rect, String, Line
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics import renderPDF

# ── Шрифты ────────────────────────────────────────────────────────────────────
FN, FB = 'Helvetica', 'Helvetica-Bold'
try:
    import matplotlib as _mpl
    _fd = os.path.join(os.path.dirname(_mpl.__file__), 'mpl-data', 'fonts', 'ttf')
    pdfmetrics.registerFont(TTFont('DV',  os.path.join(_fd, 'DejaVuSans.ttf')))
    pdfmetrics.registerFont(TTFont('DVB', os.path.join(_fd, 'DejaVuSans-Bold.ttf')))
    FN, FB = 'DV', 'DVB'
except Exception:
    pass

# ── Цвета ─────────────────────────────────────────────────────────────────────
C_NAVY   = HexColor('#0d1b2a')
C_BLUE   = HexColor('#1d4ed8')
C_BLUE2  = HexColor('#3b82f6')
C_GREEN  = HexColor('#16a34a')
C_RED    = HexColor('#dc2626')
C_AMBER  = HexColor('#d97706')
C_LIGHT  = HexColor('#f1f5f9')
C_LIGHT2 = HexColor('#e2e8f0')
C_GRAY   = HexColor('#64748b')
C_TEXT   = HexColor('#1e293b')
WHITE    = colors.white

W, H = A4  # 595, 842 points
MARGIN = 0.55 * inch
COL_W  = W - 2 * MARGIN  # usable width ~6.5 inch

# ── Форматирование чисел ──────────────────────────────────────────────────────

def _rub(v):
    v = float(v or 0)
    if v >= 1_000_000_000:
        return f'{v/1_000_000_000:.1f} млрд ₽'
    if v >= 1_000_000:
        return f'{v/1_000_000:.1f} млн ₽'
    if v >= 1_000:
        return f'{v/1_000:.0f} тыс ₽'
    return f'{int(v):,} ₽'.replace(',', ' ')

def _pct(v):
    v = float(v or 0)
    return f'{v*100:.0f}%' if v <= 1 else f'{v:.0f}%'

def _num(v):
    try:
        return f'{int(float(v)):,}'.replace(',', ' ')
    except Exception:
        return str(v)


# ── Строители ReportLab-элементов ─────────────────────────────────────────────

def _style(name='body', size=9, bold=False, align=TA_LEFT, color=C_TEXT,
           space_before=2, space_after=2, leading=None):
    return ParagraphStyle(
        name,
        fontName=FB if bold else FN,
        fontSize=size,
        textColor=color,
        alignment=align,
        spaceBefore=space_before,
        spaceAfter=space_after,
        leading=leading or (size * 1.4),
    )

def _p(text, **kw):
    return Paragraph(str(text), _style(**kw))

def _h1(text):
    return _p(text, name='h1', size=18, bold=True, color=C_NAVY,
              space_before=6, space_after=4)

def _h2(text):
    return _p(text, name='h2', size=13, bold=True, color=C_NAVY,
              space_before=10, space_after=4)

def _h3(text):
    return _p(text, name='h3', size=10, bold=True, color=C_BLUE,
              space_before=6, space_after=2)

def _body(text):
    return _p(text, size=9, space_before=2, space_after=2)

def _bullet(text):
    return _p(f'• {text}', size=9, space_before=2, space_after=2)

def _sp(h=0.1):
    return Spacer(1, h * inch)

def _hr():
    return HRFlowable(width='100%', thickness=1, color=C_LIGHT2,
                      spaceAfter=4, spaceBefore=2)

def _tbl(rows, col_widths=None, header_bg=C_NAVY, row_bg=True):
    if not rows:
        return _sp(0.05)
    n_cols = max(len(r) for r in rows)
    if col_widths is None:
        col_widths = [COL_W / n_cols] * n_cols
    t = Table(rows, colWidths=col_widths)
    cmds = [
        ('BACKGROUND', (0, 0), (-1, 0), header_bg),
        ('TEXTCOLOR',  (0, 0), (-1, 0), WHITE),
        ('FONTNAME',   (0, 0), (-1, 0), FB),
        ('FONTSIZE',   (0, 0), (-1, -1), 8),
        ('FONTNAME',   (0, 1), (-1, -1), FN),
        ('GRID',       (0, 0), (-1, -1), 0.4, C_LIGHT2),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('VALIGN',     (0, 0), (-1, -1), 'MIDDLE'),
    ]
    if row_bg:
        cmds.append(('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, C_LIGHT]))
    t.setStyle(TableStyle(cmds))
    return t


# ── Claude API ────────────────────────────────────────────────────────────────

def _claude(prompt: str, max_tokens: int = 2000) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY', ''))
    msg = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=max_tokens,
        messages=[{'role': 'user', 'content': prompt}],
    )
    return msg.content[0].text.strip()

def _json(text: str) -> dict:
    t = text.replace('```json', '').replace('```', '').strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r'\{.*\}', t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
        return {}


# ── Агенты (прямые вызовы Claude) ─────────────────────────────────────────────

def _run_master(n: dict) -> dict:
    name = n.get('name', '')
    revenue = float(n.get('revenue', 0))
    avg_price = float(n.get('avg_price', 0))
    profit_pct = float(n.get('profit_pct', 0))
    buyout_pct = float(n.get('buyout_pct', 0))
    turnover = float(n.get('turnover', 0))
    sellers = int(n.get('sellers', 0))
    sws = int(n.get('sellers_with_sales', 0))
    act = round(sws / sellers * 100) if sellers else 0
    avg_rev = round(revenue / sws) if sws else 0

    prompt = (
        f"Ты старший аналитик WB. Сделай полный анализ ниши.\n\n"
        f"НИША: {name}\n"
        f"Выручка: {revenue:,.0f} ₽/мес | Средняя цена: {avg_price:,.0f} ₽\n"
        f"Маржа: {profit_pct*100:.0f}% | Выкуп: {buyout_pct*100:.0f}%\n"
        f"Оборачиваемость: {turnover:.0f} дней | Продавцов: {sellers} (активных: {sws}, {act}%)\n"
        f"Средняя выручка/продавец: {avg_rev:,.0f} ₽/мес\n\n"
        "Ответь ONLY JSON:\n"
        '{"final_verdict":"ВХОДИТЬ|ТЕСТИРОВАТЬ|НЕ ВХОДИТЬ",'
        '"verdict_color":"#16a34a|#d97706|#dc2626",'
        '"confidence":"высокая|средняя|низкая",'
        '"market_analysis":"3-4 предложения",'
        '"competitive_landscape":"2-3 предложения",'
        '"entry_strategy":"2-3 предложения",'
        '"financial_model":{"test_batch_units":0,"test_batch_cost":0,'
        '"monthly_ad_budget":0,"breakeven_units":0,"roi_3months":"X%","payback_months":0},'
        '"seasonal_plan":{"peak":"месяцы","low":"месяцы","buy_date":"дата","ad_date":"дата"},'
        '"opportunities":["возможность 1","возможность 2","возможность 3"],'
        '"risks":[{"risk":"риск","probability":"средняя","mitigation":"решение"}],'
        '"final_recommendation":"подробная рекомендация 3-4 предложения"}'
    )
    return _json(_claude(prompt, 2500))


def _run_deep(n: dict) -> dict:
    name = n.get('name', '')
    revenue = float(n.get('revenue', 0))
    avg_price = float(n.get('avg_price', 0))
    commission = float(n.get('commission', 0))
    buyout_pct = float(n.get('buyout_pct', 0))
    profit_pct = float(n.get('profit_pct', 0))
    turnover = float(n.get('turnover', 0))
    sellers = int(n.get('sellers', 0))
    sws = int(n.get('sellers_with_sales', 0))
    rt = round(turnover / buyout_pct) if buyout_pct > 0 else round(turnover)
    avg_rev = revenue / sws if sws else 0

    prompt = (
        f"Ты эксперт по торговле на WB. Глубокий анализ ниши.\n\n"
        f"Ниша: {name} | Выручка: {revenue:,.0f} ₽ | Цена: {avg_price:,.0f} ₽\n"
        f"Продавцов: {sellers}, активных: {sws} | Комиссия: {commission:.0f}%\n"
        f"Выкуп: {buyout_pct*100:.0f}% | Оборачиваемость реальная: {rt} дней\n"
        f"Маржа: {profit_pct*100:.0f}% | Средняя выручка/продавец: {avg_rev:,.0f} ₽/мес\n\n"
        "ONLY JSON:\n"
        '{"verdict":"ВХОДИТЬ|ТЕСТИРОВАТЬ|НЕ ВХОДИТЬ",'
        '"verdict_desc":"обоснование 1-2 предложения",'
        '"entry_budget":0,"ad_budget":0,"breakeven":0,"roi_forecast":"X-Y%",'
        '"financial_plan":"2-3 предложения",'
        '"competitive_analysis":"2-3 предложения",'
        '"free_segments":"свободные сегменты",'
        '"recommendation":"2-3 предложения",'
        '"season_peak_months":"месяцы пика","season_low_months":"месяцы спада",'
        '"purchase_months":"когда закупать","season_tip":"совет по сезонности"}'
    )
    return _json(_claude(prompt, 1200))


def _run_unit(n: dict) -> dict:
    avg_price = float(n.get('avg_price', 0))
    buyout_pct = float(n.get('buyout_pct', 0.7))
    commission = float(n.get('commission', 0.25))
    name = n.get('name', '')
    if avg_price < 100:
        return {}

    cost_rub = avg_price * 0.35
    comm_pct = commission * 100 if commission <= 1 else commission
    wb_comm = avg_price * (comm_pct / 100)

    vols = [(20, 15, 10)]  # default dimensions
    wb_log = 120

    tax = avg_price * 0.06
    ret  = wb_log * (1 - buyout_pct) * 0.5
    stor = avg_price * 0.02

    def _sc(cost_mult=1.0, log_mult=1.0, label=''):
        tc = cost_rub * cost_mult + stor
        wbc = wb_comm + wb_log * log_mult + ret + tax
        pr = avg_price - tc - wbc
        roi = round(pr / cost_rub * 100, 1) if cost_rub else 0
        mar = round(pr / avg_price * 100, 1) if avg_price else 0
        vrd = 'profit' if pr > avg_price * 0.15 else ('marginal' if pr > 0 else 'loss')
        return {'title': label, 'total_cost_rub': round(tc),
                'wb_commission_rub': round(wb_comm), 'wb_logistics_rub': round(wb_log * log_mult),
                'profit_per_unit_rub': round(pr), 'roi_pct': roi, 'margin_pct': mar, 'verdict': vrd}

    scenarios = {
        's1': _sc(1.0, 1.0, '🇧🇾 Китай → WB Беларусь (FBO)'),
        's2': _sc(1.1, 1.3, '🏭 Китай → склад РБ → WB (FBS)'),
        's3': _sc(1.15, 1.0, '🇷🇺 Китай → WB Россия (FBO)'),
    }

    best = max(scenarios.values(), key=lambda s: s['profit_per_unit_rub'])
    prompt = (
        f"Ниша WB: {name}. Цена: {avg_price:.0f} ₽. "
        f"Расчётная маржа лучшего сценария: {best['margin_pct']:.1f}%, ROI: {best['roi_pct']:.1f}%.\n"
        "Дай краткую рекомендацию ONLY JSON:\n"
        '{"title":"заголовок","detail":"2-3 предложения с цифрами"}'
    )
    rec = _json(_claude(prompt, 300))
    return {'scenarios': scenarios, 'recommendation': rec}


def _run_ads(n: dict) -> dict:
    name = n.get('name', '')
    avg_price = float(n.get('avg_price', 0))
    revenue = float(n.get('revenue', 0))
    profit_pct = float(n.get('profit_pct', 0))
    buyout_pct = float(n.get('buyout_pct', 0))
    commission = float(n.get('commission', 0))

    prompt = (
        f"Ты рекламный аналитик WB.\n"
        f"НИША: {name} | Цена: {avg_price} ₽ | Выручка: {revenue:,.0f} ₽\n"
        f"Маржа: {profit_pct*100:.1f}% | Выкуп: {buyout_pct*100:.1f}% | Комиссия: {commission*100:.1f}%\n\n"
        "ONLY JSON:\n"
        '{"load_level":"low|medium|high",'
        '"load_analysis":"3-4 предложения",'
        '"strategy_type":"название",'
        '"strategy_detail":"3-4 предложения",'
        '"strategy_steps":["Шаг 1","Шаг 2","Шаг 3","Шаг 4","Шаг 5"],'
        '"budget":{"start_rub":0,"growth_rub":0,"sustain_rub":0,"comment":"логика"},'
        '"cpm_forecast":{"start_rub":0,"month2_rub":0,"comment":"прогноз"},'
        '"forecast":{"month1":{"metrics":["CTR: X%","CR: X%","DRR: X%","Pos: X","Orders: X"]},'
        '"month2":{"metrics":["CTR: X%","CR: X%","DRR: X%","Pos: X","Orders: X"]}}}'
    )
    return _json(_claude(prompt, 1500))


def _run_supplier(n: dict) -> dict:
    name = n.get('name', '')
    avg_price = float(n.get('avg_price', 0))
    avg_usd = round(avg_price / 90, 1)

    prompt = (
        f"Ты эксперт по закупкам в Китае.\n"
        f"НИША: {name} | Цена WB: {avg_price} ₽ (${avg_usd})\n"
        "Комиссия WB ~25%, логистика ~120 ₽/шт.\n\n"
        "ONLY JSON:\n"
        '{"price_taobao_usd":0,"price_alibaba_usd":0,"moq":0,'
        '"summary":"3-4 предложения о поставщиках и ценах",'
        '"search_links":['
        '{"platform":"1688","url":"https://s.1688.com/selloffer/offer_search.htm?keywords=QUERY_CN","description":"самые низкие оптовые цены"},'
        '{"platform":"Alibaba","url":"https://www.alibaba.com/trade/search?SearchText=QUERY_EN","description":"оптовые поставщики"},'
        '{"platform":"Pinduoduo","url":"https://mobile.yangkeduo.com/search_result.html?search_key=QUERY_CN","description":"групповые закупки"}],'
        '"real_margin_pct":0,"roi_pct":0,"profit_per_unit_rub":0}'
    )
    return _json(_claude(prompt, 1000))


def _run_docs(n: dict) -> dict:
    name = n.get('name', '')
    avg_price = float(n.get('avg_price', 0))

    prompt = (
        f"Ты эксперт по сертификации для WB. Компания из Беларуси, закупки в Китае.\n"
        f"НИША: {name} | Средняя цена: {avg_price:.0f} ₽\n\n"
        "ONLY JSON:\n"
        '{"complexity":"low|medium|high",'
        '"wb_docs":[{"name":"документ","description":"зачем нужен","cost_rub":0,"duration_days":0,"required":true}],'
        '"customs_docs":["документ 1","документ 2"],'
        '"risks":[{"risk":"риск","solution":"как избежать"}],'
        '"total_cost_rub":0,"total_duration_days":0}'
    )
    return _json(_claude(prompt, 1500))


def _run_warehouse(n: dict) -> dict:
    name = n.get('name', '')
    avg_price = float(n.get('avg_price', 0))
    revenue = float(n.get('revenue', 0))
    turnover = float(n.get('turnover', 0))
    buyout_pct = float(n.get('buyout_pct', 0))
    profit_pct = float(n.get('profit_pct', 0))
    commission = float(n.get('commission', 0))

    prompt = (
        "Ты эксперт по логистике WB. Компания из Беларуси.\n"
        "Склады-приоритеты: Смоленск, Коледино, Подольск, Электросталь.\n\n"
        f"НИША: {name} | Цена: {avg_price} ₽ | Выручка: {revenue:,.0f} ₽\n"
        f"Оборачиваемость: {turnover:.0f} дней | Выкуп: {buyout_pct*100:.1f}% | Маржа: {profit_pct*100:.1f}%\n\n"
        "ONLY JSON:\n"
        '{"model":"FBS|FBO|Смешанная FBS+FBO",'
        '"model_color":"fbs|fbo|mixed",'
        '"model_detail":"3-4 предложения с цифрами",'
        '"warehouse_tips":["совет 1","совет 2","совет 3","совет 4"],'
        '"stock":{"min_units":0,"opt_units":0,"max_units":0,"min_rub":0,"opt_rub":0,"max_rub":0,"comment":"логика"},'
        '"risks":["риск 1","риск 2","риск 3"]}'
    )
    return _json(_claude(prompt, 1200))


def _run_content(n: dict) -> str:
    name = n.get('name', '')
    avg_price = float(n.get('avg_price', 0))
    prompt = (
        f"Создай контент для карточки товара WB в нише «{name}».\n"
        f"Средняя цена: {avg_price:.0f} ₽\n\n"
        "1. SEO ЗАГОЛОВОК (до 100 символов)\n"
        "2. ОПИСАНИЕ ТОВАРА (500-600 символов)\n"
        "3. БУЛЛЕТЫ — 5 преимуществ с эмодзи\n"
        "4. КЛЮЧЕВЫЕ ХАРАКТЕРИСТИКИ — 5 полей\n"
        "5. РЕКОМЕНДАЦИИ ПО ФОТО\n"
        "6. СОВЕТ ПО ВИДЕО\n\n"
        "Отвечай структурированно по пунктам на русском языке."
    )
    return _claude(prompt, 1500)


# ── Генерация графиков (ReportLab drawing) ────────────────────────────────────

def _chart_price_bar(items: list, width=COL_W, height=2.5*inch) -> Drawing:
    """Распределение цен — вертикальные бары."""
    if not items:
        return Drawing(width, height)

    prices = [float(i.get('price') or i.get('final_price') or 0) for i in items if i.get('price') or i.get('final_price')]
    if not prices:
        return Drawing(width, height)

    prices = [p for p in prices if p > 0]
    if not prices:
        return Drawing(width, height)

    mn, mx = min(prices), max(prices)
    if mx == mn:
        mx = mn + 1

    n_bins = 8
    step = (mx - mn) / n_bins
    bins = [0] * n_bins
    for p in prices:
        idx = min(int((p - mn) / step), n_bins - 1)
        bins[idx] += 1

    labels = [f'{int(mn + i*step)}-{int(mn + (i+1)*step)}' for i in range(n_bins)]

    d = Drawing(width, height)
    chart = VerticalBarChart()
    chart.x = 50
    chart.y = 40
    chart.width = width - 70
    chart.height = height - 60
    chart.data = [bins]
    chart.bars[0].fillColor = C_BLUE2
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = max(bins) + 1
    chart.valueAxis.labelTextFormat = '%d'
    chart.categoryAxis.categoryNames = [f'{int(mn + i*step/1000):.0f}k' if mn + i*step > 1000 else str(int(mn + i*step)) for i in range(n_bins)]
    chart.categoryAxis.labels.angle = 30
    chart.categoryAxis.labels.fontSize = 6
    d.add(chart)
    # Title
    from reportlab.graphics.shapes import String
    d.add(String(width/2, height - 12, 'Распределение цен (₽)', fontSize=9,
                 fontName=FB, fillColor=C_TEXT, textAnchor='middle'))
    return d


def _chart_sellers_pie(items: list, width=3*inch, height=3*inch) -> Drawing:
    """Доля топ-5 продавцов (pie)."""
    if not items:
        return Drawing(width, height)

    # group by brand/seller
    seller_rev = {}
    for item in items:
        seller = str(item.get('brand') or item.get('supplier') or 'Прочие')
        rev = float(item.get('revenue') or 0)
        seller_rev[seller] = seller_rev.get(seller, 0) + rev

    if not seller_rev:
        return Drawing(width, height)

    sorted_sellers = sorted(seller_rev.items(), key=lambda x: x[1], reverse=True)
    top5 = sorted_sellers[:5]
    others = sum(v for _, v in sorted_sellers[5:])
    if others > 0:
        top5.append(('Остальные', others))

    total = sum(v for _, v in top5)
    if total == 0:
        return Drawing(width, height)

    d = Drawing(width, height)
    pie = Pie()
    pie.x = 20
    pie.y = 30
    pie.width = width - 100
    pie.height = height - 60
    pie.data = [v for _, v in top5]
    pie.labels = [f'{k[:12]}\n{v/total*100:.0f}%' for k, v in top5]

    pie_colors = [C_BLUE2, C_GREEN, C_AMBER, C_RED, C_GRAY, HexColor('#7c3aed')]
    for i, c in enumerate(pie_colors[:len(top5)]):
        pie.slices[i].fillColor = c
        pie.slices[i].strokeColor = WHITE
        pie.slices[i].strokeWidth = 1

    pie.sideLabels = True
    pie.sideLabelsOffset = 0.05
    d.add(pie)
    d.add(String(width/2, height - 12, 'Доля продавцов', fontSize=9,
                 fontName=FB, fillColor=C_TEXT, textAnchor='middle'))
    return d


def _chart_revenue_line(items: list, width=COL_W, height=2.5*inch) -> Drawing:
    """Топ товары по выручке — горизонтальная линия."""
    if not items:
        return Drawing(width, height)

    top = sorted(items, key=lambda x: float(x.get('revenue') or 0), reverse=True)[:12]
    revenues = [float(i.get('revenue') or 0) for i in top]
    if not revenues or max(revenues) == 0:
        return Drawing(width, height)

    d = Drawing(width, height)
    chart = HorizontalLineChart()
    chart.x = 55
    chart.y = 25
    chart.width = width - 75
    chart.height = height - 45
    chart.data = [revenues]
    chart.lines[0].strokeColor = C_BLUE2
    chart.lines[0].strokeWidth = 2
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = max(revenues) * 1.1
    chart.valueAxis.labelTextFormat = lambda v: f'{v/1e6:.1f}M' if v >= 1e6 else f'{v/1e3:.0f}k'
    chart.categoryAxis.categoryNames = [str(i+1) for i in range(len(revenues))]
    d.add(chart)
    d.add(String(width/2, height - 12, 'Выручка топ товаров (₽)',
                 fontSize=9, fontName=FB, fillColor=C_TEXT, textAnchor='middle'))
    return d


# ── PDF секции ─────────────────────────────────────────────────────────────────

def _sec_cover(niche: dict, level: str) -> list:
    name = niche.get('name') or niche.get('display_name') or 'Анализ ниши'
    level_names = {'basic': 'Basic', 'standard': 'Standard', 'deep': 'Deep'}
    level_colors = {'basic': C_GRAY, 'standard': C_BLUE2, 'deep': HexColor('#7c3aed')}
    lname = level_names.get(level, level.capitalize())
    lcol  = level_colors.get(level, C_BLUE2)

    from reportlab.platypus import Frame, BaseDocTemplate
    els = []
    # Logo / brand
    els.append(_p('WBAnalyzer', name='logo', size=24, bold=True, color=C_NAVY,
                  align=TA_CENTER, space_before=20, space_after=4))
    els.append(_p('Аналитическая платформа для продавцов Wildberries',
                  name='tagline', size=10, color=C_GRAY, align=TA_CENTER,
                  space_before=0, space_after=20))
    els.append(_hr())
    els.append(_sp(0.3))
    els.append(_p(name, name='niche_title', size=22, bold=True, color=C_NAVY,
                  align=TA_CENTER, space_before=8, space_after=8))

    # Level badge via table
    badge = Table([[_p(f'PDF {lname}', size=12, bold=True, color=WHITE, align=TA_CENTER)]],
                  colWidths=[1.5*inch])
    badge.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), lcol),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING', (0,0), (-1,-1), 10),
        ('RIGHTPADDING', (0,0), (-1,-1), 10),
    ]))
    from reportlab.platypus import HRFlowable
    els.append(Table([[badge]], colWidths=[COL_W]))
    els.append(_sp(0.2))

    from datetime import date
    els.append(_p(f'Дата: {date.today().strftime("%d.%m.%Y")}',
                  size=9, color=C_GRAY, align=TA_CENTER))
    els.append(_sp(0.5))
    els.append(_hr())
    return els


def _sec_metrics(niche: dict) -> list:
    n = niche
    revenue = float(n.get('revenue', 0))
    orders = int(n.get('orders', 0))
    sellers = int(n.get('sellers', 0))
    sws = int(n.get('sellers_with_sales', 0))
    buyout = float(n.get('buyout_pct', 0))
    profit = float(n.get('profit_pct', 0))
    turnover = float(n.get('turnover', 0))
    avg_price = float(n.get('avg_price', 0))
    lost_rev_pct = float(n.get('lost_revenue_pct', 0))
    lost_rev = float(n.get('lost_revenue', 0))
    commission = float(n.get('commission', 0))
    avg_rating = float(n.get('avg_rating', 0))

    els = [_h2('Ключевые показатели ниши'), _hr()]

    # 3 большие карточки в ряд
    def _card(label, value, sub='', color=C_BLUE2):
        return [
            _p(label, size=8, color=C_GRAY, space_before=2, space_after=1),
            _p(value, size=14, bold=True, color=color, space_before=0, space_after=1),
            _p(sub, size=8, color=C_GRAY, space_before=0, space_after=2),
        ]

    row1 = [
        _card('ВЫРУЧКА НИШИ / МЕС', _rub(revenue), 'суммарная по всем товарам', C_NAVY),
        _card('ЗАКАЗОВ / МЕС', _num(orders), '', C_BLUE2),
        _card('ПРОДАВЦОВ', f'{sellers} / {sws} акт.', f'{round(sws/sellers*100) if sellers else 0}% с продажами', C_GREEN),
    ]
    row2 = [
        _card('ВЫКУП', _pct(buyout), 'доля выкупленных заказов',
              C_GREEN if buyout >= 0.7 else (C_AMBER if buyout >= 0.5 else C_RED)),
        _card('ОБОРАЧИВАЕМОСТЬ', f'{turnover:.0f} дней', 'сколько дней до продажи',
              C_GREEN if turnover <= 45 else (C_AMBER if turnover <= 90 else C_RED)),
        _card('МАРЖИНАЛЬНОСТЬ', _pct(profit), 'до вычета себестоимости',
              C_GREEN if profit >= 0.3 else (C_AMBER if profit >= 0.15 else C_RED)),
    ]
    row3 = [
        _card('СРЕДНИЙ ЧЕК', _rub(avg_price), 'средняя цена товара', C_NAVY),
        _card('УПУЩ. ВЫРУЧКА', _pct(lost_rev_pct) if lost_rev_pct else _rub(lost_rev),
              'потенциал при заполнении спроса',
              C_GREEN if lost_rev_pct > 0.2 else C_GRAY),
        _card('КОМИССИЯ WB', _pct(commission) if commission else '~25%', '', C_GRAY),
    ]

    cw = COL_W / 3

    def _cards_tbl(row):
        cells = [[Spacer(1, 2)] + c for c in row]
        t = Table([cells], colWidths=[cw]*3)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), C_LIGHT),
            ('BOX', (0,0), (0,-1), 1, C_LIGHT2),
            ('BOX', (1,0), (1,-1), 1, C_LIGHT2),
            ('BOX', (2,0), (2,-1), 1, C_LIGHT2),
            ('TOPPADDING', (0,0), (-1,-1), 8),
            ('BOTTOMPADDING', (0,0), (-1,-1), 8),
            ('LEFTPADDING', (0,0), (-1,-1), 8),
            ('RIGHTPADDING', (0,0), (-1,-1), 8),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ]))
        return t

    els.append(_cards_tbl(row1))
    els.append(_sp(0.08))
    els.append(_cards_tbl(row2))
    els.append(_sp(0.08))
    els.append(_cards_tbl(row3))
    els.append(_sp(0.15))
    return els


def _sec_top_products(items: list, limit: int = 20) -> list:
    if not items:
        return []
    els = [_h2(f'Топ-{min(limit, len(items))} товаров'), _hr()]
    rows = [['#', 'Название', 'Бренд', 'Цена, ₽', 'Выручка', 'Рейт.']]
    for i, it in enumerate(items[:limit], 1):
        name = str(it.get('name') or it.get('title') or '')[:45]
        brand = str(it.get('brand') or '')[:18]
        price = _rub(it.get('price') or it.get('final_price') or 0)
        rev = _rub(it.get('revenue') or 0)
        rating = f"{float(it.get('rating') or 0):.1f}" if it.get('rating') else '—'
        rows.append([str(i), name, brand, price, rev, rating])
    cw = [0.35*inch, 2.7*inch, 1.2*inch, 0.8*inch, 0.95*inch, 0.5*inch]
    els.append(_tbl(rows, col_widths=cw))
    els.append(_sp(0.1))
    return els


def _sec_master(r: dict) -> list:
    if not r:
        return []
    els = [PageBreak(), _h2('Мастер-анализ'), _hr()]

    verdict = str(r.get('final_verdict', ''))
    vc = r.get('verdict_color', '#d97706')
    conf = r.get('confidence', '')
    if verdict:
        vrow = [[_p(f'Вердикт: {verdict}', size=14, bold=True,
                    color=HexColor(vc) if vc.startswith('#') else C_AMBER,
                    align=TA_CENTER),
                 _p(f'Уверенность: {conf}', size=10, color=C_GRAY, align=TA_CENTER)]]
        vt = Table(vrow, colWidths=[COL_W*0.6, COL_W*0.4])
        vt.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (0,-1), C_LIGHT),
            ('TOPPADDING', (0,0), (-1,-1), 10), ('BOTTOMPADDING', (0,0), (-1,-1), 10),
            ('LEFTPADDING', (0,0), (-1,-1), 10),
        ]))
        els.append(vt)
        els.append(_sp(0.1))

    for field, label in [
        ('market_analysis', 'Анализ рынка'),
        ('competitive_landscape', 'Конкурентная среда'),
        ('entry_strategy', 'Стратегия входа'),
        ('final_recommendation', 'Итоговая рекомендация'),
    ]:
        txt = str(r.get(field, ''))
        if txt:
            els.append(_h3(label))
            els.append(_body(txt))

    opps = list(r.get('opportunities') or [])
    if opps:
        els.append(_h3('Возможности'))
        for o in opps:
            els.append(_bullet(str(o)))

    risks = list(r.get('risks') or [])
    if risks:
        els.append(_h3('Риски'))
        rows = [['Риск', 'Вероятность', 'Решение']]
        for risk in risks:
            rows.append([
                str(risk.get('risk', '')),
                str(risk.get('probability', '')),
                str(risk.get('mitigation', '')),
            ])
        els.append(_tbl(rows, col_widths=[2.5*inch, 1.2*inch, 2.9*inch]))

    fm = r.get('financial_model') or {}
    if fm:
        els.append(_h3('Финансовая модель'))
        rows = [['Показатель', 'Значение']]
        for k, lbl in [('test_batch_units','Тестовая партия, шт'), ('test_batch_cost','Стоимость партии, ₽'),
                       ('monthly_ad_budget','Бюджет рекламы/мес'), ('breakeven_units','Точка безубыточности, шт'),
                       ('roi_3months','ROI за 3 мес'), ('payback_months','Окупаемость, мес')]:
            v = fm.get(k)
            if v is not None:
                rows.append([lbl, _rub(v) if 'cost' in k or 'budget' in k else str(v)])
        els.append(_tbl(rows, col_widths=[3.5*inch, 3.1*inch]))

    sp = r.get('seasonal_plan') or {}
    if sp:
        els.append(_h3('Сезонный план'))
        rows = [['Пик', 'Спад', 'Закупка', 'Реклама']]
        rows.append([str(sp.get('peak','')), str(sp.get('low','')),
                     str(sp.get('buy_date','')), str(sp.get('ad_date',''))])
        els.append(_tbl(rows, col_widths=[1.6*inch]*4))

    els.append(_sp(0.1))
    return els


def _sec_deep(r: dict) -> list:
    if not r:
        return []
    els = [PageBreak(), _h2('Глубокий анализ'), _hr()]

    verdict = str(r.get('verdict', ''))
    desc = str(r.get('verdict_desc', ''))
    if verdict:
        els.append(_h3(f'Вердикт: {verdict}'))
    if desc:
        els.append(_body(desc))

    for field, label in [
        ('financial_plan', 'Финансовый план'),
        ('competitive_analysis', 'Конкурентный анализ'),
        ('free_segments', 'Свободные сегменты'),
        ('recommendation', 'Рекомендация'),
    ]:
        txt = str(r.get(field, ''))
        if txt:
            els.append(_h3(label))
            els.append(_body(txt))

    rows = [['Бюджет входа', 'Бюджет рекламы', 'Точка безуб.', 'ROI прогноз']]
    rows.append([
        _rub(r.get('entry_budget', 0)),
        _rub(r.get('ad_budget', 0)),
        _num(r.get('breakeven', 0)) + ' шт',
        str(r.get('roi_forecast', '—')),
    ])
    els.append(_tbl(rows, col_widths=[1.6*inch]*4))

    seas = [
        ('Пик продаж', r.get('season_peak_months', '')),
        ('Спад', r.get('season_low_months', '')),
        ('Когда закупать', r.get('purchase_months', '')),
        ('Совет по сезонности', r.get('season_tip', '')),
    ]
    if any(v for _, v in seas):
        els.append(_h3('Сезонность'))
        for label, val in seas:
            if val:
                els.append(_body(f'{label}: {val}'))

    els.append(_sp(0.1))
    return els


def _sec_unit(r: dict) -> list:
    if not r:
        return []
    els = [PageBreak(), _h2('Юнит-экономика'), _hr()]

    rec = r.get('recommendation') or {}
    if rec.get('title'):
        els.append(_h3(str(rec['title'])))
    if rec.get('detail'):
        els.append(_body(str(rec['detail'])))
        els.append(_sp(0.1))

    scenarios = r.get('scenarios') or {}
    vmap = {'profit': '✅ Прибыльно', 'marginal': '⚠ На грани', 'loss': '❌ Убыток'}
    rows = [['Показатель', 'Сц.1 (FBO BY)', 'Сц.2 (FBS)', 'Сц.3 (FBO RU)']]
    for field, label in [
        ('total_cost_rub',      'Себест.+логистика, ₽'),
        ('wb_commission_rub',   'Комиссия WB, ₽'),
        ('wb_logistics_rub',    'Логистика WB, ₽'),
        ('profit_per_unit_rub', 'Прибыль/ед, ₽'),
        ('roi_pct',             'ROI, %'),
        ('margin_pct',          'Маржа, %'),
    ]:
        row = [label]
        for sk in ('s1', 's2', 's3'):
            s = scenarios.get(sk) or {}
            val = s.get(field, '—')
            if val == '—':
                row.append('—')
            elif field.endswith('_pct'):
                row.append(f'{val}%')
            else:
                row.append(f'{int(float(val)):,}'.replace(',', ' '))
        rows.append(row)

    verdict_row = ['Вердикт']
    for sk in ('s1','s2','s3'):
        s = scenarios.get(sk) or {}
        verdict_row.append(vmap.get(s.get('verdict',''), '—'))
    rows.append(verdict_row)

    els.append(_tbl(rows, col_widths=[2.4*inch, 1.3*inch, 1.3*inch, 1.3*inch]))
    els.append(_sp(0.1))
    return els


def _sec_ads(r: dict) -> list:
    if not r:
        return []
    els = [PageBreak(), _h2('Рекламная стратегия'), _hr()]

    load = str(r.get('load_level', ''))
    load_labels = {'low': 'Низкая', 'medium': 'Средняя', 'high': 'Высокая'}
    load_colors = {'low': C_GREEN, 'medium': C_AMBER, 'high': C_RED}
    if load:
        lc = load_colors.get(load, C_GRAY)
        els.append(_p(f'Рекламная нагрузка: {load_labels.get(load, load)}',
                      size=11, bold=True, color=lc))

    analysis = str(r.get('load_analysis', ''))
    if analysis:
        els.append(_body(analysis))

    stype = str(r.get('strategy_type', ''))
    sdetail = str(r.get('strategy_detail', ''))
    if stype:
        els.append(_h3(f'Стратегия: {stype}'))
    if sdetail:
        els.append(_body(sdetail))

    steps = list(r.get('strategy_steps') or [])
    if steps:
        els.append(_h3('Шаги реализации'))
        for s in steps:
            els.append(_bullet(str(s)))

    budget = r.get('budget') or {}
    if budget:
        els.append(_h3('Бюджет рекламы'))
        rows = [['Фаза', 'Бюджет, ₽', 'Комментарий']]
        for phase, label in [('start_rub','Старт'),('growth_rub','Рост'),('sustain_rub','Поддержание')]:
            if budget.get(phase):
                rows.append([label, _rub(budget[phase]), ''])
        if budget.get('comment'):
            rows.append(['', '', str(budget['comment'])])
        els.append(_tbl(rows, col_widths=[1.5*inch, 1.5*inch, 3.6*inch]))

    cpm = r.get('cpm_forecast') or {}
    if cpm:
        els.append(_h3('Прогноз CPM'))
        rows = [['Старт', 'Мес. 2', 'Комментарий']]
        rows.append([_rub(cpm.get('start_rub',0)), _rub(cpm.get('month2_rub',0)),
                     str(cpm.get('comment',''))])
        els.append(_tbl(rows, col_widths=[1.5*inch, 1.5*inch, 3.6*inch]))

    forecast = r.get('forecast') or {}
    for mkey, mlabel in [('month1','Месяц 1 KPI'),('month2','Месяц 2 KPI')]:
        m = forecast.get(mkey) or {}
        metrics = list(m.get('metrics') or [])
        if metrics:
            els.append(_h3(mlabel))
            for metric in metrics:
                els.append(_bullet(str(metric)))

    els.append(_sp(0.1))
    return els


def _sec_supplier(r: dict) -> list:
    if not r:
        return []
    els = [PageBreak(), _h2('Поиск поставщиков и цены закупки'), _hr()]

    rows = [['Площадка', 'Цена, USD', 'MOQ, шт', 'Маржа', 'ROI', 'Прибыль/ед']]
    for platform, key in [('Taobao / 1688', 'price_taobao_usd'), ('Alibaba', 'price_alibaba_usd')]:
        price = r.get(key, 0)
        if price:
            rows.append([
                platform,
                f'${price}',
                str(r.get('moq', 0)),
                _pct(r.get('real_margin_pct', 0) / 100),
                _pct(r.get('roi_pct', 0) / 100),
                _rub(r.get('profit_per_unit_rub', 0)),
            ])
    if len(rows) > 1:
        els.append(_tbl(rows, col_widths=[1.8*inch, 1.0*inch, 0.9*inch, 0.8*inch, 0.8*inch, 1.3*inch]))

    summary = str(r.get('summary', ''))
    if summary:
        els.append(_h3('Вывод'))
        els.append(_body(summary))

    links = list(r.get('search_links') or [])
    if links:
        els.append(_h3('Площадки для поиска'))
        for lk in links:
            platform = str(lk.get('platform', ''))
            desc = str(lk.get('description', ''))
            url = str(lk.get('url', ''))
            els.append(_body(f'{platform}: {desc}'))

    els.append(_sp(0.1))
    return els


def _sec_docs(r: dict) -> list:
    if not r:
        return []
    els = [PageBreak(), _h2('Документы и сертификация'), _hr()]

    complexity = str(r.get('complexity', ''))
    comp_labels = {'low': 'Низкая сложность', 'medium': 'Средняя сложность', 'high': 'Высокая сложность'}
    comp_colors = {'low': C_GREEN, 'medium': C_AMBER, 'high': C_RED}
    if complexity:
        els.append(_p(comp_labels.get(complexity, complexity),
                      size=11, bold=True, color=comp_colors.get(complexity, C_GRAY)))

    wb_docs = list(r.get('wb_docs') or [])
    if wb_docs:
        els.append(_h3('Документы для WB'))
        rows = [['Документ', 'Стоимость', 'Срок', 'Обязат.']]
        for doc in wb_docs:
            rows.append([
                str(doc.get('name',''))[:40],
                _rub(doc.get('cost_rub', 0)),
                f"{doc.get('duration_days', 0)} дн.",
                'Да' if doc.get('required') else 'Нет',
            ])
        els.append(_tbl(rows, col_widths=[3.0*inch, 1.2*inch, 0.8*inch, 0.8*inch + 0.8*inch]))
        for doc in wb_docs:
            desc = str(doc.get('description', ''))
            if desc:
                els.append(_body(f'• {doc.get("name","")}: {desc}'))

    customs = list(r.get('customs_docs') or [])
    if customs:
        els.append(_h3('Документы от поставщика'))
        for doc in customs:
            els.append(_bullet(str(doc)))

    risks = list(r.get('risks') or [])
    if risks:
        els.append(_h3('Риски и решения'))
        for risk in risks:
            els.append(_body(f'⚠ {risk.get("risk","")} → {risk.get("solution","")}'))

    total_cost = r.get('total_cost_rub', 0)
    total_days = r.get('total_duration_days', 0)
    if total_cost or total_days:
        rows = [['Итого затраты', 'Итого срок']]
        rows.append([_rub(total_cost), f'{total_days} дней'])
        els.append(_tbl(rows, col_widths=[3.25*inch, 3.35*inch]))

    els.append(_sp(0.1))
    return els


def _sec_warehouse(r: dict) -> list:
    if not r:
        return []
    els = [PageBreak(), _h2('Стратегия поставок'), _hr()]

    model = str(r.get('model', ''))
    detail = str(r.get('model_detail', ''))
    if model:
        mc = {'fbs': C_BLUE2, 'fbo': C_GREEN, 'mixed': C_AMBER}
        col = mc.get(str(r.get('model_color', '')).lower(), C_BLUE2)
        els.append(_p(f'Модель: {model}', size=12, bold=True, color=col))
    if detail:
        els.append(_body(detail))
        els.append(_sp(0.1))

    tips = list(r.get('warehouse_tips') or [])
    if tips:
        els.append(_h3('Рекомендации по складам'))
        for tip in tips:
            els.append(_bullet(str(tip)))

    stock = r.get('stock') or {}
    if stock:
        els.append(_h3('Объём первой поставки'))
        rows = [['', 'Минимум', 'Оптимум', 'Максимум']]
        rows.append(['Единиц', str(stock.get('min_units','—')), str(stock.get('opt_units','—')), str(stock.get('max_units','—'))])
        rows.append(['Сумма', _rub(stock.get('min_rub',0)), _rub(stock.get('opt_rub',0)), _rub(stock.get('max_rub',0))])
        els.append(_tbl(rows, col_widths=[1.5*inch, 1.7*inch, 1.7*inch, 1.7*inch]))
        if stock.get('comment'):
            els.append(_body(str(stock['comment'])))

    wr = list(r.get('risks') or [])
    if wr:
        els.append(_h3('Риски логистики'))
        for risk in wr:
            els.append(_bullet(str(risk)))

    els.append(_sp(0.1))
    return els


def _sec_content(text: str) -> list:
    if not text:
        return []
    els = [PageBreak(), _h2('Создание карточки товара'), _hr()]
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            els.append(_sp(0.05))
        elif line[0].isdigit() and len(line) > 2 and line[1] == '.':
            els.append(_h3(line))
        elif line.startswith('- ') or line.startswith('• '):
            els.append(_bullet(line[2:]))
        elif line.startswith('**') and line.endswith('**'):
            els.append(_h3(line.strip('**')))
        else:
            els.append(_body(line))
    els.append(_sp(0.1))
    return els


def _sec_upsell(current_level: str) -> list:
    """Блок продажи в Basic PDF."""
    if current_level != 'basic':
        return []
    els = [PageBreak(), _h2('Хотите больше данных?'), _hr()]
    els.append(_body('Этот отчёт — Basic версия. Для полного анализа перед входом в нишу:'))
    els.append(_sp(0.1))

    rows = [
        ['', 'PDF Standard', 'PDF Deep'],
        ['Ключевые метрики', '✅', '✅'],
        ['Графики', '✅', '✅'],
        ['Топ-20 товаров', '✅', '✅'],
        ['Юнит-экономика', '✅', '✅'],
        ['Рекламная стратегия', '✅', '✅'],
        ['Глубокий анализ', '—', '✅'],
        ['Выбор поставщиков', '—', '✅'],
        ['Документы и сертификация', '—', '✅'],
        ['Стратегия поставок', '—', '✅'],
        ['Создание карточки', '—', '✅'],
    ]
    t = Table(rows, colWidths=[3.5*inch, 1.5*inch, 1.6*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), C_NAVY),
        ('TEXTCOLOR',  (0,0), (-1,0), WHITE),
        ('FONTNAME',   (0,0), (-1,0), FB),
        ('FONTSIZE',   (0,0), (-1,-1), 9),
        ('FONTNAME',   (0,1), (-1,-1), FN),
        ('GRID',       (0,0), (-1,-1), 0.4, C_LIGHT2),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [WHITE, C_LIGHT]),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('ALIGN',      (1,0), (-1,-1), 'CENTER'),
    ]))
    els.append(t)
    els.append(_sp(0.15))
    els.append(_p('Откройте WBAnalyzer и нажмите PDF Standard или PDF Deep для получения полного отчёта.',
                  size=9, color=C_GRAY))
    return els


# ── Главная функция ───────────────────────────────────────────────────────────

def generate(level: str, niche: dict, chart_items: list = None) -> bytes:
    """
    Генерирует PDF-отчёт.

    Args:
        level:       'basic' | 'standard' | 'deep'
        niche:       window.currentNiche из браузера
        chart_items: список товаров из MPStats (для графиков)
    Returns:
        PDF bytes
    """
    print(f'[PDF] Генерация уровень={level}, ниша={niche.get("name","")}')
    t0 = time.time()

    # ── Запускаем агентов ─────────────────────────────────────────────────────
    agents = {}
    content_text = ''

    try:
        print('[PDF] master...')
        agents['master'] = _run_master(niche)
    except Exception as e:
        print(f'[PDF] master error: {e}')
        agents['master'] = {}

    if level in ('standard', 'deep'):
        try:
            print('[PDF] unit economy...')
            agents['unit'] = _run_unit(niche)
        except Exception as e:
            print(f'[PDF] unit error: {e}')
            agents['unit'] = {}

        try:
            print('[PDF] ads...')
            agents['ads'] = _run_ads(niche)
        except Exception as e:
            print(f'[PDF] ads error: {e}')
            agents['ads'] = {}

    if level == 'deep':
        try:
            print('[PDF] deep analysis...')
            agents['deep'] = _run_deep(niche)
        except Exception as e:
            print(f'[PDF] deep error: {e}')
            agents['deep'] = {}

        try:
            print('[PDF] supplier...')
            agents['supplier'] = _run_supplier(niche)
        except Exception as e:
            print(f'[PDF] supplier error: {e}')
            agents['supplier'] = {}

        try:
            print('[PDF] docs...')
            agents['docs'] = _run_docs(niche)
        except Exception as e:
            print(f'[PDF] docs error: {e}')
            agents['docs'] = {}

        try:
            print('[PDF] warehouse...')
            agents['warehouse'] = _run_warehouse(niche)
        except Exception as e:
            print(f'[PDF] warehouse error: {e}')
            agents['warehouse'] = {}

        try:
            print('[PDF] content...')
            content_text = _run_content(niche)
        except Exception as e:
            print(f'[PDF] content error: {e}')
            content_text = ''

    print(f'[PDF] Агенты готовы за {time.time()-t0:.1f}s, собираем PDF...')

    # ── Собираем PDF ──────────────────────────────────────────────────────────
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        rightMargin=MARGIN, leftMargin=MARGIN,
        topMargin=0.6*inch, bottomMargin=0.5*inch,
    )

    items = chart_items or []
    top_limit = 5 if level == 'basic' else 20

    els = []
    els += _sec_cover(niche, level)
    els += _sec_metrics(niche)

    # Графики из данных MPStats
    if items:
        if len(items) >= 4:
            d = _chart_revenue_line(items)
            if d:
                els.append(_h2('Топ товары по выручке'))
                els.append(_hr())
                els.append(d)
                els.append(_sp(0.1))

        if level in ('standard', 'deep') and len(items) >= 6:
            d2 = _chart_price_bar(items)
            if d2:
                els.append(_h2('Распределение цен'))
                els.append(_hr())
                els.append(d2)
                els.append(_sp(0.1))

        if level == 'deep' and len(items) >= 8:
            d3 = _chart_sellers_pie(items)
            if d3:
                els.append(_h2('Доля продавцов'))
                els.append(_hr())
                els.append(d3)
                els.append(_sp(0.1))

    els += _sec_top_products(items, limit=top_limit)
    els += _sec_master(agents.get('master', {}))

    if level in ('standard', 'deep'):
        els += _sec_unit(agents.get('unit', {}))
        els += _sec_ads(agents.get('ads', {}))

    if level == 'deep':
        els += _sec_deep(agents.get('deep', {}))
        els += _sec_supplier(agents.get('supplier', {}))
        els += _sec_docs(agents.get('docs', {}))
        els += _sec_warehouse(agents.get('warehouse', {}))
        els += _sec_content(content_text)

    els += _sec_upsell(level)

    doc.build(els)
    buf.seek(0)
    print(f'[PDF] Готово за {time.time()-t0:.1f}s, размер={len(buf.getvalue())} байт')
    return buf.getvalue()
