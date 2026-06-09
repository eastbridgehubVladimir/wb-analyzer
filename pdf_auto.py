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
sys.path.insert(0, _ROOT)

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
    BaseDocTemplate, PageTemplate, Frame, NextPageTemplate,
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

# ── Стили из pdf_styles.py ────────────────────────────────────────────────────
from pdf_styles import (
    C_COVER_BG, C_COVER_SUB, C_NAVY, C_ACCENT, C_BLUE2, C_GREEN, C_RED,
    C_ORANGE, C_AMBER, C_GOLD, C_TEXT, C_GRAY, C_LIGHT_BG, C_TABLE_ODD,
    C_BORDER, C_WARN_BG, C_INFO_BG, WHITE,
    LEVEL_ACCENT, LEVEL_BADGE_BG, LEVEL_NAMES, LEVEL_SUBTITLES,
    LEVEL_UPSELL_NEXT, PLATFORM_URL, PLATFORM_YEAR,
    FS_COVER_LOGO, FS_COVER_NICHE, FS_H2, FS_H3, FS_BODY, FS_CAPTION, FS_SMALL,
)

# Алиасы совместимости (для кода, который использует старые имена)
C_LIGHT   = C_LIGHT_BG
C_LIGHT2  = C_BORDER
C_BLUE    = C_ACCENT
C_DARK    = C_COVER_BG
C_CYAN    = HexColor('#38bdf8')

W, H = A4
MARGIN = 0.55 * inch
COL_W  = W - 2 * MARGIN

# Текущий уровень документа (устанавливается в render())
_CURRENT_LEVEL = 'basic'

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

def _h2(text: str):
    """Заголовок раздела: светлый фон #f1f5f9 + цветная полоска слева."""
    accent = LEVEL_ACCENT.get(_CURRENT_LEVEL, C_ACCENT)
    ts = ParagraphStyle('_h2t', fontName=FB, fontSize=FS_H2,
                        textColor=C_NAVY, leading=18, spaceBefore=0, spaceAfter=0)
    inner = Table(
        [[Spacer(5, 20), Paragraph(text, ts)]],
        colWidths=[5, COL_W - 5]
    )
    inner.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), C_LIGHT_BG),
        ('BACKGROUND',    (0, 0), (0, -1),  accent),
        ('TOPPADDING',    (0, 0), (-1, -1), 9),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 9),
        ('LEFTPADDING',   (0, 0), (0, -1),  0),
        ('LEFTPADDING',   (1, 0), (1, -1),  12),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    return KeepTogether([Spacer(1, 0.15 * inch), inner])

def _h3(text: str):
    return _p(text, name='h3', size=FS_H3, bold=True, color=C_NAVY,
              space_before=8, space_after=3)

def _body(text: str):
    return _p(text, size=FS_BODY, space_before=2, space_after=3, leading=FS_BODY * 1.45)

def _bullet(text: str):
    return _p(f'• {text}', size=FS_BODY, space_before=2, space_after=2)

def _sp(h=0.1):
    return Spacer(1, h * inch)

def _hr():
    return HRFlowable(width='100%', thickness=0.5, color=C_BORDER,
                      spaceAfter=4, spaceBefore=2)

def _warning(text: str):
    """Блок-предупреждение: оранжевый фон с левой полоской."""
    s = ParagraphStyle('_warn', fontName=FN, fontSize=FS_CAPTION,
                       textColor=C_TEXT, leading=12, spaceBefore=0, spaceAfter=0)
    t = Table([[Spacer(5, 1), Paragraph(f'⚠ {text}', s)]],
              colWidths=[5, COL_W - 5])
    t.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), C_WARN_BG),
        ('BACKGROUND',    (0, 0), (0, -1),  C_ORANGE),
        ('TOPPADDING',    (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING',   (0, 0), (0, -1),  0),
        ('LEFTPADDING',   (1, 0), (1, -1),  10),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 10),
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
    ]))
    return t

def _info(text: str):
    """Блок-подсказка: синий фон с левой полоской."""
    s = ParagraphStyle('_info', fontName=FN, fontSize=FS_CAPTION,
                       textColor=C_TEXT, leading=12, spaceBefore=0, spaceAfter=0)
    t = Table([[Spacer(5, 1), Paragraph(f'ℹ {text}', s)]],
              colWidths=[5, COL_W - 5])
    t.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), C_INFO_BG),
        ('BACKGROUND',    (0, 0), (0, -1),  C_ACCENT),
        ('TOPPADDING',    (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING',   (0, 0), (0, -1),  0),
        ('LEFTPADDING',   (1, 0), (1, -1),  10),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 10),
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
    ]))
    return t

def _tbl(rows, col_widths=None, header_bg=None, row_bg=True):
    if not rows:
        return _sp(0.05)
    if header_bg is None:
        header_bg = C_NAVY
    n_cols = max(len(r) for r in rows)
    if col_widths is None:
        col_widths = [COL_W / n_cols] * n_cols
    _th_s = ParagraphStyle('_th', fontName=FB, fontSize=FS_SMALL,
                            textColor=WHITE, leading=11)
    _td_s = ParagraphStyle('_td', fontName=FN, fontSize=FS_SMALL,
                            textColor=C_TEXT, leading=11)
    wrapped = []
    for ri, row in enumerate(rows):
        crow = []
        for cell in row:
            if isinstance(cell, str):
                crow.append(Paragraph(cell, _th_s if ri == 0 else _td_s))
            else:
                crow.append(cell)
        wrapped.append(crow)
    t = Table(wrapped, colWidths=col_widths)
    cmds = [
        ('BACKGROUND',    (0, 0), (-1, 0), header_bg),
        ('GRID',          (0, 0), (-1, -1), 0.4, C_BORDER),
        ('TOPPADDING',    (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING',   (0, 0), (-1, -1), 6),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 6),
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
    ]
    if row_bg:
        cmds.append(('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, C_TABLE_ODD]))
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

def _run_master(n: dict, level: str = 'standard') -> dict:
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

    base = (
        f"НИША: {name}\n"
        f"Выручка: {revenue:,.0f} ₽/мес | Средняя цена: {avg_price:,.0f} ₽\n"
        f"Маржа: {profit_pct*100:.0f}% | Выкуп: {buyout_pct*100:.0f}%\n"
        f"Оборачиваемость: {turnover:.0f} дней | Продавцов: {sellers} (активных: {sws}, {act}%)\n"
        f"Средняя выручка/продавец: {avg_rev:,.0f} ₽/мес\n\n"
    )

    if level == 'basic':
        prompt = (
            "Ты аналитик WB. Сделай КРАТКИЙ обзор ниши для базового отчёта.\n\n"
            + base +
            "Правила:\n"
            "- market_analysis: 2-3 предложения, общий вывод без имён конкурентов и конкретных бюджетов. "
            "Заканчивай фразой: Подробный разбор — в PDF Standard.\n"
            "- competitive_landscape: 1-2 предложения общими словами о конкуренции. "
            "Заканчивай: Детальный анализ конкурентов — в PDF Standard.\n"
            "- entry_strategy: 1-2 предложения без конкретного бюджета и шагов. "
            "Заканчивай: Стратегия с цифрами — в PDF Standard.\n"
            "- final_recommendation: 2-3 предложения общего вывода, без конкретики. "
            "Заканчивай: Для полного анализа с цифрами получите PDF Standard.\n"
            "Ответь ONLY JSON:\n"
            '{"final_verdict":"ВХОДИТЬ|ТЕСТИРОВАТЬ|НЕ ВХОДИТЬ",'
            '"verdict_color":"#16a34a|#d97706|#dc2626",'
            '"market_analysis":"...",'
            '"competitive_landscape":"...",'
            '"entry_strategy":"...",'
            '"financial_model":{"test_batch_units":20,"test_batch_cost":220000,'
            '"monthly_ad_budget":45000,"breakeven_units":9,"roi_3months":"38%","payback_months":5},'
            '"opportunities":["возможность 1","возможность 2"],'
            '"risks":[{"risk":"риск","probability":"средняя","mitigation":"решение"}],'
            '"final_recommendation":"..."}'
        )
        return _json(_claude(prompt, 1200))

    if level == 'deep':
        prompt = (
            "Ты эксперт-аналитик WB. Сделай МАКСИМАЛЬНО ГЛУБОКИЙ анализ ниши.\n\n"
            + base +
            "Правила:\n"
            "- market_analysis: 5-6 предложений с конкретными цифрами объёма, динамики, сезонности.\n"
            "- competitive_landscape: Детальный разбор топ-3 конкурентов с именами, выручкой, долей рынка, "
            "слабыми местами каждого. 4-5 предложений.\n"
            "- entry_strategy: Конкретная дорожная карта на 3 месяца — что делать в каждый месяц, "
            "сколько тратить, какой результат ожидать. 4-5 предложений с цифрами.\n"
            "- final_recommendation: Подробный вывод с ключевыми метриками для масштабирования, "
            "чёткими условиями входа и планом действий. Минимум 5-6 предложений.\n"
            "Ответь ONLY JSON:\n"
            '{"final_verdict":"ВХОДИТЬ|ТЕСТИРОВАТЬ|НЕ ВХОДИТЬ",'
            '"verdict_color":"#16a34a|#d97706|#dc2626",'
            '"market_analysis":"...",'
            '"competitive_landscape":"...",'
            '"entry_strategy":"...",'
            '"financial_model":{"test_batch_units":20,"test_batch_cost":220000,'
            '"monthly_ad_budget":45000,"breakeven_units":9,"roi_3months":"38%","payback_months":5},'
            '"seasonal_plan":{"peak":"месяцы","low":"месяцы","buy_date":"дата","ad_date":"дата"},'
            '"opportunities":["возможность 1","возможность 2","возможность 3","возможность 4"],'
            '"risks":[{"risk":"риск","probability":"средняя","mitigation":"решение"},'
            '{"risk":"риск2","probability":"низкая","mitigation":"решение2"}],'
            '"final_recommendation":"..."}'
        )
        return _json(_claude(prompt, 3500))

    # standard (default)
    prompt = (
        "Ты старший аналитик WB. Сделай полный развёрнутый анализ ниши.\n\n"
        + base +
        "Правила:\n"
        "- market_analysis: 3-4 предложения с конкретными цифрами.\n"
        "- competitive_landscape: Назови конкретных конкурентов по имени, их ценовые диапазоны, доли. "
        "2-3 предложения.\n"
        "- entry_strategy: Конкретная стратегия входа с ценовым диапазоном и шагами. 3-4 предложения. "
        "Заканчивай: Поставщики, сертификаты и готовая карточка товара — только в PDF Deep.\n"
        "- final_recommendation: Полный абзац с чёткими условиями входа и метриками для принятия решения. "
        "4-5 предложений. Заканчивай: Для поиска поставщиков и полного пакета документов — PDF Deep.\n"
        "Ответь ONLY JSON:\n"
        '{"final_verdict":"ВХОДИТЬ|ТЕСТИРОВАТЬ|НЕ ВХОДИТЬ",'
        '"verdict_color":"#16a34a|#d97706|#dc2626",'
        '"market_analysis":"...",'
        '"competitive_landscape":"...",'
        '"entry_strategy":"...",'
        '"financial_model":{"test_batch_units":20,"test_batch_cost":220000,'
        '"monthly_ad_budget":45000,"breakeven_units":9,"roi_3months":"38%","payback_months":5},'
        '"seasonal_plan":{"peak":"месяцы","low":"месяцы","buy_date":"дата","ad_date":"дата"},'
        '"opportunities":["возможность 1","возможность 2","возможность 3"],'
        '"risks":[{"risk":"риск","probability":"средняя","mitigation":"решение"}],'
        '"final_recommendation":"..."}'
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
    """Обложка — контент на тёмном фоне (фон устанавливается через PageTemplate)."""
    from datetime import date
    name     = niche.get('display_name') or niche.get('name') or 'Анализ ниши'
    lname    = LEVEL_NAMES.get(level, level.upper())
    lsub     = LEVEL_SUBTITLES.get(level, '')
    accent   = LEVEL_ACCENT.get(level, C_ACCENT)
    badge_bg = LEVEL_BADGE_BG.get(level, HexColor('#1d4ed8'))

    def _wp(text, size=10, bold=False, color=WHITE, align=TA_CENTER,
            space_before=0, space_after=0):
        s = ParagraphStyle('_cp', fontName=FB if bold else FN, fontSize=size,
                            textColor=color, alignment=align,
                            spaceBefore=space_before, spaceAfter=space_after,
                            leading=size * 1.35)
        return Paragraph(str(text), s)

    els = []
    els.append(_sp(1.1))

    # ── Логотип WBAnalyzer ──────────────────────────────────────────────────
    els.append(_wp('WBAnalyzer', size=FS_COVER_LOGO, bold=True, color=WHITE))
    els.append(_sp(0.1))
    els.append(_wp('◆ AI Platform · Аналитика нового поколения',
                    size=10, color=C_COVER_SUB))
    els.append(_sp(0.35))

    # Акцентная горизонтальная линия
    acl = Table([['']], colWidths=[3.5 * inch], rowHeights=[2.5])
    acl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), accent),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    acl_wrap = Table([[acl]], colWidths=[COL_W])
    acl_wrap.setStyle(TableStyle([
        ('ALIGN',       (0, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING',  (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING',(0,0), (-1, -1), 0),
    ]))
    els.append(acl_wrap)
    els.append(_sp(0.5))

    # ── Название ниши ────────────────────────────────────────────────────────
    els.append(_wp(name, size=FS_COVER_NICHE, bold=True, color=WHITE,
                    space_before=0, space_after=0))
    els.append(_sp(0.55))

    # ── Бейдж уровня ─────────────────────────────────────────────────────────
    badge = Table(
        [[_wp(lname, size=11, bold=True, color=WHITE)]],
        colWidths=[2.6 * inch]
    )
    badge.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), badge_bg),
        ('TOPPADDING',    (0, 0), (-1, -1), 9),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 9),
        ('LEFTPADDING',   (0, 0), (-1, -1), 20),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 20),
    ]))
    badge_wrap = Table([[badge]], colWidths=[COL_W])
    badge_wrap.setStyle(TableStyle([
        ('ALIGN',       (0, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING',  (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING',(0,0), (-1, -1), 0),
    ]))
    els.append(badge_wrap)

    if lsub:
        els.append(_sp(0.14))
        els.append(_wp(lsub, size=9, color=C_COVER_SUB))

    # ── Дата ─────────────────────────────────────────────────────────────────
    els.append(_sp(0.6))
    els.append(_wp(date.today().strftime('%d.%m.%Y'), size=9,
                    color=HexColor('#64748b'), align=TA_RIGHT))

    # ── Акцентная линия внизу ─────────────────────────────────────────────────
    els.append(_sp(0.18))
    bot_line = Table([['']], colWidths=[COL_W], rowHeights=[2])
    bot_line.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), accent),
        ('TOPPADDING',    (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    els.append(bot_line)

    # ── Персонализация ───────────────────────────────────────────────────────
    prepared_for = str(niche.get('prepared_for', '')).strip()
    if prepared_for:
        els.append(_sp(0.2))
        pf = Table(
            [[_wp(f'Подготовлено персонально для: {prepared_for}',
                  size=9, color=HexColor('#bfdbfe'))]],
            colWidths=[COL_W]
        )
        pf.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), HexColor('#1e3a5f')),
            ('TOPPADDING', (0, 0), (-1, -1), 9),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 9),
            ('BOX', (0, 0), (-1, -1), 0.5, HexColor('#3b82f6')),
        ]))
        els.append(pf)

    return els


def _sec_metrics(niche: dict) -> list:
    n = niche
    # revenue_annual = revenue/2 (DB stores ~2yr total); fallback to revenue/2 if not set
    revenue    = float(n.get('revenue_annual', 0)) or float(n.get('revenue', 0)) / 2
    orders     = int(n.get('orders', 0))
    sellers    = int(n.get('sellers', 0))
    sws        = int(n.get('sellers_with_sales', 0))
    buyout     = float(n.get('buyout_pct', 0))
    profit     = float(n.get('profit_pct', 0))
    turnover   = float(n.get('turnover', 0))
    avg_price  = float(n.get('avg_price', 0))
    commission = float(n.get('commission', 0))

    act_pct        = round(sws / sellers * 100) if sellers else 0
    # Используем raw orders как есть — не пересчитываем
    orders_per_day = round(orders / 30) if orders > 0 else 0

    els = [_h2('Ключевые показатели ниши'), _hr()]

    def _card(label, value, sub='', color=C_BLUE2):
        return [
            _p(label, size=7, color=C_GRAY, space_before=2, space_after=1),
            _p(value, size=14, bold=True, color=color, space_before=0, space_after=1),
            _p(sub,   size=7, color=C_GRAY, space_before=0, space_after=2),
        ]

    # Ряд 1: Выручка, Заказы, Продавцы
    orders_sub = f'≈ {orders_per_day} в день' if orders_per_day else ''
    row1 = [
        _card('ВЫРУЧКА НИШИ', _rub(revenue), 'за 12 месяцев', C_NAVY),
        _card('ЗАКАЗОВ / МЕС', _num(orders), orders_sub, C_BLUE2),
        _card('ПРОДАВЦОВ', f'{sellers} / {sws} акт.', f'{act_pct}% с продажами', C_GREEN),
    ]

    # Ряд 2: Выкуп, Средний чек, Комиссия (оборачиваемость и маржа — в блоке ниже)
    buy_col  = C_GREEN if buyout >= 0.7 else (C_AMBER if buyout >= 0.5 else C_RED)
    comm_str = _pct(commission) if commission > 0 else '~20–25%'
    row2 = [
        _card('ВЫКУП', _pct(buyout), 'доля выкупленных заказов', buy_col),
        _card('СРЕДНИЙ ЧЕК', _rub(avg_price), 'средняя цена единицы', C_NAVY),
        _card('КОМИССИЯ WB', comm_str, 'по категории товара', C_GRAY),
    ]

    cw = COL_W / 3

    def _cards_tbl(row):
        cells = [[Spacer(1, 2)] + c for c in row]
        t = Table([cells], colWidths=[cw]*3)
        t.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), C_LIGHT),
            ('BOX',           (0,0), (0,-1), 1, C_LIGHT2),
            ('BOX',           (1,0), (1,-1), 1, C_LIGHT2),
            ('BOX',           (2,0), (2,-1), 1, C_LIGHT2),
            ('TOPPADDING',    (0,0), (-1,-1), 8),
            ('BOTTOMPADDING', (0,0), (-1,-1), 8),
            ('LEFTPADDING',   (0,0), (-1,-1), 8),
            ('RIGHTPADDING',  (0,0), (-1,-1), 8),
            ('VALIGN',        (0,0), (-1,-1), 'TOP'),
        ]))
        return t

    els.append(_cards_tbl(row1))
    els.append(_sp(0.05))
    els.append(_cards_tbl(row2))

    # ── Оборачиваемость и Маржа — блоки с пояснениями ────────────────────────
    if turnover:
        if turnover > 90:
            els.append(_sp(0.1))
            els.append(_warning(
                f'<b>Оборачиваемость: {turnover:.0f} дней</b> — рекомендуем начать с малой партии '
                f'и работать по FBS-схеме, чтобы не замораживать капитал в стоке.'
            ))
        elif turnover > 45:
            els.append(_sp(0.1))
            els.append(_info(
                f'<b>Оборачиваемость: {turnover:.0f} дней</b> — умеренная. '
                f'Следите за остатками, не допускайте затоваривания.'
            ))
        else:
            els.append(_sp(0.1))
            els.append(_info(
                f'<b>Оборачиваемость: {turnover:.0f} дней</b> — отлично, товар быстро продаётся.'
            ))

    if profit > 0:
        els.append(_sp(0.06))
        els.append(_info(
            f'<b>Маржа по WB: {_pct(profit)}</b> — выручка ниши за вычетом комиссии и логистики WB, '
            f'но <b>без учёта себестоимости товара</b>. '
            f'Реальная чистая прибыль обычно 20–35% — уточняйте в разделе «Юнит-экономика».'
        ))

    els.append(_sp(0.12))
    return els


def _sec_top_products(items: list, limit: int = 20, level: str = 'standard') -> list:
    if not items:
        return []
    count = min(limit, len(items))
    els = [_h2(f'Топ-{count} товаров ниши'), _hr()]

    # Вычисляем суммарную выручку для доли рынка
    total_rev = sum(float(it.get('revenue') or 0) for it in items)

    if level == 'basic':
        # Basic: компактная таблица с 5 товарами
        rows = [['#', 'Название товара', 'Цена, ₽', 'Выручка/мес', 'Отзывы', 'Ссылка']]
        for i, it in enumerate(items[:limit], 1):
            name  = str(it.get('name') or it.get('title') or '')[:45]
            price = _rub(it.get('price') or it.get('final_price') or 0)
            rev   = _rub(it.get('revenue') or 0)
            fb    = str(int(it.get('feedbacks') or it.get('reviews') or
                             it.get('reviews_count') or 0))
            sku   = str(it.get('sku') or it.get('wb_sku') or it.get('id') or '—')
            link  = f'→ {sku}' if sku != '—' else '—'
            rows.append([str(i), name, price, rev, fb, link])
        cw = [0.28*inch, 2.8*inch, 0.8*inch, 0.95*inch, 0.6*inch, 0.9*inch]
    else:
        # Standard/Deep: таблица с 20 товарами + доля рынка, компактная
        rows = [['#', 'Название товара', 'Цена, ₽', 'Выручка/мес', 'Отзывы', 'Доля, %', 'WB']]
        for i, it in enumerate(items[:limit], 1):
            name  = str(it.get('name') or it.get('title') or '')[:38]
            price = _rub(it.get('price') or it.get('final_price') or 0)
            rev   = _rub(it.get('revenue') or 0)
            fb    = str(int(it.get('feedbacks') or it.get('reviews') or
                             it.get('reviews_count') or 0))
            item_rev = float(it.get('revenue') or 0)
            share = f'{item_rev / total_rev * 100:.1f}%' if total_rev else '—'
            sku   = str(it.get('sku') or it.get('wb_sku') or it.get('id') or '—')
            link  = f'→{sku}' if sku != '—' else '—'
            rows.append([str(i), name, price, rev, fb, share, link])
        cw = [0.25*inch, 2.45*inch, 0.7*inch, 0.85*inch, 0.55*inch, 0.6*inch, 0.7*inch]

    # Компактный стиль для всех уровней
    t = _tbl(rows, col_widths=cw)
    t.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, 0), C_NAVY),
        ('FONTSIZE',      (0, 0), (-1, -1), 8 if level == 'basic' else 7.5),
        ('TOPPADDING',    (0, 0), (-1, -1), 4 if level == 'basic' else 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4 if level == 'basic' else 3),
        ('LEFTPADDING',   (0, 0), (-1, -1), 5 if level == 'basic' else 4),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 5 if level == 'basic' else 4),
        ('GRID',          (0, 0), (-1, -1), 0.3, C_BORDER),
        ('ROWBACKGROUNDS',(0, 1), (-1, -1), [WHITE, C_TABLE_ODD]),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    els.append(t)
    els.append(_sp(0.1))
    return els


_FM_CANONICAL = {
    'test_batch_units': 20,
    'test_batch_cost': 220000,
    'monthly_ad_budget': 45000,
    'breakeven_units': 9,
    'roi_3months': '38%',
    'payback_months': '5',
}


def _sec_master(r: dict) -> list:
    if not r:
        return []
    level = _CURRENT_LEVEL
    els = [_h2('Мастер-анализ'), _hr()]

    verdict = str(r.get('final_verdict', ''))
    vc = r.get('verdict_color', '#d97706')
    if verdict:
        vt = Table([[_p(f'Вердикт: {verdict}', size=14, bold=True,
                        color=HexColor(vc) if vc.startswith('#') else C_AMBER,
                        align=TA_CENTER)]], colWidths=[COL_W])
        vt.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), C_LIGHT),
            ('TOPPADDING', (0,0), (-1,-1), 12), ('BOTTOMPADDING', (0,0), (-1,-1), 12),
            ('LEFTPADDING', (0,0), (-1,-1), 10), ('RIGHTPADDING', (0,0), (-1,-1), 10),
        ]))
        els.append(vt)
        els.append(_sp(0.1))

    if level == 'basic':
        # Краткий обзор: только market_analysis + final_recommendation
        for field, label in [
            ('market_analysis',      'Обзор рынка'),
            ('competitive_landscape', 'Конкурентная среда'),
            ('entry_strategy',        'Стратегия входа'),
            ('final_recommendation',  'Итоговый вывод'),
        ]:
            txt = str(r.get(field, ''))
            if txt:
                els.append(_h3(label))
                els.append(_body(txt))
    else:
        # Standard / Deep: полный набор разделов
        for field, label in [
            ('market_analysis',      'Анализ рынка'),
            ('competitive_landscape', 'Конкурентная среда'),
            ('entry_strategy',        'Стратегия входа'),
            ('final_recommendation',  'Итоговая рекомендация'),
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
            _prob_colors = {'высокая': C_RED, 'средняя': C_AMBER, 'низкая': C_GREEN}
            rows = [['Риск', 'Вероятность', 'Решение']]
            for risk in risks:
                prob_str = str(risk.get('probability', '')).lower().strip()
                prob_cell = _p(prob_str.capitalize(), size=8, bold=True,
                               color=_prob_colors.get(prob_str, C_GRAY), align=TA_CENTER)
                rows.append([str(risk.get('risk', '')), prob_cell, str(risk.get('mitigation', ''))])
            els.append(_tbl(rows, col_widths=[2.5*inch, 1.1*inch, 3.0*inch]))

    # Финансовая модель — во всех уровнях, с каноническими значениями как база
    fm = r.get('financial_model') or {}
    fm_merged = {**_FM_CANONICAL, **{k: v for k, v in fm.items() if v is not None and str(v).strip()}}
    els.append(_h3('Финансовая модель'))
    rows = [['Показатель', 'Значение']]
    for k, lbl in [('test_batch_units','Тестовая партия, шт'), ('test_batch_cost','Стоимость партии, ₽'),
                   ('monthly_ad_budget','Бюджет рекламы/мес'), ('breakeven_units','Точка безубыточности, шт'),
                   ('roi_3months','ROI за 3 мес'), ('payback_months','Окупаемость, мес')]:
        v = fm_merged.get(k)
        if v is not None:
            rows.append([lbl, _rub(v) if 'cost' in k or 'budget' in k else str(v)])
    els.append(_tbl(rows, col_widths=[3.5*inch, 3.1*inch]))

    if level != 'basic':
        sp = r.get('seasonal_plan') or {}
        if sp:
            els.append(_h3('Сезонный план'))
            for lbl, val in [('Пик продаж', sp.get('peak', '')), ('Период спада', sp.get('low', '')),
                              ('Когда закупать', sp.get('buy_date', '')), ('Старт рекламы', sp.get('ad_date', ''))]:
                if str(val).strip():
                    els.append(_p(f'<b>{lbl}:</b> {val}', size=9, space_before=2, space_after=2))

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
    els = [_h2('Юнит-экономика'), _hr()]

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
    els = [_h2('Рекламная стратегия'), _hr()]

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

    _KPI_EXPAND = {
        'CTR':    'CTR (кликабельность, %)',
        'CR':     'CR (конверсия в заказ, %)',
        'DRR':    'DRR (доля рекламных расходов, %)',
        'ДРР':    'ДРР (доля рекламных расходов, %)',
        'Pos':    'Позиция в выдаче',
        'Orders': 'Заказов в месяц',
    }
    def _expand_kpi(s: str) -> str:
        for abbr, full in _KPI_EXPAND.items():
            if s.startswith(abbr + ':') or s.startswith(abbr + ' '):
                return full + s[len(abbr):]
        return s

    forecast = r.get('forecast') or {}
    for mkey, mlabel in [('month1','Месяц 1 KPI'),('month2','Месяц 2 KPI')]:
        m = forecast.get(mkey) or {}
        metrics = list(m.get('metrics') or [])
        if metrics:
            els.append(_h3(mlabel))
            for metric in metrics:
                els.append(_bullet(_expand_kpi(str(metric))))

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

    # Нейтральная рекомендация по складам (независимо от AI-агента)
    els.append(_info(
        '<b>Выбор склада:</b> ориентируйтесь на ваш регион и скорость приёмки. '
        'Топ складов WB: <b>Коледино</b> (Московская обл.), <b>Подольск</b>, '
        '<b>Электросталь</b>, <b>Казань</b>, <b>Краснодар</b>, <b>Екатеринбург</b>. '
        'Для поставщиков из Беларуси: Смоленск и транзит через белорусские склады.'
    ))
    els.append(_sp(0.08))

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


def _sec_conclusion(level: str, agents: dict) -> list:
    """Итоговый вывод — выделенный блок в конце аналитического контента."""
    master  = agents.get('master') or {}
    verdict = str(master.get('final_verdict', '')).strip()
    rec     = str(master.get('final_recommendation', '')).strip()
    vc      = str(master.get('verdict_color', '#d97706')).strip()
    if not (verdict or rec):
        return []

    if '#16a34a' in vc or '#22c55e' in vc:
        bg, bord, tc = HexColor('#f0fdf4'), C_GREEN,  C_GREEN
    elif '#dc2626' in vc or '#ef4444' in vc:
        bg, bord, tc = HexColor('#fef2f2'), C_RED,    C_RED
    else:
        bg, bord, tc = HexColor('#fffbeb'), C_AMBER,  C_AMBER

    inner = []
    inner.append(_p('Итоговый вывод', size=10, bold=True, color=tc,
                    space_before=0, space_after=4))
    if verdict:
        inner.append(_p(f'Решение: {verdict}', size=13, bold=True, color=tc,
                        space_before=2, space_after=4))
    if rec:
        inner.append(_p(rec, size=9, color=C_TEXT, space_before=2, space_after=2))

    tbl = Table([[inner]], colWidths=[COL_W])
    tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,-1), bg),
        ('TOPPADDING',    (0,0), (-1,-1), 12),
        ('BOTTOMPADDING', (0,0), (-1,-1), 12),
        ('LEFTPADDING',   (0,0), (-1,-1), 14),
        ('RIGHTPADDING',  (0,0), (-1,-1), 14),
        ('BOX',           (0,0), (-1,-1), 1.5, bord),
    ]))
    return [_hr(), _sp(0.05), tbl, _sp(0.15)]


def _sec_upsell(current_level: str) -> list:
    """Блок апсейла в конце PDF."""
    if current_level == 'deep':
        return []
    els = []

    if current_level == 'basic':
        els.append(_sp(0.1))
        els.append(_hr())
        els.append(_h2('Получите полный анализ — PDF Standard и Deep'))
        els.append(_hr())
        els.append(_body('Этот отчёт — Basic версия. Сравните, что содержит каждый уровень:'))
        els.append(_sp(0.1))
        rows = [
            ['Раздел анализа',           'Basic', 'Standard', 'Deep'],
            ['Ключевые метрики ниши',    '✅',    '✅',       '✅'],
            ['2 ключевых графика',        '✅',    '—',        '—'],
            ['Все 3 графика ниши',         '—',     '✅',       '✅'],
            ['Топ-5 товаров',             '✅',    '—',        '—'],
            ['Топ-20 товаров',            '—',     '✅',       '✅'],
            ['Мастер-анализ AI',          '✅',    '✅',       '✅'],
            ['Юнит-экономика (3 сценария)','—',   '✅',       '✅'],
            ['Рекламная стратегия',       '—',     '✅',       '✅'],
            ['Глубокий анализ ниши',      '—',     '—',        '✅'],
            ['Поиск поставщиков + цены',  '—',     '—',        '✅'],
            ['Документы и сертификаты',   '—',     '—',        '✅'],
            ['Стратегия поставок WB',     '—',     '—',        '✅'],
            ['Карточка товара (AI-текст)','—',     '—',        '✅'],
        ]
        t = Table(rows, colWidths=[3.1*inch, 1.1*inch, 1.1*inch, 1.3*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND',     (0,0), (-1,0), C_NAVY),
            ('TEXTCOLOR',      (0,0), (-1,0), WHITE),
            ('FONTNAME',       (0,0), (-1,0), FB),
            ('FONTSIZE',       (0,0), (-1,-1), 8.5),
            ('FONTNAME',       (0,1), (-1,-1), FN),
            ('GRID',           (0,0), (-1,-1), 0.4, C_LIGHT2),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [WHITE, C_LIGHT]),
            ('TOPPADDING',     (0,0), (-1,-1), 6),
            ('BOTTOMPADDING',  (0,0), (-1,-1), 6),
            ('ALIGN',          (1,0), (-1,-1), 'CENTER'),
            ('BACKGROUND',     (2,1), (2,-1), HexColor('#eff6ff')),
            ('BACKGROUND',     (3,1), (3,-1), HexColor('#f5f3ff')),
        ]))
        els.append(t)
        els.append(_sp(0.15))
        els.append(_p('Нажмите кнопку PDF Standard или PDF Deep в WBAnalyzer для полного отчёта.',
                      size=9, color=C_GRAY, align=TA_CENTER))

    elif current_level == 'standard':
        els.append(_sp(0.25))

        # ── Главный баннер ─────────────────────────────────────────────────────
        C_GOLD   = HexColor('#f59e0b')
        C_GOLD2  = HexColor('#fef3c7')
        C_DEEP   = HexColor('#1e0a3c')
        C_PURP   = HexColor('#7c3aed')
        C_PURP2  = HexColor('#ede9fe')

        # Верхняя плашка — «только в Deep»
        badge_s  = ParagraphStyle('_up_badge', fontName=FB, fontSize=8,
                                   textColor=C_DEEP, leading=11, alignment=TA_CENTER)
        badge_p  = Paragraph('◆ ЭКСКЛЮЗИВНО В PDF DEEP ◆', badge_s)
        badge_t  = Table([[badge_p]], colWidths=[COL_W])
        badge_t.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), C_GOLD),
            ('TOPPADDING',    (0,0), (-1,-1), 6),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
            ('LEFTPADDING',   (0,0), (-1,-1), 0),
            ('RIGHTPADDING',  (0,0), (-1,-1), 0),
        ]))
        els.append(badge_t)

        # Основной заголовок на тёмном фоне
        head_s   = ParagraphStyle('_up_head', fontName=FB, fontSize=18,
                                   textColor=WHITE, leading=24, alignment=TA_CENTER)
        sub_s    = ParagraphStyle('_up_sub', fontName=FN, fontSize=10,
                                   textColor=HexColor('#c4b5fd'), leading=14, alignment=TA_CENTER)
        head_blk = Table([
            [Paragraph('Получите PDF Deep —', head_s)],
            [Paragraph('полный профессиональный анализ ниши', head_s)],
            [Spacer(1, 4)],
            [Paragraph('5 дополнительных разделов · Готовые данные для старта · AI-текст карточки', sub_s)],
        ], colWidths=[COL_W])
        head_blk.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), C_DEEP),
            ('TOPPADDING',    (0,0), (-1,-1), 14),
            ('BOTTOMPADDING', (0,0), (-1,-1), 14),
            ('LEFTPADDING',   (0,0), (-1,-1), 12),
            ('RIGHTPADDING',  (0,0), (-1,-1), 12),
        ]))
        els.append(head_blk)

        # ── 5 фич в 2 колонки ──────────────────────────────────────────────────
        feats = [
            ('🔬', 'Глубокий анализ ниши',
             'Детальный разбор конкурентной среды, свободных сегментов рынка и ROI на 12 месяцев вперёд'),
            ('🏭', 'Поиск поставщиков',
             'Закупочные цены на Alibaba и 1688, расчёт маржи, MOQ и прямые ссылки на проверенных поставщиков'),
            ('📋', 'Документы и сертификаты',
             'Полный список обязательных документов для выхода на WB: стоимость и сроки оформления'),
            ('📦', 'Стратегия поставок',
             'Анализ FBS vs FBO, расчёт объёма первой поставки и выбор оптимальных складов WB'),
            ('✍', 'Карточка товара (AI)',
             'Готовый текст: заголовок, полное описание, характеристики и ключевые слова для SEO-продвижения'),
        ]

        def _feat_cell(icon, title, desc):
            icon_s  = ParagraphStyle('_fi', fontName=FB, fontSize=16, textColor=C_GOLD,
                                      leading=20, alignment=TA_CENTER)
            title_s = ParagraphStyle('_ft', fontName=FB, fontSize=9, textColor=C_DEEP,
                                      leading=12, spaceAfter=2)
            desc_s  = ParagraphStyle('_fd', fontName=FN, fontSize=8, textColor=HexColor('#374151'),
                                      leading=11)
            return Table([
                [Paragraph(icon, icon_s)],
                [Paragraph(title, title_s)],
                [Paragraph(desc, desc_s)],
            ], colWidths=[(COL_W / 2) - 10])

        hw = COL_W / 2 - 8
        feat_style = TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), WHITE),
            ('BOX',           (0,0), (-1,-1), 1.5, C_PURP),
            ('TOPPADDING',    (0,0), (-1,-1), 10),
            ('BOTTOMPADDING', (0,0), (-1,-1), 10),
            ('LEFTPADDING',   (0,0), (-1,-1), 10),
            ('RIGHTPADDING',  (0,0), (-1,-1), 10),
            ('VALIGN',        (0,0), (-1,-1), 'TOP'),
        ])

        def _feat_box(icon, title, desc):
            icon_s  = ParagraphStyle('_fi2', fontName=FB, fontSize=14, textColor=C_PURP,
                                      leading=18, alignment=TA_LEFT)
            title_s = ParagraphStyle('_ft2', fontName=FB, fontSize=9.5, textColor=C_DEEP,
                                      leading=13, spaceAfter=3)
            desc_s  = ParagraphStyle('_fd2', fontName=FN, fontSize=8, textColor=HexColor('#374151'),
                                      leading=11)
            inner = Table([
                [Paragraph(icon + '  ' + title, title_s)],
                [Paragraph(desc, desc_s)],
            ], colWidths=[hw])
            inner.setStyle(TableStyle([
                ('LEFTPADDING',   (0,0), (-1,-1), 0),
                ('RIGHTPADDING',  (0,0), (-1,-1), 0),
                ('TOPPADDING',    (0,0), (-1,-1), 1),
                ('BOTTOMPADDING', (0,0), (-1,-1), 1),
            ]))
            wrap = Table([[inner]], colWidths=[hw + 20])
            wrap.setStyle(TableStyle([
                ('BACKGROUND',    (0,0), (-1,-1), WHITE),
                ('BOX',           (0,0), (-1,-1), 1.5, C_PURP),
                ('TOPPADDING',    (0,0), (-1,-1), 9),
                ('BOTTOMPADDING', (0,0), (-1,-1), 9),
                ('LEFTPADDING',   (0,0), (-1,-1), 10),
                ('RIGHTPADDING',  (0,0), (-1,-1), 10),
                ('VALIGN',        (0,0), (-1,-1), 'TOP'),
            ]))
            return wrap

        gap = COL_W - 2 * (hw + 20)
        # Строим 2 колонки: феату 0+1, 2+3, 4 по центру
        pairs = [
            (feats[0], feats[1]),
            (feats[2], feats[3]),
        ]
        feat_bg = Table([
            [_feat_box(*pairs[0][0]), Spacer(gap, 1), _feat_box(*pairs[0][1])],
            [Spacer(1, 6), '', ''],
            [_feat_box(*pairs[1][0]), Spacer(gap, 1), _feat_box(*pairs[1][1])],
        ], colWidths=[hw + 20, gap, hw + 20])
        feat_bg.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), C_PURP2),
            ('LEFTPADDING',   (0,0), (-1,-1), 0),
            ('RIGHTPADDING',  (0,0), (-1,-1), 0),
            ('TOPPADDING',    (0,0), (-1,-1), 0),
            ('BOTTOMPADDING', (0,0), (-1,-1), 0),
            ('SPAN',          (0,1), (-1,1)),
        ]))
        outer = Table([[feat_bg]], colWidths=[COL_W])
        outer.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), C_PURP2),
            ('TOPPADDING',    (0,0), (-1,-1), 12),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('LEFTPADDING',   (0,0), (-1,-1), 12),
            ('RIGHTPADDING',  (0,0), (-1,-1), 12),
        ]))
        els.append(outer)

        # 5-й элемент — полная ширина
        fifth_box = _feat_box(*feats[4])
        fifth_outer = Table([[fifth_box]], colWidths=[COL_W])
        fifth_outer.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), C_PURP2),
            ('TOPPADDING',    (0,0), (-1,-1), 0),
            ('BOTTOMPADDING', (0,0), (-1,-1), 12),
            ('LEFTPADDING',   (0,0), (-1,-1), 12),
            ('RIGHTPADDING',  (0,0), (-1,-1), 12),
        ]))
        els.append(fifth_outer)

        # ── CTA-кнопка ─────────────────────────────────────────────────────────
        cta_s   = ParagraphStyle('_cta', fontName=FB, fontSize=13,
                                  textColor=C_DEEP, leading=18, alignment=TA_CENTER)
        cta_sub = ParagraphStyle('_cta2', fontName=FN, fontSize=9,
                                  textColor=C_DEEP, leading=13, alignment=TA_CENTER)
        cta_blk = Table([
            [Paragraph('▶  Нажмите PDF Deep в WBAnalyzer', cta_s)],
            [Paragraph('и получите полный анализ прямо сейчас', cta_sub)],
        ], colWidths=[COL_W])
        cta_blk.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), C_GOLD),
            ('TOPPADDING',    (0,0), (-1,-1), 12),
            ('BOTTOMPADDING', (0,0), (-1,-1), 12),
            ('LEFTPADDING',   (0,0), (-1,-1), 12),
            ('RIGHTPADDING',  (0,0), (-1,-1), 12),
        ]))
        els.append(cta_blk)

    return els


def _sec_glossary() -> list:
    """Словарь терминов — одна страница перед финалом."""
    els = [_h2('Словарь терминов'), _hr()]
    els.append(_body('Расшифровка аббревиатур и терминов, использованных в этом отчёте.'))
    els.append(_sp(0.1))

    terms = [
        ('FBO (Fulfillment by Operator)',
         'Хранение и отправка товара со склада Wildberries. '
         'Вы отгружаете партию на склад WB, далее WB сам упаковывает и доставляет заказы.'),
        ('FBS (Fulfillment by Seller)',
         'Хранение у продавца, отправка самостоятельно. '
         'Вы держите товар у себя и передаёте его в WB только после получения заказа.'),
        ('FBO BY',
         'Схема FBO с хранением на складе Wildberries в Беларуси. '
         'Актуально для поставщиков из РБ или при выгодных условиях хранения.'),
        ('DRR / ДРР',
         'Доля рекламных расходов от выручки, в %. '
         'Формула: бюджет рекламы / выручка × 100. Норма для WB — 10–15%.'),
        ('CPM',
         'Стоимость 1 000 показов рекламного объявления, в рублях. '
         'Чем выше CPM — тем дороже трафик. Типичный диапазон для WB: 200–600 ₽.'),
        ('CTR',
         'Кликабельность: процент пользователей, кликнувших на товар после просмотра. '
         'Нормальный CTR на WB: 3–6%. Зависит от главного фото карточки.'),
        ('CR',
         'Конверсия: процент пользователей, оформивших заказ после открытия карточки. '
         'Нормальный CR на WB: 5–12%. Зависит от описания, фото, цены, отзывов.'),
        ('ROI',
         'Возврат на инвестиции. Формула: (прибыль − затраты) / затраты × 100%. '
         'ROI 100% означает, что каждый вложенный рубль принёс ещё один рубль прибыли.'),
        ('MOQ',
         'Минимальный объём заказа (Minimum Order Quantity) у поставщика. '
         'Чем ниже MOQ — тем меньше рисковый тестовый заказ.'),
        ('Оборачиваемость',
         'Среднее число дней, за которое распродаётся товарный запас. '
         'До 45 дней — быстро. 45–90 дней — умеренно. Свыше 90 дней — медленно.'),
    ]

    rows = [['Термин', 'Значение']]
    for term, definition in terms:
        rows.append([term, definition])

    els.append(_tbl(rows, col_widths=[1.9 * inch, COL_W - 1.9 * inch]))
    els.append(_sp(0.1))
    return els


def _sec_toc_deep(sections: list) -> list:
    """Компактное содержание документа для Deep-уровня."""
    els = [_h2('Содержание документа'), _hr()]
    ts = ParagraphStyle('_toc', fontName=FN, fontSize=9, textColor=C_TEXT, leading=14)
    tb = ParagraphStyle('_tocb', fontName=FB, fontSize=9, textColor=C_NAVY, leading=14)
    rows = [['Раздел', 'Стр.']]
    for name, page_approx in sections:
        rows.append([name, f'~{page_approx}'])
    t = Table(rows, colWidths=[COL_W - 0.7 * inch, 0.7 * inch])
    t.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, 0), C_NAVY),
        ('FONTNAME',      (0, 0), (-1, 0), FB),
        ('FONTSIZE',      (0, 0), (-1, -1), 8.5),
        ('FONTNAME',      (0, 1), (-1, -1), FN),
        ('TEXTCOLOR',     (0, 0), (-1, 0), WHITE),
        ('GRID',          (0, 0), (-1, -1), 0.4, C_BORDER),
        ('TOPPADDING',    (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING',   (0, 0), (-1, -1), 8),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
        ('ROWBACKGROUNDS',(0, 1), (-1, -1), [WHITE, C_TABLE_ODD]),
        ('ALIGN',         (-1, 0), (-1, -1), 'CENTER'),
    ]))
    els.append(t)
    els.append(_sp(0.1))
    return els


def _sec_deep_value_block() -> list:
    """Блок уникальной ценности PDF Deep — тёмный с золотой рамкой."""
    bg   = HexColor('#1a2f5e')
    gold = HexColor('#f59e0b')
    head_s = ParagraphStyle('_dvb_h', fontName=FB, fontSize=12, textColor=gold,
                             alignment=TA_CENTER, leading=18)
    bull_s = ParagraphStyle('_dvb_b', fontName=FN, fontSize=9.5, textColor=WHITE,
                             leading=15, leftIndent=10)
    inner = [
        Paragraph('Только в PDF Deep — ваш полный стартовый пакет', head_s),
        Spacer(1, 8),
        Paragraph('✓ Глубокий анализ конкурентов: топ-игроки, их выручка и слабые места', bull_s),
        Paragraph('✓ Поиск поставщиков: цены Alibaba/1688, MOQ, прямые контакты', bull_s),
        Paragraph('✓ Полный список документов и сертификатов для выхода на WB', bull_s),
        Paragraph('✓ Стратегия поставок FBS/FBO с расчётом первой партии', bull_s),
        Paragraph('✓ Готовый AI-текст карточки товара для SEO-продвижения', bull_s),
    ]
    tbl = Table([[inner]], colWidths=[COL_W])
    tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), bg),
        ('TOPPADDING',    (0, 0), (-1, -1), 14),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 14),
        ('LEFTPADDING',   (0, 0), (-1, -1), 18),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 18),
        ('BOX',           (0, 0), (-1, -1), 2, gold),
    ]))
    return [_sp(0.15), tbl, _sp(0.15)]


def _sec_finale(level: str, agents: dict = None) -> list:
    """Финальная страница на тёмном фоне (фон через PageTemplate)."""
    accent   = LEVEL_ACCENT.get(level, C_ACCENT)
    badge_bg = LEVEL_BADGE_BG.get(level, HexColor('#1d4ed8'))

    def _wp(text, size=10, bold=False, color=WHITE, align=TA_CENTER,
            space_before=0, space_after=0):
        s = ParagraphStyle('_fp', fontName=FB if bold else FN, fontSize=size,
                            textColor=color, alignment=align,
                            spaceBefore=space_before, spaceAfter=space_after,
                            leading=size * 1.35)
        return Paragraph(str(text), s)

    els = []
    els.append(_sp(1.2))

    # Логотип
    els.append(_wp('WBAnalyzer', size=30, bold=True, color=WHITE))
    els.append(_sp(0.12))
    els.append(_wp('◆ AI Platform · Аналитика нового поколения',
                    size=9, color=C_COVER_SUB))
    els.append(_sp(0.3))

    # Тонкая акцентная линия
    acl = Table([['']], colWidths=[COL_W], rowHeights=[1.5])
    acl.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,-1), accent),
                               ('TOPPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),0)]))
    els.append(acl)
    els.append(_sp(0.35))

    # Текст
    els.append(_wp('Анализ подготовлен платформой WBAnalyzer', size=11, color=C_COVER_SUB))
    els.append(_sp(0.1))
    els.append(_wp(PLATFORM_URL, size=9, color=accent))

    # ── Апсейл для Basic и Standard ──────────────────────────────────────────
    upsell = LEVEL_UPSELL_NEXT.get(level)
    if upsell:
        next_name, next_desc, next_bg = upsell
        els.append(_sp(0.5))

        # Плашка "Хотите больше?"
        bump_s = ParagraphStyle('_bmp', fontName=FB, fontSize=9,
                                 textColor=HexColor('#0d1b2a'), alignment=TA_CENTER)
        bump = Table([[Paragraph('▲ БОЛЬШЕ ДАННЫХ — ЛУЧШЕ РЕШЕНИЕ ▲', bump_s)]],
                     colWidths=[COL_W])
        bump.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), accent),
            ('TOPPADDING',    (0,0), (-1,-1), 7),
            ('BOTTOMPADDING', (0,0), (-1,-1), 7),
        ]))
        els.append(bump)

        # Блок с предложением
        if level == 'basic':
            bullets = [
                'Все 3 графика ниши с подробными описаниями',
                'Топ-20 товаров с полной аналитикой',
                'Юнит-экономика в 3 сценариях (FBO BY / FBS / FBO RU)',
                'Рекламная стратегия с прогнозом KPI',
            ]
        else:  # standard
            bullets = [
                'Глубокий анализ конкурентной среды и ROI на 12 месяцев',
                'Поиск поставщиков: цены Alibaba/1688, MOQ, прямые ссылки',
                'Полный список документов и сертификатов для WB',
                'Стратегия поставок FBS/FBO + объём первой партии',
                'Готовый AI-текст карточки товара для SEO-продвижения',
            ]

        head_s  = ParagraphStyle('_fh', fontName=FB, fontSize=14, textColor=WHITE,
                                  alignment=TA_CENTER, leading=20)
        body_s  = ParagraphStyle('_fb', fontName=FN, fontSize=9,
                                  textColor=HexColor('#cbd5e1'), leading=14)
        bull_s  = ParagraphStyle('_fbl', fontName=FN, fontSize=9.5,
                                  textColor=WHITE, leading=14)

        inner_rows = [[Paragraph(f'Перейдите на {next_name}', head_s)]]
        inner_rows.append([Spacer(1, 6)])
        for b in bullets:
            inner_rows.append([Paragraph(f'✓  {b}', bull_s)])
        inner_rows.append([Spacer(1, 10)])
        inner_rows.append([Paragraph(
            f'Откройте WBAnalyzer и нажмите кнопку «{next_name}»',
            ParagraphStyle('_fcta', fontName=FB, fontSize=10,
                            textColor=accent, alignment=TA_CENTER, leading=14)
        )])

        blk = Table(inner_rows, colWidths=[COL_W - 32])
        blk.setStyle(TableStyle([
            ('TOPPADDING',    (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('LEFTPADDING',   (0,0), (-1,-1), 0),
            ('RIGHTPADDING',  (0,0), (-1,-1), 0),
        ]))
        wrap = Table([[blk]], colWidths=[COL_W])
        wrap.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), HexColor('#1e293b')),
            ('TOPPADDING',    (0,0), (-1,-1), 18),
            ('BOTTOMPADDING', (0,0), (-1,-1), 18),
            ('LEFTPADDING',   (0,0), (-1,-1), 16),
            ('RIGHTPADDING',  (0,0), (-1,-1), 16),
            ('BOX',           (0,0), (-1,-1), 1.5, accent),
        ]))
        els.append(wrap)

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

    # ── Параллельный запуск агентов (ThreadPoolExecutor — I/O bound) ───────────
    from concurrent.futures import ThreadPoolExecutor, as_completed

    task_map = {'master': lambda n: _run_master(n, level)}
    if level in ('standard', 'deep'):
        task_map['unit'] = _run_unit
        task_map['ads']  = _run_ads
    if level == 'deep':
        task_map['deep']      = _run_deep
        task_map['supplier']  = _run_supplier
        task_map['docs']      = _run_docs
        task_map['warehouse'] = _run_warehouse
        task_map['content']   = _run_content

    agents = {}
    content_text = ''
    print(f'[PDF] Запускаем {len(task_map)} агентов параллельно...')

    with ThreadPoolExecutor(max_workers=len(task_map)) as pool:
        futs = {pool.submit(fn, niche): name for name, fn in task_map.items()}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                result = fut.result(timeout=55)
                if name == 'content':
                    content_text = result if isinstance(result, str) else ''
                else:
                    agents[name] = result if isinstance(result, dict) else {}
            except Exception as e:
                print(f'[PDF] agent {name} error: {e}')
                if name != 'content':
                    agents[name] = {}

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

    els += _sec_top_products(items, limit=top_limit, level=level)
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

    els += _sec_conclusion(level, agents)
    els += _sec_upsell(level)

    doc.build(els)
    buf.seek(0)
    print(f'[PDF] Готово за {time.time()-t0:.1f}s, размер={len(buf.getvalue())} байт')
    return buf.getvalue()


# ── Раздельные точки входа (sequential mode) ──────────────────────────────────

_AGENT_FNS = {
    'master':    (_run_master,    'dict'),
    'unit':      (_run_unit,      'dict'),
    'ads':       (_run_ads,       'dict'),
    'deep':      (_run_deep,      'dict'),
    'supplier':  (_run_supplier,  'dict'),
    'docs':      (_run_docs,      'dict'),
    'warehouse': (_run_warehouse, 'dict'),
    'content':   (_run_content,   'str'),
}


def run_agent(name: str, niche: dict, level: str = 'standard'):
    """Запускает ОДИН агент. Вызывается из /pdf-stream."""
    entry = _AGENT_FNS.get(name)
    if entry is None:
        return {'error': f'unknown agent: {name}'}
    fn, ret_type = entry
    result = fn(niche, level) if name == 'master' else fn(niche)
    if ret_type == 'str':
        return {'text': result if isinstance(result, str) else ''}
    return result if isinstance(result, dict) else {}


def _sec_browser_charts(charts: dict, level: str, niche: dict = None) -> list:
    """Вставляет графики из браузера — полная ширина, одна на строку, с пояснением."""
    if not charts:
        return []
    n = niche or {}
    revenue   = float(n.get('revenue_annual', 0)) or float(n.get('revenue', 0)) / 2
    avg_price = float(n.get('avg_price', 0))
    sellers   = int(n.get('sellers', 0))
    sws       = int(n.get('sellers_with_sales', 0))
    turnover  = float(n.get('turnover', 0))
    act_pct   = round(sws / sellers * 100) if sellers else 0

    chart_meta = {
        'revenueChart': {
            'label': 'Динамика выручки',
            'desc': (f'Изменение месячной выручки ниши за последние 12 месяцев. '
                     f'Годовой объём рынка — {_rub(revenue)}. '
                     'Растущие столбцы указывают на расширение ниши.'),
        },
        'salesChart': {
            'label': 'Сезонность заказов',
            'desc': ('Динамика заказов по месяцам — ключ к планированию закупок и рекламных бюджетов. '
                     + (f'Оборачиваемость {turnover:.0f} дней: ' +
                        ('товар продаётся быстро.' if turnover <= 45 else
                         'продажи идут умеренно.' if turnover <= 90 else
                         'медленные продажи, планируйте запасы осторожно.')
                        if turnover else 'Закупайте товар заблаговременно перед пиком спроса.')),
        },
        'priceChart': {
            'label': 'Распределение цен',
            'desc': (f'Количество товаров в каждом ценовом диапазоне. '
                     f'Средний чек ниши — {_rub(avg_price)}. '
                     'Входите в диапазон с наибольшим количеством товаров — там концентрируется основной спрос.'),
        },
        'trendChart': {
            'label': 'Тренд ниши',
            'desc': ('Долгосрочный тренд изменения спроса. '
                     'Восходящий тренд — рынок растёт, это благоприятное время для входа. '
                     'Нисходящий — рынок сжимается, требуется осторожность.'),
        },
        'sellersChart': {
            'label': 'Доля продавцов',
            'desc': (f'Распределение выручки между продавцами. '
                     f'Активных продавцов {sws} из {sellers} ({act_pct}%). '
                     + ('Рынок высококонцентрирован: 3–5 игроков доминируют.' if act_pct < 15 else
                        'Рынок умеренно распределён — есть место для нового игрока.')),
        },
        'forecastChart': {
            'label': 'Прогноз выручки',
            'desc': ('Прогноз объёма ниши на ближайшие 3 месяца на основе исторической динамики. '
                     'Используйте как ориентир при планировании бюджета закупок и рекламы.'),
        },
    }

    chart_order = {
        'basic':    ['revenueChart', 'salesChart'],
        'standard': ['revenueChart', 'salesChart', 'priceChart', 'trendChart', 'forecastChart'],
        'deep':     ['revenueChart', 'salesChart', 'priceChart', 'trendChart', 'forecastChart'],
    }.get(level, list(chart_meta.keys()))

    els = [_h2('Графики ниши'), _hr()]
    found = 0
    for chart_id in chart_order:
        data_url = charts.get(chart_id, '')
        if not data_url:
            continue
        meta = chart_meta.get(chart_id, {'label': chart_id, 'desc': ''})
        try:
            _header, b64 = data_url.split(',', 1)
            img_bytes = __import__('base64').b64decode(b64)
            img_buf = io.BytesIO(img_bytes)
            img = Image(img_buf, width=COL_W, height=2.4*inch)
            img.hAlign = 'CENTER'
            els.append(_p(meta['label'], size=10, bold=True, color=C_NAVY,
                          align=TA_CENTER, space_before=8, space_after=2))
            els.append(img)
            if meta.get('desc'):
                els.append(_p(meta['desc'], size=8, color=C_GRAY,
                               align=TA_CENTER, space_before=3, space_after=2))
            els.append(_sp(0.12))
            found += 1
        except Exception as _e:
            print(f'[PDF] chart {chart_id} skip: {_e}')

    if found == 0:
        return []
    return els


def render(level: str, niche: dict, agents: dict,
           content_text: str = '', chart_items: list = None,
           charts: dict = None) -> bytes:
    """
    Собирает PDF из готовых результатов агентов — без вызовов Claude.
    charts: словарь {chart_id: base64_data_url} из браузера (canvas.toDataURL).
    """
    global _CURRENT_LEVEL
    _CURRENT_LEVEL = level

    t0 = time.time()
    items     = chart_items or []
    top_limit = 5 if level == 'basic' else 20

    niche_name = (niche.get('display_name') or niche.get('name') or 'Анализ ниши')[:55]
    lname      = LEVEL_NAMES.get(level, level.upper())
    accent     = LEVEL_ACCENT.get(level, C_ACCENT)

    buf = io.BytesIO()

    # ── Зоны страницы ─────────────────────────────────────────────────────────
    HEADER_ZONE = 0.68 * inch   # резервируется сверху для колонтитула
    FOOTER_ZONE = 0.52 * inch   # резервируется снизу

    # ── Callbacks для PageTemplate ─────────────────────────────────────────────
    def _on_dark(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_COVER_BG)
        canvas.rect(0, 0, W, H, fill=1, stroke=0)
        canvas.restoreState()

    def _on_content(canvas, doc):
        canvas.saveState()
        # ── Верхний колонтитул
        hY = H - 0.38 * inch
        canvas.setFont(FB, 7.5)
        canvas.setFillColor(C_NAVY)
        canvas.drawString(MARGIN, hY, f'WBAnalyzer  ·  {niche_name}')
        canvas.setFont(FN, 7.5)
        canvas.setFillColor(accent)
        canvas.drawRightString(W - MARGIN, hY, lname)
        # Линия под заголовком
        canvas.setStrokeColor(accent)
        canvas.setLineWidth(0.7)
        canvas.line(MARGIN, H - HEADER_ZONE + 0.04 * inch,
                    W - MARGIN, H - HEADER_ZONE + 0.04 * inch)
        # ── Нижний колонтитул
        fY = 0.22 * inch
        canvas.setFont(FN, 7)
        canvas.setFillColor(C_GRAY)
        canvas.drawString(MARGIN, fY,
                          f'© {PLATFORM_YEAR} WBAnalyzer · {PLATFORM_URL}')
        # Номер страницы: -1 чтобы обложка не считалась
        canvas.drawRightString(W - MARGIN, fY, f'Стр. {doc.page - 1}')
        canvas.setStrokeColor(C_BORDER)
        canvas.setLineWidth(0.4)
        canvas.line(MARGIN, fY + 0.13 * inch, W - MARGIN, fY + 0.13 * inch)
        canvas.restoreState()

    # ── Фреймы ────────────────────────────────────────────────────────────────
    full_frame = Frame(
        MARGIN, MARGIN,
        W - 2 * MARGIN, H - 2 * MARGIN,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id='full'
    )
    content_frame = Frame(
        MARGIN, FOOTER_ZONE,
        W - 2 * MARGIN, H - HEADER_ZONE - FOOTER_ZONE,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id='content'
    )

    doc = BaseDocTemplate(buf, pagesize=A4, pageTemplates=[
        PageTemplate(id='Cover',   frames=[full_frame],    onPage=_on_dark),
        PageTemplate(id='Content', frames=[content_frame], onPage=_on_content),
        PageTemplate(id='Finale',  frames=[full_frame],    onPage=_on_dark),
    ])

    # ── Контент ───────────────────────────────────────────────────────────────
    els = []

    # Обложка
    els += _sec_cover(niche, level)
    els.append(NextPageTemplate('Content'))
    els.append(PageBreak())

    # Deep: Содержание документа в начале
    if level == 'deep':
        deep_toc = [
            ('Ключевые показатели ниши',   2),
            ('Графики ниши',               3),
            ('Топ-20 товаров ниши',        4),
            ('Мастер-анализ AI',           5),
            ('Юнит-экономика',             6),
            ('Рекламная стратегия',        7),
            ('Глубокий анализ ниши',       8),
            ('Поиск поставщиков',          9),
            ('Документы и сертификаты',   10),
            ('Стратегия поставок',        11),
            ('Создание карточки товара',  12),
            ('Итоговый вывод',            13),
            ('Словарь терминов',          14),
        ]
        els += _sec_toc_deep(deep_toc)
        els += _sec_deep_value_block()

    # Контентные страницы
    els += _sec_metrics(niche)

    browser_charts = dict(charts or {})
    if browser_charts:
        els += _sec_browser_charts(browser_charts, level, niche)
    elif items:
        if len(items) >= 4:
            d = _chart_revenue_line(items)
            if d: els += [_h2('Топ товары по выручке'), d, _sp(0.1)]
        if level in ('standard', 'deep') and len(items) >= 6:
            d2 = _chart_price_bar(items)
            if d2: els += [_h2('Распределение цен'), d2, _sp(0.1)]

    els += _sec_top_products(items, limit=top_limit, level=level)
    els += _sec_master(agents.get('master') or {})

    if level in ('standard', 'deep'):
        els += _sec_unit(agents.get('unit') or {})
        els += _sec_ads(agents.get('ads') or {})

    if level == 'deep':
        els += _sec_deep(agents.get('deep') or {})
        els += _sec_supplier(agents.get('supplier') or {})
        els += _sec_docs(agents.get('docs') or {})
        els += _sec_warehouse(agents.get('warehouse') or {})
        els += _sec_content(content_text)

    els += _sec_conclusion(level, agents)

    # Словарь терминов
    els += _sec_glossary()

    # Финальная тёмная страница
    els.append(NextPageTemplate('Finale'))
    els.append(PageBreak())
    els += _sec_finale(level, agents)

    doc.build(els)
    buf.seek(0)
    pdf = buf.getvalue()
    print(f'[PDF-RENDER] Готово за {time.time()-t0:.1f}s, размер={len(pdf)} байт')
    return pdf
