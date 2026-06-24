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

import sourcing_intel as _si

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

def _tip(text: str):
    """Блок-совет эксперта: зелёный фон с левой полоской, тон «рекомендация»."""
    _TIP_BG     = HexColor('#f0fdf4')
    _TIP_BORDER = HexColor('#16a34a')
    s = ParagraphStyle('_tip', fontName=FN, fontSize=FS_CAPTION,
                       textColor=C_TEXT, leading=12, spaceBefore=0, spaceAfter=0)
    t = Table([[Spacer(5, 1), Paragraph(f'✓ {text}', s)]],
              colWidths=[5, COL_W - 5])
    t.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), _TIP_BG),
        ('BACKGROUND',    (0, 0), (0, -1),  _TIP_BORDER),
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


# ── Единая финансовая модель ───────────────────────────────────────────────────

def _compute_finance(n: dict) -> dict:
    """Детерминированный расчёт — одни цифры для всех агентов и разделов PDF."""
    avg_price  = max(float(n.get('avg_price', 0)), 1)
    buyout_pct = float(n.get('buyout_pct', 0.7)) or 0.7
    comm_raw   = float(n.get('commission', 0.25))
    commission = comm_raw if comm_raw <= 1 else comm_raw / 100

    cost   = round(avg_price * 0.35)              # себестоимость из Китая
    stor   = round(avg_price * 0.02)              # хранение WB (как в _run_unit)
    wb_c   = round(avg_price * commission)         # комиссия WB
    wb_l   = 120                                   # логистика WB, ₽/ед
    tax    = round(avg_price * 0.06)               # УСН 6%
    ret    = round(wb_l * (1 - buyout_pct) * 0.5) # возвраты
    profit = round(avg_price - cost - stor - wb_c - wb_l - tax - ret)
    margin = round(profit / avg_price * 100, 1) if avg_price else 0

    test_qty   = 20
    batch_cost = cost * test_qty
    entry      = round(batch_cost * 1.25)          # +25%: доставка, упаковка, буфер
    ad_monthly = max(15_000, round(avg_price * test_qty * 0.08))
    breakeven  = max(1, round(entry / profit)) if profit > 0 else 0
    pay_months = max(1, round(entry / (profit * test_qty))) if profit > 0 else 0
    roi        = round(profit * test_qty / entry * 100) if entry > 0 else 0

    return {
        'avg_price':         round(avg_price),
        'cost_per_unit':     cost,
        'wb_commission_rub': wb_c,
        'wb_logistics_rub':  wb_l,
        'tax_rub':           tax,
        'profit_per_unit':   profit,
        'margin_pct':        margin,
        'test_units':        test_qty,
        'test_batch_cost':   batch_cost,
        'entry_budget':      entry,
        'monthly_ad_budget': ad_monthly,
        'breakeven_units':   breakeven,
        'payback_months':    pay_months,
        'roi_pct':           roi,
        'roi_3months':       f'{roi}%',
    }


def _finance_block(fm: dict) -> str:
    """Строка с зафиксированными цифрами для инжекции в промпты агентов."""
    return (
        f"\nФИНАНСОВЫЕ ПАРАМЕТРЫ (ЗАФИКСИРОВАНЫ — используй в тексте ТОЛЬКО эти цифры):\n"
        f"Себестоимость: {fm['cost_per_unit']:,} ₽/ед | Прибыль/ед: {fm['profit_per_unit']:,} ₽ | Маржа: {fm['margin_pct']}%\n"
        f"Тест. партия: {fm['test_units']} шт = {fm['test_batch_cost']:,} ₽ | Бюджет входа (с буфером): {fm['entry_budget']:,} ₽\n"
        f"Реклама/мес: {fm['monthly_ad_budget']:,} ₽ | Точка безуб.: {fm['breakeven_units']} шт | "
        f"Окупаемость: {fm['payback_months']} мес | ROI: {fm['roi_3months']}\n"
    )


# ── Агенты (прямые вызовы Claude) ─────────────────────────────────────────────

def _run_master(n: dict, level: str = 'standard') -> dict:
    name = n.get('name', '')
    revenue = float(n.get('revenue_annual', 0)) or float(n.get('revenue', 0)) * 12
    avg_price = float(n.get('avg_price', 0))
    profit_pct = float(n.get('profit_pct', 0))
    buyout_pct = float(n.get('buyout_pct', 0))
    turnover = float(n.get('turnover', 0))
    sellers = int(n.get('sellers', 0))
    sws = int(n.get('sellers_with_sales', 0))
    act = round(sws / sellers * 100) if sellers else 0
    avg_rev = round(revenue / sws) if sws else 0

    fm = _compute_finance(n)

    avg_rev_monthly = round(avg_rev / 12) if avg_rev else 0
    base = (
        f"НИША: {name}\n"
        f"Выручка ниши: {revenue:,.0f} ₽/год ({round(revenue/12):,.0f} ₽/мес) | Средняя цена: {avg_price:,.0f} ₽\n"
        f"Маржа: {profit_pct*100:.0f}% | Выкуп: {buyout_pct*100:.0f}%\n"
        f"Оборачиваемость: {turnover:.0f} дней | Продавцов: {sellers} (активных: {sws}, {act}%)\n"
        f"Средняя выручка/продавец: {avg_rev:,.0f} ₽/год ({avg_rev_monthly:,.0f} ₽/мес)\n"
        + _finance_block(fm)
    )

    def _inject_fm(result: dict) -> dict:
        result['financial_model'] = {
            'test_batch_units':  fm['test_units'],
            'test_batch_cost':   fm['test_batch_cost'],
            'monthly_ad_budget': fm['monthly_ad_budget'],
            'breakeven_units':   fm['breakeven_units'],
            'roi_3months':       fm['roi_3months'],
            'payback_months':    str(fm['payback_months']),
        }
        return result

    if level == 'basic':
        prompt = (
            "Ты аналитик WB. Сделай КРАТКИЙ обзор ниши для базового отчёта.\n\n"
            + base +
            "\nПравила:\n"
            "- market_analysis: 3-4 предложения — насколько ниша привлекательна, объём, динамика. "
            "Без имён конкурентов и конкретных бюджетов.\n"
            "- competitive_landscape: 2-3 предложения — общий уровень конкуренции, плотность рынка.\n"
            "- final_recommendation: ОДНО предложение-вердикт — стоит ли входить и почему.\n"
            "- seasonal_plan: конкретные месяцы для пика, спада, закупки и старта рекламы.\n"
            "Ответь ONLY JSON:\n"
            '{"final_verdict":"ВХОДИТЬ|ТЕСТИРОВАТЬ|НЕ ВХОДИТЬ",'
            '"verdict_color":"#16a34a|#d97706|#dc2626",'
            '"market_analysis":"3-4 предложения",'
            '"competitive_landscape":"2-3 предложения",'
            '"final_recommendation":"одно предложение-вердикт",'
            '"seasonal_plan":{"peak":"месяц–месяц","low":"месяц–месяц","buy_date":"месяц","ad_date":"месяц"}}'
        )
        return _json(_claude(prompt, 800))

    if level == 'deep':
        prompt = (
            "Ты эксперт-аналитик WB. Сделай МАКСИМАЛЬНО ГЛУБОКИЙ развёрнутый анализ ниши.\n\n"
            + base +
            "\nПравила (ОБЯЗАТЕЛЬНО заполнить ВСЕ текстовые поля, не оставлять пустыми):\n"
            "- market_analysis: 5-6 предложений с конкретными цифрами объёма, динамики, сезонности и насыщенности рынка.\n"
            "- competitive_landscape: Детальный разбор топ-3 конкурентов с именами, выручкой, долей рынка, "
            "ценовыми диапазонами и слабыми местами каждого. 4-5 предложений.\n"
            "- entry_strategy: ОБЯЗАТЕЛЬНО начни каждый блок с «Месяц 1:», «Месяц 2:», «Месяц 3:». "
            "Для каждого месяца: конкретные действия, суммы (из ФИНАНСОВЫХ ПАРАМЕТРОВ), ожидаемый результат.\n"
            "- final_recommendation: Развёрнутый вывод с чёткими условиями входа, ключевыми метриками "
            "и планом масштабирования. Минимум 5-6 предложений.\n"
            "- opportunities: 4 конкретные рыночные возможности для входа.\n"
            "- risks: 3 основных риска с вероятностью и способом снижения.\n"
            "- deep_risks: 2-3 дополнительных специфических риска (не повторяй risks).\n"
            "ВАЖНО: используй только двойные кавычки в значениях JSON. Внутри строк используй угловые кавычки «» вместо прямых.\n"
            "Ответь ONLY JSON:\n"
            '{"final_verdict":"ВХОДИТЬ|ТЕСТИРОВАТЬ|НЕ ВХОДИТЬ",'
            '"verdict_color":"#16a34a|#d97706|#dc2626",'
            '"market_analysis":"5-6 предложений с цифрами...",'
            '"competitive_landscape":"4-5 предложений с именами конкурентов...",'
            '"entry_strategy":"Месяц 1: действия и бюджет. Месяц 2: действия. Месяц 3: результат.",'
            '"seasonal_plan":{"peak":"месяцы","low":"месяцы","buy_date":"дата","ad_date":"дата"},'
            '"opportunities":["возможность 1","возможность 2","возможность 3","возможность 4"],'
            '"risks":[{"risk":"риск","probability":"средняя","mitigation":"решение"},'
            '{"risk":"риск2","probability":"низкая","mitigation":"решение2"},'
            '{"risk":"риск3","probability":"высокая","mitigation":"решение3"}],'
            '"deep_risks":[{"risk":"риск","probability":"низкая","mitigation":"решение"},'
            '{"risk":"риск2","probability":"средняя","mitigation":"решение2"}],'
            '"final_recommendation":"5-6 предложений развёрнутого вывода..."}'
        )
        result = _inject_fm(_json(_claude(prompt, 4500)))
        if not result.get('market_analysis') and not result.get('final_verdict'):
            fallback = (
                f"Ниша WB: {name}. Кратко ответь ONLY JSON (не используй прямые кавычки внутри строк):\n"
                '{"final_verdict":"ВХОДИТЬ|ТЕСТИРОВАТЬ|НЕ ВХОДИТЬ",'
                '"verdict_color":"#16a34a|#d97706|#dc2626",'
                f'"market_analysis":"Выручка ниши {revenue:,.0f} рублей в год. Средняя цена {avg_price:,.0f} рублей. '
                f'Активных продавцов {sws}. Рынок показывает стабильный спрос с выраженной сезонностью.",'
                '"competitive_landscape":"Конкуренция в нише умеренная. Топ продавцы занимают большую долю рынка, однако есть место для новых игроков с качественным продуктом.",'
                '"entry_strategy":"Месяц 1: закупка тестовой партии и настройка карточки товара. Месяц 2: запуск рекламы и сбор первых отзывов. Месяц 3: масштабирование при положительном ROI.",'
                '"seasonal_plan":{"peak":"","low":"","buy_date":"","ad_date":""},'
                '"opportunities":[],"risks":[],"deep_risks":[],'
                '"final_recommendation":"Проверьте юнит-экономику в разделе ниже."}'
            )
            try:
                result = _inject_fm(_json(_claude(fallback, 800)))
            except Exception:
                result = _inject_fm({})
        return result

    # standard (default)
    prompt = (
        "Ты старший аналитик WB. Сделай полный развёрнутый анализ ниши.\n\n"
        + base +
        "\nПравила:\n"
        "- market_analysis: 3-4 предложения с конкретными цифрами.\n"
        "- competitive_landscape: Назови конкретных конкурентов по имени, их ценовые диапазоны, доли. "
        "2-3 предложения.\n"
        "- entry_strategy: Стратегия входа — ОБЯЗАТЕЛЬНО начни каждый блок с «Месяц 1:», «Месяц 2:», «Месяц 3:». "
        "Используй цифры из ФИНАНСОВЫХ ПАРАМЕТРОВ. Заканчивай последний блок: Поставщики, сертификаты и готовая карточка товара — только в PDF Deep.\n"
        "- final_recommendation: Полный абзац с чёткими условиями входа и метриками для принятия решения. "
        "4-5 предложений. Заканчивай: Для поиска поставщиков и полного пакета документов — PDF Deep.\n"
        "ВАЖНО: используй только двойные кавычки в значениях JSON. Внутри строк используй угловые кавычки «» вместо прямых.\n"
        "Ответь ONLY JSON:\n"
        '{"final_verdict":"ВХОДИТЬ|ТЕСТИРОВАТЬ|НЕ ВХОДИТЬ",'
        '"verdict_color":"#16a34a|#d97706|#dc2626",'
        '"market_analysis":"...",'
        '"competitive_landscape":"...",'
        '"entry_strategy":"...",'
        '"seasonal_plan":{"peak":"месяцы","low":"месяцы","buy_date":"дата","ad_date":"дата"},'
        '"opportunities":["возможность 1","возможность 2","возможность 3"],'
        '"risks":[{"risk":"риск","probability":"средняя","mitigation":"решение"}],'
        '"final_recommendation":"..."}'
    )
    result = _inject_fm(_json(_claude(prompt, 3000)))
    # Если главные поля пустые — retry с упрощённым промптом
    if not result.get('market_analysis') and not result.get('final_verdict'):
        fallback = (
            f"Ниша WB: {name}. Кратко ответь ONLY JSON (не используй прямые кавычки внутри строк):\n"
            '{"final_verdict":"ВХОДИТЬ|ТЕСТИРОВАТЬ|НЕ ВХОДИТЬ",'
            '"verdict_color":"#16a34a|#d97706|#dc2626",'
            f'"market_analysis":"Выручка ниши {revenue:,.0f} рублей в год. Средняя цена {avg_price:,.0f} рублей. Активных продавцов {sws}.",'
            '"competitive_landscape":"Конкуренция в нише.",'
            '"entry_strategy":"Месяц 1: подготовка. Месяц 2: закупка партии. Месяц 3: запуск продаж.",'
            '"seasonal_plan":{"peak":"","low":"","buy_date":"","ad_date":""},'
            '"opportunities":[],"risks":[],'
            '"final_recommendation":"Проверьте юнит-экономику в разделе ниже."}'
        )
        try:
            result = _inject_fm(_json(_claude(fallback, 800)))
        except Exception:
            result = _inject_fm({})
    return result


def _run_deep(n: dict, master_verdict: str = '') -> dict:
    name = n.get('name', '')
    revenue = float(n.get('revenue_annual', 0)) or float(n.get('revenue', 0)) * 12
    avg_price = float(n.get('avg_price', 0))
    commission = float(n.get('commission', 0))
    buyout_pct = float(n.get('buyout_pct', 0))
    profit_pct = float(n.get('profit_pct', 0))
    turnover = float(n.get('turnover', 0))
    sellers = int(n.get('sellers', 0))
    sws = int(n.get('sellers_with_sales', 0))
    rt = round(turnover / buyout_pct) if buyout_pct > 0 else round(turnover)
    avg_rev = revenue / sws if sws else 0

    fm = _compute_finance(n)

    verdict_hint = (
        f'ВАЖНО: мастер-анализ уже вынес вердикт «{master_verdict}». '
        f'Твой вердикт ДОЛЖЕН совпадать. Если есть причины расходиться — объясни в verdict_desc.\n'
    ) if master_verdict else ''

    prompt = (
        f"Ты эксперт по торговле на WB. Глубокий анализ ниши.\n\n"
        f"Ниша: {name} | Выручка ниши: {revenue:,.0f} ₽/год | Цена: {avg_price:,.0f} ₽\n"
        f"Продавцов: {sellers}, активных: {sws} | Комиссия: {commission:.0f}%\n"
        f"Выкуп: {buyout_pct*100:.0f}% | Оборачиваемость реальная: {rt} дней\n"
        f"Маржа: {profit_pct*100:.0f}% | Средняя выручка/продавец: {avg_rev:,.0f} ₽/год\n"
        + _finance_block(fm)
        + verdict_hint
        + "ONLY JSON (entry_budget/ad_budget/breakeven/roi_forecast будут заменены расчётными):\n"
        '{"verdict":"ВХОДИТЬ|ТЕСТИРОВАТЬ|НЕ ВХОДИТЬ",'
        '"verdict_desc":"обоснование 1-2 предложения",'
        '"financial_plan":"2-3 предложения с теми же цифрами что в ФИНАНСОВЫХ ПАРАМЕТРАХ",'
        '"competitive_analysis":"2-3 предложения",'
        '"free_segments":"свободные сегменты",'
        '"recommendation":"2-3 предложения",'
        '"season_peak_months":"месяцы пика","season_low_months":"месяцы спада",'
        '"purchase_months":"когда закупать","season_tip":"совет по сезонности"}'
    )
    result = _json(_claude(prompt, 1200))
    # Переопределяем финансовые поля — единые цифры для всех разделов
    result['entry_budget']  = fm['entry_budget']
    result['ad_budget']     = fm['monthly_ad_budget']
    result['breakeven']     = fm['breakeven_units']
    result['roi_forecast']  = fm['roi_3months']
    return result


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
        "Оцени ТОЛЬКО финансовую составляющую, не делай выводов о конкурентности ниши.\n"
        "ONLY JSON: "
        '{"title":"кратко о прибыльности","detail":"2-3 предложения с цифрами"}'
    )
    try:
        rec = _json(_claude(prompt, 300))
    except Exception:
        rec = {}
    # Сценарии считаются локально — возвращаем их даже если Claude не ответил
    return {'scenarios': scenarios, 'recommendation': rec,
            'buyout_pct': buyout_pct, 'turnover': float(n.get('turnover', 60) or 60),
            'test_batch': 20}


def _run_ads(n: dict) -> dict:
    name = n.get('name', '')
    avg_price = float(n.get('avg_price', 0))
    revenue = float(n.get('revenue_annual', 0)) or float(n.get('revenue', 0)) * 12
    profit_pct = float(n.get('profit_pct', 0))
    buyout_pct = float(n.get('buyout_pct', 0))
    commission = float(n.get('commission', 0))

    fm = _compute_finance(n)
    ad_start   = fm['monthly_ad_budget']
    ad_growth  = round(ad_start * 1.5)
    ad_sustain = round(ad_start * 2.0)

    prompt = (
        f"Ты рекламный аналитик WB.\n"
        f"НИША: {name} | Цена: {avg_price} ₽ | Выручка ниши: {revenue:,.0f} ₽/год\n"
        f"Маржа: {profit_pct*100:.1f}% | Выкуп: {buyout_pct*100:.1f}% | Комиссия: {commission*100:.1f}%\n"
        f"БЮДЖЕТ РЕКЛАМЫ (ЗАФИКСИРОВАН): Старт: {ad_start:,} ₽ | Рост: {ad_growth:,} ₽ | "
        f"Поддержание: {ad_sustain:,} ₽ — используй эти суммы в тексте.\n\n"
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
    try:
        result = _json(_claude(prompt, 1500))
    except Exception:
        result = {}
    # Бюджет зафиксирован — возвращаем всегда, даже если Claude не ответил
    b = result.get('budget') or {}
    b['start_rub']   = ad_start
    b['growth_rub']  = ad_growth
    b['sustain_rub'] = ad_sustain
    result['budget'] = b
    return result


def _run_competitors(n: dict) -> dict:
    name      = n.get('name', '')
    revenue   = float(n.get('revenue_annual', 0)) or float(n.get('revenue', 0)) * 12
    avg_price = float(n.get('avg_price', 0))
    sellers   = int(n.get('sellers', 0))
    sws       = int(n.get('sellers_with_sales', 0))

    fm = _compute_finance(n)

    prompt = (
        f"Ты аналитик конкурентной среды на Wildberries.\n\n"
        f"НИША: {name} | Выручка ниши: {revenue:,.0f} ₽/год | Средняя цена: {avg_price:,.0f} ₽\n"
        f"Продавцов: {sellers}, активных: {sws}\n"
        + _finance_block(fm) +
        "Правила:\n"
        "- top_sellers: 10 реальных крупных игроков в этой нише (типичные названия магазинов WB, выручка, рейтинг)\n"
        "- roi_forecast: период «Мес 1-3» используй entry_budget из ФИНАНСОВЫХ ПАРАМЕТРОВ как investment_rub\n"
        "- market_share_pct: доли должны суммироваться примерно в 60-80% (остальное — мелкие игроки)\n\n"
        "ONLY JSON:\n"
        '{"top_sellers":['
        '{"name":"Название магазина","revenue_monthly_rub":0,"products_count":0,'
        '"avg_rating":0.0,"market_share_pct":0.0,"weak_point":"слабое место"}],'
        '"weak_spots_summary":"общий анализ слабых мест конкурентов — 2-3 предложения",'
        '"free_segments":["незанятый сегмент 1","сегмент 2","сегмент 3"],'
        '"entry_window":"когда и как оптимально входить в нишу — 1-2 предложения",'
        '"roi_forecast":['
        '{"period":"Мес 1-3 (тест)","investment_rub":0,"revenue_rub":0,"profit_rub":0,"roi_pct":0},'
        '{"period":"Мес 4-6 (рост)","investment_rub":0,"revenue_rub":0,"profit_rub":0,"roi_pct":0},'
        '{"period":"Мес 7-12 (масштаб)","investment_rub":0,"revenue_rub":0,"profit_rub":0,"roi_pct":0}]}'
    )
    result = _json(_claude(prompt, 2000))
    # Фиксируем инвестицию тестовой фазы из единой финансовой модели
    rof = result.get('roi_forecast') or []
    if rof:
        rof[0]['investment_rub'] = fm['entry_budget']
    return result


def _run_supplier(n: dict) -> dict:
    """Определяет оптимальные каналы закупки с помощью sourcing_intel,
    затем просит Claude сгенерировать конкретные рекомендации по топ-странам."""
    name      = n.get('name', '')
    avg_price = float(n.get('avg_price', 0))

    # Сигнальный анализ — определяем топ-страны без стереотипов
    report = _si.analyze(name, avg_price, revenue=float(n.get('revenue', 0)))

    # Строим умный промпт с контекстом всех вариантов
    prompt = _si.build_sourcing_prompt(report, n)

    try:
        ai_data = _json(_claude(prompt, 1200))
    except Exception:
        ai_data = {}

    # Формируем унифицированный результат с данными из обоих источников
    search_queries = ai_data.get('search_queries', {})
    search_terms   = {**report.search_terms, **search_queries}

    # Строим search_links из топ-платформ по топ-стране
    search_links = []
    for opt in report.options[:3]:
        for plat in opt.platforms:
            url = _si.platform_url(plat, search_terms)
            search_links.append({
                'platform':    plat['name'],
                'country':     opt.label,
                'url':         url or '',
                'description': plat.get('note', ''),
                'moq':         plat.get('moq', ''),
            })

    return {
        # Данные из sourcing_intel (детерминированные)
        '_report':       report,
        'sourcing_options': [
            {
                'rank':         opt.rank,
                'country':      opt.label,
                'score':        opt.score,
                'customs_pct':  opt.logistics.get('customs_pct', 0),
                'logistics_rub': opt.logistics.get('logistics_rub', 0),
                'lead_time':    opt.logistics.get('lead_time_days', ''),
                'min_order_rub': opt.logistics.get('min_order_rub', 0),
                'risks':        opt.logistics.get('risks', ''),
                'certification': opt.logistics.get('certifications', ''),
                'platforms':    opt.platforms,
            }
            for opt in report.options
        ],
        # Данные из Claude (интерпретация и конкретные советы)
        'summary':        ai_data.get('summary', ''),
        'top_country':    ai_data.get('top_country', (report.options[0].label if report.options else '')),
        'supplier_tips':  ai_data.get('supplier_tips', []),
        'red_flags':      ai_data.get('red_flags', []),
        'first_order_rub': ai_data.get('first_order_rub', 0),
        'certification_note': ai_data.get('certification_note', ''),
        'search_links':   search_links,
        'search_queries': search_queries,
        # Совместимость со старым кодом _sec_docs
        'detected_category': report.signals.category,
        'price_tier':        report.signals.price_tier,
    }


def _run_docs(n: dict, supplier_data: dict = None) -> dict:
    name = n.get('name', '')
    avg_price = float(n.get('avg_price', 0))

    # Определяем источник закупки для точных рекомендаций по документам
    sourcing_context = "закупки в Китае"
    if supplier_data:
        top = supplier_data.get('top_country', '')
        cat = supplier_data.get('detected_category', '')
        if top:
            sourcing_context = f"закупки: {top}"
        if cat in ('food',):
            sourcing_context += ". ВАЖНО: еда требует ветсертификатов, деклараций о соответствии ТР ТС и ГОСТ"
        elif cat == 'kids':
            sourcing_context += ". ВАЖНО: детские товары — повышенные требования к сертификации"

    prompt = (
        f"Ты эксперт по сертификации для WB. Клиент из Беларуси, {sourcing_context}, продажи на WB.RU.\n"
        f"НИША: {name} | Средняя цена: {avg_price:.0f} ₽\n\n"
        "ONLY JSON:\n"
        '{"complexity":"low|medium|high",'
        '"wb_docs":[{"name":"документ","description":"зачем нужен","cost_rub":0,"duration_days":0,"required":true}],'
        '"customs_docs":["документ 1","документ 2"],'
        '"risks":[{"risk":"риск","solution":"как избежать"}],'
        '"blocking_risk":"ВЫСОКИЙ|СРЕДНИЙ|НИЗКИЙ",'
        '"blocking_reason":"конкретная причина почему карточку заблокируют без документов — 1-2 предложения",'
        '"belarus_specifics":{'
        '"cross_border_note":"особенности трансграничной торговли РБ→РФ через ЕАЭС для этой категории — 2-3 предложения",'
        '"key_docs":["специфический документ для Беларуси 1","документ 2","документ 3"]},'
        '"total_cost_rub":0,"total_duration_days":0}'
    )
    return _json(_claude(prompt, 1800))


def _run_warehouse(n: dict) -> dict:
    name = n.get('name', '')
    avg_price = float(n.get('avg_price', 0))
    revenue = float(n.get('revenue_annual', 0)) or float(n.get('revenue', 0)) * 12
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

def _chart_monthly_bar(items: list, mode: str = 'revenue',
                       width=COL_W, height=2.5*inch,
                       label: str = '') -> Drawing:
    """Агрегирует revenue_graph или sales_graph по месяцам и рисует вертикальные бары."""
    from datetime import date as _date, timedelta as _td
    if not items:
        return Drawing(width, height)

    start = _date.today() - _td(days=730)
    months: dict = {}
    for item in items:
        arr = item.get(f'{mode}_graph', []) or []
        for i, val in enumerate(arr):
            d = start + _td(days=i)
            key = (d.year, d.month)
            months[key] = months.get(key, 0.0) + float(val or 0)

    if not months:
        return Drawing(width, height)

    # Последние 12 полных месяцев
    sorted_keys = sorted(months.keys())[-13:-1] if len(months) >= 13 else sorted(months.keys())
    if not sorted_keys:
        return Drawing(width, height)

    values = [months[k] for k in sorted_keys]
    cat_names = [f'{k[0] % 100:02d}-{k[1]:02d}' for k in sorted_keys]

    max_v = max(values) if max(values) > 0 else 1
    if mode == 'revenue':
        fmt = lambda v: f'{v/1e6:.1f}M' if v >= 1e6 else f'{v/1e3:.0f}k'
    else:
        fmt = lambda v: f'{int(v)}'

    d = Drawing(width, height)
    chart = VerticalBarChart()
    chart.x = 45
    chart.y = 30
    chart.width = width - 60
    chart.height = height - 50
    chart.data = [values]
    chart.bars[0].fillColor = C_BLUE2
    chart.bars[0].strokeColor = C_BLUE2
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = max_v * 1.15
    chart.valueAxis.labelTextFormat = fmt
    chart.valueAxis.labels.fontSize = 7
    chart.categoryAxis.categoryNames = cat_names
    chart.categoryAxis.labels.angle = 40
    chart.categoryAxis.labels.dy = -10
    chart.categoryAxis.labels.fontSize = 7
    d.add(chart)
    if label:
        d.add(String(width / 2, height - 14, label,
                     fontSize=9, fontName=FB, fillColor=C_TEXT, textAnchor='middle'))
    return d


def _chart_sellers_table(items: list) -> list:
    """Заменяет сломанный pie-chart таблицей Бренд/Доля/Кол-во товаров в топ-20."""
    seller_rev: dict = {}
    seller_cnt: dict = {}
    for item in items:
        seller = str(item.get('brand') or item.get('brand_name') or
                     item.get('supplier') or 'Неизвестно')[:25]
        rev = float(item.get('revenue') or 0)
        seller_rev[seller] = seller_rev.get(seller, 0.0) + rev
        seller_cnt[seller] = seller_cnt.get(seller, 0) + 1

    if not seller_rev:
        return []

    total = sum(seller_rev.values()) or 1
    sorted_s = sorted(seller_rev.items(), key=lambda x: x[1], reverse=True)

    # Группируем <5% в "Другие"
    big = [(s, v) for s, v in sorted_s if v / total >= 0.05]
    small_rev = sum(v for _, v in sorted_s if v / total < 0.05)
    small_cnt = sum(seller_cnt.get(s, 0) for s, v in sorted_s if v / total < 0.05)
    if small_rev > 0:
        big.append(('Другие (<5% каждый)', small_rev))
        seller_cnt['Другие (<5% каждый)'] = small_cnt

    rows = [['Бренд / Продавец', 'Доля рынка', 'Товаров в топ-20']]
    for seller, rev in big:
        share = rev / total * 100
        cnt   = seller_cnt.get(seller, '—')
        rows.append([seller, f'{share:.1f}%', str(cnt)])

    t = _tbl(rows, col_widths=[3.0*inch, 1.3*inch, COL_W - 4.3*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,0), C_NAVY),
        ('FONTSIZE',      (0,0), (-1,-1), 8.5),
        ('TOPPADDING',    (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('LEFTPADDING',   (0,0), (-1,-1), 7),
        ('RIGHTPADDING',  (0,0), (-1,-1), 7),
        ('GRID',          (0,0), (-1,-1), 0.3, C_BORDER),
        ('ROWBACKGROUNDS',(0,1), (-1,-1), [WHITE, C_TABLE_ODD]),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
        ('ALIGN',         (1,0), (2,-1),  'CENTER'),
    ]))
    return [_h3('Распределение продавцов в топ-20'), t]


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
    # MPStats subjects/select всегда возвращает 30-дневный снимок (поле days=30).
    # revenue_monthly = raw 30-day figure; revenue_annual = * 12 (грубая оценка, пиковый месяц завышает).
    revenue_monthly = float(n.get('revenue', 0))
    revenue    = float(n.get('revenue_annual', 0)) or revenue_monthly * 12
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
        _card('ВЫРУЧКА НИШИ', _rub(revenue_monthly), f'в мес · ~{_rub(revenue)} /год', C_NAVY),
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

    # ── Оборачиваемость и Маржа — уровень-зависимые блоки ────────────────────
    _lvl = _CURRENT_LEVEL
    if turnover:
        els.append(_sp(0.1))
        if turnover > 365:
            # Критически высокая оборачиваемость — нужно объяснить что это значит
            years = round(turnover / 365, 1)
            warn_text = (
                f'<b>Оборачиваемость: {turnover:.0f} дней ({years} года)</b> — '
                f'это среднее время полного оборота склада по нише. '
                f'Значение выше 365 дней означает перенасыщение: в нише много медленно-продающегося товара. '
                f'Это НЕ запрет на вход — топ-продавцы могут оборачивать товар за 30–60 дней. '
                f'Стратегия: FBS + тестовая партия 15–20 шт, выбирайте подниши с активным спросом.'
            )
            els.append(_warning(warn_text))
        elif turnover > 90:
            if _lvl == 'basic':
                els.append(_tip(
                    f'<b>Оборачиваемость: {turnover:.0f} дн.</b> — оптимальная стратегия для этой ниши: '
                    f'FBS-схема + тестовая партия 15–20 шт. Именно так входят в нишу с долгим оборотом.'
                ))
            elif _lvl == 'standard':
                els.append(_tip(
                    f'<b>Оборачиваемость: {turnover:.0f} дней</b> — для такой ниши правильная стратегия: '
                    f'FBS-схема + тестовая партия 20–30 шт. Сначала подтверждаете спрос — потом масштабируете. '
                    f'Именно так успешные продавцы входят в нишу с долгим оборотом.'
                ))
            else:
                els.append(_tip(
                    f'<b>Оборачиваемость: {turnover:.0f} дней</b> — это сигнал работать по FBS-схеме '
                    f'с небольшими партиями. Именно так успешные продавцы входят в эту нишу: '
                    f'тест 20 шт → подтверждение спроса → переход на FBO для масштабирования. '
                    f'Детальный план — в разделе «Стратегия поставок».'
                ))
        elif turnover > 45:
            if _lvl == 'basic':
                els.append(_tip(f'<b>Оборачиваемость: {turnover:.0f} дн.</b> — хороший темп. Держите запас на 60–90 дней продаж.'))
            elif _lvl == 'standard':
                els.append(_tip(
                    f'<b>Оборачиваемость: {turnover:.0f} дней</b> — рабочий темп для ниши. '
                    f'Оптимальный запас: 60–90 дней продаж. Пополняйте партии заблаговременно, не дожидаясь обнуления остатков.'
                ))
            else:
                els.append(_tip(
                    f'<b>Оборачиваемость: {turnover:.0f} дней</b> — рабочий темп. '
                    f'Оптимальный запас: 60–90 дней продаж. При выходе в топ-50 по категории '
                    f'переходите на FBO — логистика WB улучшает позиции в поиске.'
                ))
        else:
            els.append(_tip(
                f'<b>Оборачиваемость: {turnover:.0f} дней</b> — отличный темп, товар быстро продаётся. '
                f'Следите чтобы остатки не падали до нуля — пустая карточка теряет позиции в поиске.'
            ))

    if profit > 0:
        els.append(_sp(0.06))
        if _lvl == 'basic':
            els.append(_info(
                f'<b>Маржа по WB: {_pct(profit)}</b> — операционная прибыль ниши после вычета комиссии '
                f'и логистики WB. Точный расчёт с учётом закупки — в юнит-экономике.'
            ))
        elif _lvl == 'standard':
            els.append(_info(
                f'<b>Маржа по WB: {_pct(profit)}</b> — операционная прибыль после вычета всех расходов WB. '
                f'Реальная чистая прибыль с учётом закупки — обычно 20–35% от цены. '
                f'Детальный расчёт по трём сценариям — в разделе «Юнит-экономика».'
            ))
        else:
            els.append(_info(
                f'<b>Маржа по WB: {_pct(profit)}</b> — операционная прибыль ниши после вычета всех расходов WB. '
                f'При вашей закупочной цене реальная чистая прибыль составит 20–35% — '
                f'точные цифры по трём сценариям (FBO BY / FBS / FBO RU) смотрите в разделе «Юнит-экономика».'
            ))

    els.append(_sp(0.12))
    return els


def _sec_top_products(items: list, limit: int = 20, level: str = 'standard') -> list:
    if not items:
        return []   # Нет данных — раздел не рендерится совсем (лучше чем пустая страница)

    els = [_h2(f'Топ-{min(limit, len(items))} товаров ниши'), _hr()]

    count = min(limit, len(items))
    total_rev = sum(float(it.get('revenue') or 0) for it in items)

    _WB_URL = 'https://www.wildberries.ru/catalog/{}/detail.aspx'
    _link_s = ParagraphStyle('_top_link', fontName=FN, fontSize=7,
                              textColor=C_BLUE2, leading=9,
                              spaceBefore=0, spaceAfter=0)
    _name_s = ParagraphStyle('_top_name', fontName=FN, fontSize=7.5,
                              textColor=C_TEXT, leading=10,
                              spaceBefore=0, spaceAfter=0, wordWrap='LTR')

    def _wb_link(sku_raw):
        sku = str(sku_raw).strip() if sku_raw else ''
        if sku and sku != '—' and sku.isdigit():
            url = _WB_URL.format(sku)
            return Paragraph(f'<link href="{url}"><u>{sku}</u></link>', _link_s)
        return Paragraph(sku or '—', _link_s)

    if level == 'basic':
        rows = [['#', 'Название товара', 'Цена, ₽', 'Выр./мес', 'Отзывы', 'Арт. WB']]
        for i, it in enumerate(items[:limit], 1):
            name  = Paragraph(str(it.get('name') or it.get('title') or '')[:50], _name_s)
            price = _rub(it.get('price') or it.get('final_price') or 0)
            rev   = _rub(it.get('revenue') or 0)
            fb    = str(int(it.get('feedbacks') or it.get('reviews') or
                             it.get('reviews_count') or 0))
            sku_v = it.get('id') or it.get('nm_id') or it.get('sku') or it.get('wb_sku')
            rows.append([str(i), name, price, rev, fb, _wb_link(sku_v)])
        cw = [0.25*inch, 2.85*inch, 0.78*inch, 0.9*inch, 0.58*inch, COL_W - 5.36*inch]
    else:
        # Standard/Deep: 8 колонок — Товар/Продавец/Цена/Выр/Продажи/Рейтинг/Артикул
        rows = [['#', 'Товар', 'Продавец', 'Цена', 'Выр./мес', 'Прод.', 'Рейт.', 'Арт. WB']]
        for i, it in enumerate(items[:limit], 1):
            name   = Paragraph(str(it.get('name') or it.get('title') or '')[:40], _name_s)
            seller = str(it.get('brand_name') or it.get('brand') or
                         it.get('seller') or it.get('supplier') or '—')[:16]
            price  = _rub(it.get('price') or it.get('final_price') or 0)
            rev    = _rub(it.get('revenue') or 0)
            sales_n = int(it.get('sales') or it.get('orders') or 0)
            sales  = str(sales_n) if sales_n else '—'
            rating = str(it.get('rating') or it.get('avg_rating') or '—')
            sku_v  = it.get('id') or it.get('nm_id') or it.get('sku') or it.get('wb_sku')
            rows.append([str(i), name, seller, price, rev, sales, rating, _wb_link(sku_v)])
        cw = [0.22*inch, 1.7*inch, 1.05*inch, 0.62*inch, 0.78*inch,
              0.52*inch, 0.48*inch, COL_W - 5.37*inch]

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

    # Аналитические наблюдения после таблицы (Standard/Deep)
    if level != 'basic' and len(items) >= 3:
        top3_rev  = sorted(items[:count], key=lambda x: float(x.get('revenue') or 0), reverse=True)[:3]
        leader    = top3_rev[0]
        lead_name = str(leader.get('name') or leader.get('title') or '')[:30]
        lead_rev  = float(leader.get('revenue') or 0)
        lead_share = lead_rev / total_rev * 100 if total_rev else 0

        prices  = [float(x.get('price') or x.get('final_price') or 0) for x in items[:count] if x.get('price') or x.get('final_price')]
        p_min   = min(prices) if prices else 0
        p_max   = max(prices) if prices else 0

        obs = []
        if lead_name and lead_rev > 0:
            obs.append(f'Лидер по выручке — <b>{lead_name}</b> ({_rub(lead_rev)}/мес, доля {lead_share:.1f}%).')
        if p_min and p_max:
            obs.append(f'Ценовой диапазон топ-{count}: от {_rub(p_min)} до {_rub(p_max)}.')
        if obs:
            obs.append('Войдите в середину ценового диапазона и предложите лучший контент карточки.')
            els.append(_sp(0.06))
            els.append(_p('  '.join(obs), size=8.5, color=C_TEXT, space_before=2))

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


def _sec_verdict_placard(master: dict) -> list:
    """Единственный вердикт-плакат в документе — размещается сразу после метрик."""
    verdict = str(master.get('final_verdict', '')).strip()
    if not verdict:
        return []
    vc = str(master.get('verdict_color', '#d97706')).strip()
    rec = str(master.get('final_recommendation', '')).strip()
    first_sentence = (rec.split('.')[0].strip() + '.') if rec else ''
    if '#16a34a' in vc or '#22c55e' in vc:
        bg, tc = HexColor('#f0fdf4'), C_GREEN
    elif '#dc2626' in vc or '#ef4444' in vc:
        bg, tc = HexColor('#fef2f2'), C_RED
    else:
        bg, tc = HexColor('#fffbeb'), C_AMBER
    inner = [_p(f'Вердикт AI: {verdict}', size=17, bold=True, color=tc, align=TA_CENTER)]
    if first_sentence:
        inner.append(_p(first_sentence, size=10, color=C_TEXT, align=TA_CENTER))
    tbl = Table([[inner]], colWidths=[COL_W])
    tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), bg),
        ('TOPPADDING',    (0, 0), (-1, -1), 14),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 14),
        ('LEFTPADDING',   (0, 0), (-1, -1), 14),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 14),
        ('BOX',           (0, 0), (-1, -1), 2, tc),
    ]))
    return [_sp(0.1), tbl, _sp(0.15)]


def _sec_master(r: dict, niche: dict = None) -> list:
    if not r:
        return []
    level = _CURRENT_LEVEL
    niche = niche or {}
    els = [_h2('Мастер-анализ'), _hr()]

    if level == 'basic':
        # Три коротких раздела — без конкретики и финансов
        for field, label in [
            ('market_analysis',      'Обзор ниши'),
            ('competitive_landscape', 'Конкуренция'),
            ('final_recommendation',  'Вердикт'),
        ]:
            txt = str(r.get(field, ''))
            if txt:
                els.append(_h3(label))
                els.append(_body(txt))
        # Серая курсивная подсказка
        upsell_s = ParagraphStyle('_mu', fontName=FN, fontSize=8.5,
                                   textColor=C_GRAY, alignment=TA_CENTER,
                                   leading=13, spaceBefore=8, spaceAfter=4)
        els.append(Paragraph(
            '<i>Детальный разбор конкурентов, стратегия входа и финансовая модель — в PDF Standard</i>',
            upsell_s))
    else:
        # Standard / Deep: полный набор разделов
        for field, label in [
            ('market_analysis',      'Анализ рынка'),
            ('competitive_landscape', 'Конкурентная среда'),
            ('entry_strategy',        'Стратегия входа'),
        ]:
            txt = str(r.get(field, ''))
            if txt:
                els.append(_h3(label))
                if field == 'entry_strategy':
                    els.extend(_render_entry_strategy(txt))
                else:
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
            els.append(_tbl(rows, col_widths=[2.6*inch, 1.1*inch, COL_W - 3.7*inch]))

    # Финансовая модель — только в Standard и Deep
    if level != 'basic':
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
        els.append(_tbl(rows, col_widths=[COL_W * 0.52, COL_W * 0.48]))

    if level != 'basic':
        sp = r.get('seasonal_plan') or {}
        if sp:
            els.append(_h3('Сезонный план закупок и рекламы'))
            els += _render_seasonal_table(sp)

    # Deep: расширенные риски + ROI 12-месяцев
    if level == 'deep':
        deep_risks = list(r.get('deep_risks') or [])
        if deep_risks:
            els.append(_h3('Дополнительные риски (Deep)'))
            _prob_colors = {'высокая': C_RED, 'средняя': C_AMBER, 'низкая': C_GREEN}
            dr_rows = [['Риск', 'Вероятность', 'Решение']]
            for risk in deep_risks[:3]:
                prob_str = str(risk.get('probability', '')).lower().strip()
                prob_cell = _p(prob_str.capitalize(), size=8, bold=True,
                               color=_prob_colors.get(prob_str, C_GRAY), align=TA_CENTER)
                dr_rows.append([str(risk.get('risk', '')), prob_cell, str(risk.get('mitigation', ''))])
            els.append(_tbl(dr_rows, col_widths=[2.6*inch, 1.1*inch, COL_W - 3.7*inch]))

        # ROI 12-month прогноз — реалистичный (от breakeven, не от доли ниши)
        fm = r.get('financial_model') or {}
        fm_merged = {**_FM_CANONICAL, **{k: v for k, v in fm.items() if v is not None and str(v).strip()}}
        avg_price       = float(niche.get('avg_price') or fm_merged.get('avg_price') or 1500)
        buyout_pct      = float(niche.get('buyout_pct') or fm_merged.get('buyout_pct') or 0.75)
        margin_pct      = 0.25   # консервативная маржа
        ad_budget       = float(fm_merged.get('monthly_ad_budget') or 45000)
        test_batch_cost = float(fm_merged.get('test_batch_cost') or 220000)
        # Breakeven — минимальные продажи для выхода в 0
        be_raw = fm_merged.get('breakeven_units')
        try:
            breakeven = max(1, int(str(be_raw).replace(',', '').strip())) if be_raw else 10
        except (ValueError, TypeError):
            breakeven = 10

        els.append(_h3('ROI-прогноз: 12 месяцев'))
        roi_rows = [['Месяц', 'Продажи, шт', 'Выручка', 'Затраты', 'Прибыль', 'ROI мес.']]
        for mo in range(1, 13):
            # Рост от точки безубыточности: мес1=be, мес2=1.5×be, мес3=2×be, мес4-6 +20%/мес, мес7-12 стабильно
            if mo == 1:
                sales_n = breakeven
            elif mo == 2:
                sales_n = max(breakeven, int(breakeven * 1.5))
            elif mo == 3:
                sales_n = max(breakeven, int(breakeven * 2.0))
            elif mo <= 6:
                prev = int(breakeven * 2.0 * (1.2 ** (mo - 3)))
                sales_n = prev
            else:
                sales_n = int(breakeven * 2.0 * (1.2 ** 3))   # стабильно на уровне мес6

            revenue        = sales_n * avg_price * buyout_pct
            init_invest    = test_batch_cost if mo == 1 else 0.0
            profit         = revenue * margin_pct - ad_budget - init_invest
            cogs           = revenue * (1 - margin_pct) + ad_budget + init_invest
            roi_month_pct  = profit / cogs * 100 if cogs != 0 else 0
            roi_cell = _p(f'{roi_month_pct:+.0f}%', size=7.5, bold=True,
                          color=C_GREEN if roi_month_pct > 0 else C_RED, align=TA_CENTER)
            roi_rows.append([
                f'Мес {mo:02d}',
                str(sales_n),
                _rub(revenue),
                _rub(cogs),
                _rub(profit),
                roi_cell,
            ])
        roi_t = _tbl(roi_rows,
                     col_widths=[0.65*inch, 0.8*inch, 1.1*inch, 1.1*inch, 1.1*inch,
                                 COL_W - 4.75*inch])
        roi_t.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, 0), C_NAVY),
            ('FONTSIZE',      (0, 0), (-1, -1), 7.5),
            ('TOPPADDING',    (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING',   (0, 0), (-1, -1), 4),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 4),
            ('GRID',          (0, 0), (-1, -1), 0.3, C_BORDER),
            ('ROWBACKGROUNDS',(0, 1), (-1, -1), [WHITE, C_TABLE_ODD]),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        els.append(roi_t)
        els.append(_p(
            '* ROI рассчитан за каждый месяц отдельно (прибыль / затраты × 100%). '
            'Месяц 1 включает единовременные затраты на тестовую партию.',
            size=7.5, color=C_GRAY, space_before=3
        ))

    # Итоговая рекомендация — в конце, после всех данных и прогнозов
    if level != 'basic':
        final_rec = str(r.get('final_recommendation', ''))
        if final_rec:
            els.append(_h3('Итоговая рекомендация'))
            els.append(_body(final_rec))

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
    if isinstance(rec, str):
        if rec:
            els.append(_body(rec))
            els.append(_sp(0.1))
        rec = {}
    else:
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

    els.append(_tbl(rows, col_widths=[COL_W - 3.9*inch, 1.3*inch, 1.3*inch, 1.3*inch]))
    els.append(_sp(0.1))

    # ── Deep: чувствительность + оборотный капитал ──────────────────────────
    if _CURRENT_LEVEL == 'deep' and scenarios:
        # Берём лучший сценарий для чувствительного анализа
        best = max(scenarios.values(), key=lambda s: float(s.get('margin_pct', 0) or 0))
        ap   = float(best.get('avg_price_used', 0) or 0)  # fallback
        cost = float(best.get('total_cost_rub', 0) or 0)
        prof = float(best.get('profit_per_unit_rub', 0) or 0)
        roi  = float(best.get('roi_pct', 0) or 0)
        mar  = float(best.get('margin_pct', 0) or 0)

        # Вычисляем цену из cost+profit (раз avg_price недоступен напрямую)
        # Приблизительно: price ≈ cost + wb_comm + wb_log + profit
        wb_comm = float(best.get('wb_commission_rub', 0) or 0)
        wb_log  = float(best.get('wb_logistics_rub', 0) or 0)
        approx_price = cost + wb_comm + wb_log + prof

        if approx_price > 0:
            els.append(_h3('Чувствительность к изменениям'))
            sens_rows = [['Сценарий', 'Новая маржа', 'Новый ROI', 'Прибыль/ед']]
            # Закупочная цена +10%
            cost_10 = cost * 1.1
            prof_10 = approx_price - cost_10 - wb_comm - wb_log
            mar_10  = prof_10 / approx_price * 100 if approx_price else 0
            roi_10  = prof_10 / cost_10 * 100 if cost_10 else 0
            sens_rows.append(['Закуп. цена +10%',
                               f'{mar_10:.1f}%', f'{roi_10:.0f}%', _rub(prof_10)])
            # Цена продажи -5%
            price_5  = approx_price * 0.95
            prof_5   = price_5 - cost - wb_comm - wb_log
            mar_5    = prof_5 / price_5 * 100 if price_5 else 0
            roi_5    = prof_5 / cost * 100 if cost else 0
            sens_rows.append(['Цена продажи -5%',
                               f'{mar_5:.1f}%', f'{roi_5:.0f}%', _rub(prof_5)])
            # Выкуп 70%
            buyout_orig = float(r.get('buyout_pct', 0.84) or 0.84)
            ret_orig = wb_log * (1 - buyout_orig) * 0.5
            ret_70   = wb_log * 0.30 * 0.5
            prof_70  = prof + ret_orig - ret_70
            mar_70   = prof_70 / approx_price * 100 if approx_price else 0
            sens_rows.append([f'Выкуп 70% (вместо {buyout_orig*100:.0f}%)',
                               f'{mar_70:.1f}%', f'{roi:.0f}%', _rub(prof_70)])
            els.append(_tbl(sens_rows, col_widths=[2.3*inch, 1.1*inch, 1.0*inch, COL_W-4.4*inch]))

        # Оборотный капитал
        els.append(_h3('Расчёт оборотного капитала'))
        turnover       = float(r.get('turnover', 60) or 60)
        frozen_capital = float(_FM_CANONICAL.get('test_batch_cost', 0) or 0)
        free_cashflow  = frozen_capital / turnover * 30 if turnover > 0 else 0
        reserve_2x     = frozen_capital * 2
        reserve_pct    = 30

        els.append(_body(
            f'Оборотный капитал — это деньги, которые постоянно «крутятся» в бизнесе: '
            f'часть заморожена в товаре на складе, часть приходит от продаж. '
            f'При оборачиваемости {turnover:.0f} дней ваш товар в среднем проводит '
            f'на складе {turnover:.0f} дней до продажи — значительная часть вложений '
            f'будет недоступна в течение этого срока.'
        ))
        wc_rows = [
            ['Показатель',                              'Значение'],
            ['Заморожено в товаре (тестовая партия)',   _rub(frozen_capital)],
            ['Свободный денежный поток',                f'≈ {_rub(free_cashflow)}/мес'],
            ['Рекомендуемый резерв (2 оборота)',        _rub(reserve_2x)],
        ]
        els.append(_tbl(wc_rows, col_widths=[COL_W * 0.58, COL_W * 0.42]))
        els.append(_sp(0.08))
        els.append(_warning(
            f'Не вкладывайте в товар все свободные деньги. Держите резерв минимум '
            f'{reserve_pct}% от стоимости партии на операционные расходы: реклама, '
            f'возвраты, непредвиденные затраты. При оборачиваемости свыше 90 дней '
            f'это особенно критично.'
        ))
        els.append(_sp(0.08))
        els.append(_tip(
            f'Как считать: перед каждым дозаказом убедитесь, что у вас есть свободные '
            f'средства на рекламный бюджет следующих {turnover:.0f} дней плюс стоимость '
            f'новой партии. Иначе вы окажетесь в ситуации, когда товар есть, деньги '
            f'заморожены в стоке, а рекламу запустить не на что.'
        ))

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

    # Шаги реализации — только в Deep (Standard ограничивается описанием стратегии + бюджетом)
    if _CURRENT_LEVEL == 'deep':
        steps = list(r.get('strategy_steps') or [])
        if steps:
            els.append(_h3('Шаги реализации'))
            for s in steps:
                els.append(_bullet(str(s)))

    budget = r.get('budget') or {}
    if budget:
        els.append(_h3('Бюджет рекламы'))
        rows = [['Фаза', 'Рекомендуемый бюджет, ₽', 'Диапазон по рынку']]
        ad_ranges = {
            'start_rub':   '10 000 – 30 000 ₽/мес',
            'growth_rub':  '30 000 – 80 000 ₽/мес',
            'sustain_rub': '50 000 – 120 000 ₽/мес',
        }
        for phase, label in [('start_rub','Старт'),('growth_rub','Рост'),('sustain_rub','Поддержание')]:
            if budget.get(phase):
                rows.append([label, _rub(budget[phase]), ad_ranges.get(phase, '')])
        if budget.get('comment'):
            rows.append(['Комментарий', str(budget['comment']), ''])
        els.append(_tbl(rows, col_widths=[1.3*inch, 1.8*inch, COL_W - 3.1*inch]))

    # Рекомендуемые каналы продвижения — Standard и Deep
    _ad_start = int((budget.get('start_rub') or 30000))
    _ad_day   = max(500, _ad_start // 30)
    _ad_traf  = int(_ad_start * 0.3)
    els.append(_h3('Рекомендуемые каналы продвижения'))
    els.append(_body(
        f'<b>Автоматическая реклама (Аукцион WB)</b> — основной канал на старте. '
        f'Ставка: авто, дневной бюджет {_rub(_ad_day)}/день. '
        f'Цель: CTR &gt;3%, ДРР &lt;25% в первый месяц.'
    ))
    els.append(_body(
        f'<b>Трафарет на конкурентов</b> — подключить с месяца 2, когда набрано 10+ отзывов. '
        f'Бюджет: {_rub(_ad_traf)}/мес (30% от общего рекламного бюджета).'
    ))
    els.append(_body(
        '<b>SEO-оптимизация</b> — параллельно с рекламой заполнить все характеристики карточки, '
        'добавить синонимы запросов в описание. Органика даёт 40–60% трафика к 3-му месяцу.'
    ))

    # CPM и KPI по месяцам — только в Deep
    if _CURRENT_LEVEL == 'deep':
        cpm = r.get('cpm_forecast') or {}
        _b = budget or {}
        _cpm_m = [
            int(cpm.get('start_rub', 350)),
            int(cpm.get('month2_rub', 320)),
            300,
        ]
        _budgets = [
            int(_b.get('start_rub', 30000)),
            int(_b.get('growth_rub', 45000)),
            int(_b.get('sustain_rub', 60000)),
        ]
        els.append(_h3('Прогноз CPM и показателей по месяцам'))
        cpm_rows = [['Период', 'CPM, ₽', 'Бюджет, ₽', 'Показы', 'Клики (CTR 4%)', 'Заказы (CR 8%)']]
        for mo, (label, cpm_v, bud) in enumerate([
            ('Месяц 1', _cpm_m[0], _budgets[0]),
            ('Месяц 2', _cpm_m[1], _budgets[1]),
            ('Месяц 3', _cpm_m[2], _budgets[2]),
        ]):
            shows  = int(bud * 1000 / cpm_v) if cpm_v else 0
            clicks = int(shows * 0.04)
            orders = int(clicks * 0.08)
            cpm_rows.append([label, _rub(cpm_v), _rub(bud), _num(shows), _num(clicks), _num(orders)])
        cpm_t = _tbl(cpm_rows, col_widths=[0.72*inch, 0.65*inch, 0.9*inch, 0.85*inch, 1.05*inch,
                                            COL_W - 4.17*inch])
        cpm_t.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,0), C_NAVY),
            ('FONTSIZE',      (0,0), (-1,-1), 8),
            ('TOPPADDING',    (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('LEFTPADDING',   (0,0), (-1,-1), 5),
            ('RIGHTPADDING',  (0,0), (-1,-1), 5),
            ('GRID',          (0,0), (-1,-1), 0.3, C_BORDER),
            ('ROWBACKGROUNDS',(0,1), (-1,-1), [WHITE, C_TABLE_ODD]),
            ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
        ]))
        els.append(cpm_t)
        if cpm.get('comment'):
            els.append(_p(str(cpm['comment']), size=7.5, color=C_GRAY, space_before=3))

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
        for mkey, mlabel in [('month1', 'Месяц 1 — KPI'),('month2', 'Месяц 2 — KPI')]:
            m = forecast.get(mkey) or {}
            if isinstance(m, str):
                # Если AI вернул строку, а не dict — показываем как обычный текст
                if m:
                    els.append(_h3(mlabel))
                    els.append(_body(m))
                continue
            metrics = list(m.get('metrics') or [])
            if metrics:
                els.append(_h3(mlabel))
                for metric in metrics:
                    els.append(_bullet(_expand_kpi(str(metric))))

    els.append(_sp(0.1))
    return els


def _level_divider(label: str, sublabel: str, color) -> list:
    """Тонкая полоса с меткой уровня — только в Deep PDF, между блоками контента."""
    s = ParagraphStyle('_ldiv', fontName=FB, fontSize=7.5, textColor=WHITE,
                       alignment=TA_LEFT, leading=11)
    tbl = Table([[Paragraph(f'  ▌  {label}  ·  {sublabel}', s)]],
                colWidths=[COL_W], rowHeights=[18])
    tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), color),
        ('TOPPADDING',    (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING',   (0, 0), (-1, -1), 6),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 6),
    ]))
    return [_sp(0.18), tbl, _sp(0.06)]


def _sec_competitors(r: dict) -> list:
    if not r:
        return []
    els = [PageBreak(), _h2('Анализ конкурентов'), _hr()]

    # Топ-10 продавцов
    sellers = list(r.get('top_sellers') or [])
    if sellers:
        els.append(_h3('Топ продавцов в нише'))
        _WEAK_COL_W = COL_W - 3.7*inch
        rows = [['Продавец', 'Выр./мес', 'Рейтинг', 'Доля %', 'Слабое место']]
        _ws = ParagraphStyle('_wsp', fontName=FN, fontSize=7, textColor=C_TEXT,
                              leading=10, spaceBefore=0, spaceAfter=0, wordWrap='LTR')
        for s in sellers[:10]:
            rows.append([
                str(s.get('name', ''))[:20],
                _rub(s.get('revenue_monthly_rub', 0)),
                str(s.get('avg_rating', '—')),
                f"{s.get('market_share_pct', 0):.1f}%",
                Paragraph(str(s.get('weak_point', '')), _ws),
            ])
        comp_tbl = _tbl(rows, col_widths=[1.1*inch, 0.9*inch, 0.6*inch, 0.55*inch, _WEAK_COL_W])
        comp_tbl.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,0), C_NAVY),
            ('FONTSIZE',      (0,0), (-1,0), 8),
            ('FONTSIZE',      (0,1), (-1,-1), 7.5),
            ('TOPPADDING',    (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('LEFTPADDING',   (0,0), (-1,-1), 5),
            ('RIGHTPADDING',  (0,0), (-1,-1), 5),
            ('GRID',          (0,0), (-1,-1), 0.3, C_BORDER),
            ('ROWBACKGROUNDS',(0,1), (-1,-1), [WHITE, C_TABLE_ODD]),
            ('VALIGN',        (0,0), (-1,-1), 'TOP'),
        ]))
        els.append(comp_tbl)

    # Слабые места конкурентов
    weak = str(r.get('weak_spots_summary', ''))
    if weak:
        els.append(_h3('Слабые места конкурентов'))
        els.append(_body(weak))

    # Свободные сегменты
    segs = list(r.get('free_segments') or [])
    if segs:
        els.append(_h3('Свободные сегменты рынка'))
        for seg in segs:
            els.append(_bullet(str(seg)))

    # Окно для входа
    window = str(r.get('entry_window', ''))
    if window:
        els.append(_tip(f'<b>Когда входить:</b> {window}'))

    # ROI прогноз 12 месяцев
    rof = list(r.get('roi_forecast') or [])
    if rof:
        els.append(_h3('ROI-прогноз: первые 12 месяцев'))
        rows = [['Период', 'Инвестиции, ₽', 'Выручка, ₽', 'Прибыль, ₽', 'ROI']]
        for row in rof:
            rows.append([
                str(row.get('period', '')),
                _rub(row.get('investment_rub', 0)),
                _rub(row.get('revenue_rub', 0)),
                _rub(row.get('profit_rub', 0)),
                f"{row.get('roi_pct', 0)}%",
            ])
        els.append(_tbl(rows, col_widths=[1.7*inch, 1.4*inch, 1.4*inch, 1.4*inch,
                                          COL_W - 5.9*inch]))

    els.append(_sp(0.1))
    return els


def _sec_competitors_merged(comp: dict, deep: dict) -> list:
    """Объединённый раздел конкурентов для Deep: _run_competitors + _run_deep в одном блоке."""
    if not comp and not deep:
        return []
    els = [_h2('Анализ конкурентов'), _hr()]

    # Сводная таблица входа (из _run_deep)
    if deep:
        entry  = deep.get('entry_budget', 0)
        ad_b   = deep.get('ad_budget', 0)
        be     = deep.get('breakeven', 0)
        roi    = deep.get('roi_forecast', '—')
        rows   = [['Бюджет входа', 'Бюджет рекламы/мес', 'Точка безуб.', 'ROI прогноз']]
        rows.append([_rub(entry), _rub(ad_b), _num(be) + ' шт', str(roi)])
        els.append(_tbl(rows, col_widths=[1.6*inch, 1.7*inch, 1.5*inch, COL_W - 4.8*inch]))
        els.append(_sp(0.08))

    # Топ-10 продавцов (из _run_competitors)
    sellers = list(comp.get('top_sellers') or [])
    if sellers:
        els.append(_h3('Топ продавцов в нише'))
        _WEAK_COL_W2 = COL_W - 3.7*inch
        rows = [['Продавец', 'Выр./мес', 'Рейтинг', 'Доля %', 'Слабое место']]
        _ws2 = ParagraphStyle('_wsp2', fontName=FN, fontSize=7, textColor=C_TEXT,
                               leading=10, spaceBefore=0, spaceAfter=0, wordWrap='LTR')
        for s in sellers[:10]:
            rows.append([
                str(s.get('name', ''))[:20],
                _rub(s.get('revenue_monthly_rub', 0)),
                str(s.get('avg_rating', '—')),
                f"{s.get('market_share_pct', 0):.1f}%",
                Paragraph(str(s.get('weak_point', '')), _ws2),
            ])
        comp_tbl2 = _tbl(rows, col_widths=[1.1*inch, 0.9*inch, 0.6*inch, 0.55*inch, _WEAK_COL_W2])
        comp_tbl2.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,0), C_NAVY),
            ('FONTSIZE',      (0,0), (-1,0), 8),
            ('FONTSIZE',      (0,1), (-1,-1), 7.5),
            ('TOPPADDING',    (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('LEFTPADDING',   (0,0), (-1,-1), 5),
            ('RIGHTPADDING',  (0,0), (-1,-1), 5),
            ('GRID',          (0,0), (-1,-1), 0.3, C_BORDER),
            ('ROWBACKGROUNDS',(0,1), (-1,-1), [WHITE, C_TABLE_ODD]),
            ('VALIGN',        (0,0), (-1,-1), 'TOP'),
        ]))
        els.append(comp_tbl2)

    # Где конкуренты проигрывают (из _run_competitors)
    weak = str(comp.get('weak_spots_summary', ''))
    if weak:
        els.append(_h3('Где конкуренты проигрывают'))
        els.append(_body(weak))

    # Расстановка сил на рынке (из _run_deep)
    if deep:
        ca = str(deep.get('competitive_analysis', ''))
        if ca:
            els.append(_h3('Расстановка сил на рынке'))
            els.append(_body(ca))

    # Где есть место для входа — объединяем free_segments из обоих источников
    fs   = str(deep.get('free_segments', '')) if deep else ''
    segs = list(comp.get('free_segments') or [])
    if fs or segs:
        els.append(_h3('Где есть место для входа'))
        if fs:
            els.append(_body(fs))
        if fs and segs:
            els.append(_hr())
        for seg in segs:
            els.append(_bullet(str(seg)))

    # Когда входить (из _run_competitors)
    window = str(comp.get('entry_window', ''))
    if window:
        els.append(_tip(f'<b>Когда входить:</b> {window}'))

    # ROI-прогноз 12 месяцев (из _run_competitors)
    rof = list(comp.get('roi_forecast') or [])
    if rof:
        els.append(_h3('ROI-прогноз: первые 12 месяцев'))
        rows = [['Период', 'Инвестиции', 'Выручка', 'Прибыль', 'ROI']]
        for row in rof:
            rows.append([
                str(row.get('period', '')),
                _rub(row.get('investment_rub', 0)),
                _rub(row.get('revenue_rub', 0)),
                _rub(row.get('profit_rub', 0)),
                f"{row.get('roi_pct', 0)}%",
            ])
        els.append(_tbl(rows, col_widths=[1.7*inch, 1.4*inch, 1.4*inch, 1.4*inch,
                                          COL_W - 5.9*inch]))

    # Финансовый план и рекомендация (из _run_deep)
    if deep:
        for field, label in [('financial_plan', 'Финансовый план'),
                              ('recommendation', 'Рекомендация')]:
            txt = str(deep.get(field, ''))
            if txt:
                els.append(_h3(label))
                els.append(_body(txt))

    els.append(_sp(0.1))
    return els


def _sec_supplier(r: dict) -> list:
    if not r:
        return []
    els = [PageBreak(), _h2('Стратегия закупки'), _hr()]

    # ── Сводная таблица по странам ─────────────────────────────
    options = list(r.get('sourcing_options') or [])
    if options:
        els.append(_h3('Оптимальные источники закупки для этой ниши'))
        rows = [['#', 'Страна', 'Тамож. пошлина', 'Логистика ₽/кг', 'Срок', 'Мин. партия']]
        for opt in options:
            customs = opt.get('customs_pct', 0)
            customs_str = f"{customs*100:.0f}%" if customs else "0% (ЕАЭС)"
            rows.append([
                str(opt.get('rank', '')),
                str(opt.get('country', '')),
                customs_str,
                f"~{opt.get('logistics_rub', 0)} ₽",
                str(opt.get('lead_time', ''))[:20],
                _rub(opt.get('min_order_rub', 0)),
            ])
        els.append(_tbl(rows, col_widths=[
            0.25*inch, 1.9*inch, 0.7*inch, 1.05*inch, 1.5*inch, COL_W - 5.4*inch
        ]))
        els.append(_p(
            '* 0% — в рамках ЕАЭС (Беларусь, Россия, Казахстан). '
            '10% — ввоз из Китая. 8% — ввоз из Турции. '
            'К пошлине добавляется НДС 20%.',
            size=7.5, color=C_GRAY, space_before=3,
        ))

    # ── Вывод от AI ────────────────────────────────────────────
    summary = str(r.get('summary', ''))
    if summary:
        els.append(_h3('Рекомендация эксперта'))
        els.append(_body(summary))

    # ── Детали по каждой стране + площадки ─────────────────────
    _link_s = ParagraphStyle('_lnk2', fontName=FN, fontSize=8,
                              textColor=C_BLUE2, leading=11,
                              spaceBefore=1, spaceAfter=1)
    _desc_s = ParagraphStyle('_ldsc2', fontName=FN, fontSize=8,
                              textColor=C_TEXT, leading=11,
                              spaceBefore=0, spaceAfter=0)

    for opt in options:
        country = str(opt.get('country', ''))
        risks   = str(opt.get('risks', ''))
        cert    = str(opt.get('certification', ''))
        plats   = list(opt.get('platforms') or [])

        if not plats:
            continue

        els.append(_h3(f'{country}'))
        if risks:
            els.append(_body(f'⚠ {risks}'))
        if cert:
            els.append(_body(f'📋 Документы: {cert}'))

        link_rows = [['Площадка', 'MOQ', 'Описание / Ссылка']]
        for plat in plats:
            url  = str(plat.get('url_tpl') or '')
            note = str(plat.get('note', ''))[:90]
            moq  = str(plat.get('moq', ''))
            name = str(plat.get('name', ''))
            if url and url.startswith('http'):
                short = (url[:48] + '…') if len(url) > 51 else url
                cell  = Paragraph(f'<link href="{url}"><u>{short}</u></link><br/>{note}', _link_s)
            else:
                cell = Paragraph(note or '—', _desc_s)
            link_rows.append([name, moq, cell])
        els.append(_tbl(link_rows, col_widths=[1.25*inch, 1.1*inch, COL_W - 2.35*inch]))

    # ── Поисковые запросы (от Claude) ─────────────────────────
    sq = r.get('search_queries') or {}
    sq_items = [(k.upper(), v) for k, v in sq.items() if v and v != r.get('_report', {}) and k in ('en', 'tr')]
    if sq_items:
        els.append(_h3('Поисковые запросы на других языках'))
        rows2 = [['Язык', 'Запрос для поиска']]
        for lang, q in sq_items:
            rows2.append([lang, str(q)])
        els.append(_tbl(rows2, col_widths=[0.6*inch, COL_W - 0.6*inch]))

    # ── Советы поиска ─────────────────────────────────────────
    tips = list(r.get('supplier_tips') or [])
    if tips:
        els.append(_h3('Как искать надёжного поставщика'))
        for t in tips:
            els.append(_bullet(str(t)))

    # ── Красные флаги ─────────────────────────────────────────
    red_flags = list(r.get('red_flags') or [])
    if red_flags:
        els.append(_h3('На что обратить внимание'))
        for f in red_flags:
            els.append(_body(f'🔴 {f}'))

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

    # Предупреждение о блокировке карточки
    blocking_reason = str(r.get('blocking_reason', ''))
    block_text = (
        blocking_reason if blocking_reason else
        'WB вправе заблокировать карточку в любой момент — даже если продажи уже идут. '
        'После блокировки восстановление занимает от 3 до 30 дней, продажи встают полностью.'
    )
    els.append(_sp(0.08))
    els.append(_warning(
        f'<b>Блокировка карточки:</b> {block_text} '
        '<b>Оформите все обязательные документы ДО первой поставки на склад WB.</b>'
    ))
    els.append(_sp(0.08))

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
        els.append(_tbl(rows, col_widths=[COL_W - 3.6*inch, 1.2*inch, 0.8*inch, 1.6*inch]))
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

    # Блок для продавцов из Беларуси
    by_specs = r.get('belarus_specifics') or {}
    by_note   = str(by_specs.get('cross_border_note', ''))
    by_docs   = list(by_specs.get('key_docs') or [])

    by_text = by_note if by_note else (
        'Продавцы из Беларуси работают в рамках ЕАЭС — товар из Китая растаможивается '
        'один раз на границе ЕАЭС, затем свободно перемещается в Россию без дополнительной таможни. '
        'Поставки на склады WB (Смоленск, Коледино) проходят как внутренняя торговля ЕАЭС.'
    )
    if not by_docs:
        by_docs = [
            'Свидетельство о государственной регистрации ИП/ООО (Республика Беларусь)',
            'Таможенная декларация импорта товара (Китай → ЕАЭС)',
            'Договор с ООО «Вайлдберриз» (юрисдикция РФ) — подписывается онлайн',
        ]

    els.append(_h3('Для продавцов из Беларуси'))
    els.append(_info(f'<b>Трансграничная торговля РБ → WB.RU (ЕАЭС):</b> {by_text}'))
    els.append(_sp(0.06))
    for d in by_docs:
        els.append(_bullet(str(d)))

    total_cost = r.get('total_cost_rub', 0)
    total_days = r.get('total_duration_days', 0)
    if total_cost or total_days:
        els.append(_sp(0.08))
        rows = [['Итого затраты', 'Итого срок']]
        rows.append([_rub(total_cost), f'{total_days} дней'])
        els.append(_tbl(rows, col_widths=[COL_W * 0.5, COL_W * 0.5]))

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
        els.append(_tbl(rows, col_widths=[1.5*inch, 1.7*inch, 1.7*inch, COL_W - 4.9*inch]))
        if stock.get('comment'):
            els.append(_body(str(stock['comment'])))

    wr = list(r.get('risks') or [])
    if wr:
        els.append(_h3('Риски логистики'))
        for risk in wr:
            els.append(_bullet(str(risk)))

    els.append(_sp(0.1))
    return els


def _render_entry_strategy(text: str) -> list:
    """Format entry_strategy: detect 'Месяц N:' labels and render as navy header blocks."""
    import re
    if not text:
        return []
    pattern = re.compile(r'(Месяц\s*\d+)', re.IGNORECASE | re.UNICODE)
    if not pattern.search(text):
        return [_body(text)]

    els = []
    tokens = pattern.split(text)
    # tokens: [pre_text, 'Месяц 1', 'content 1', 'Месяц 2', 'content 2', ...]
    if tokens[0].strip():
        els.append(_body(tokens[0].strip()))

    month_head_s = ParagraphStyle('_mhd', fontName=FB, fontSize=10,
                                   textColor=WHITE, alignment=TA_LEFT,
                                   leading=14, spaceBefore=0, spaceAfter=0)
    i = 1
    while i + 1 <= len(tokens) - 1:
        label = tokens[i].strip()
        content = tokens[i + 1].strip() if i + 1 < len(tokens) else ''
        # Strip leading colon/dash after the label
        content = re.sub(r'^[:\-–—\s]+', '', content).strip()

        head_tbl = Table([[Paragraph(f'▸  {label}', month_head_s)]], colWidths=[COL_W])
        head_tbl.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), C_NAVY),
            ('TOPPADDING',    (0, 0), (-1, -1), 7),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
            ('LEFTPADDING',   (0, 0), (-1, -1), 14),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 10),
        ]))
        els.append(_sp(0.08))
        els.append(head_tbl)
        if content:
            els.append(_body(content))
        i += 2
    return els


def _md_inline(text: str) -> str:
    """Convert inline markdown **bold** → <b>bold</b>, strip leftover backticks."""
    import re
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    return text


def _sec_content(text: str) -> list:
    if not text:
        return []
    els = [PageBreak(), _h2('Карточка товара'), _hr()]
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            els.append(_sp(0.04))
            continue
        # Skip pure markdown separators: | --- | --- | or ---
        stripped_sep = line.replace(' ', '').replace('-', '').replace('|', '').replace(':', '')
        if not stripped_sep:
            continue
        # ## Header / ### Header / # Header
        if line.startswith('### '):
            els.append(_h3(line[4:].strip()))
        elif line.startswith('## '):
            els.append(_h3(line[3:].strip()))
        elif line.startswith('# '):
            els.append(_h3(line[2:].strip()))
        # Numbered section: "1. SEO ЗАГОЛОВОК" or "1) ..."
        elif len(line) > 2 and line[0].isdigit() and line[1] in '.':
            rest = line[2:].strip()
            els.append(_h3(_md_inline(f'{line[0]}. {rest}') if rest else line))
        # Markdown table row: | col | col |
        elif line.startswith('|') and line.endswith('|'):
            cols = [c.strip() for c in line.strip('|').split('|')]
            row_text = '   |   '.join(_md_inline(c) for c in cols if c)
            if row_text:
                els.append(_body(row_text))
        # Bullet point: -, •, *
        elif line[:2] in ('- ', '• ', '* '):
            els.append(_bullet(_md_inline(line[2:])))
        # Emoji bullet: lines like "✅ text", "🔹 text"
        elif len(line) > 2 and ord(line[0]) > 127 and line[1] == ' ':
            els.append(_bullet(_md_inline(line)))
        # Standalone **bold** line (header-like)
        elif line.startswith('**') and line.endswith('**') and len(line) > 4:
            els.append(_h3(line[2:-2]))
        else:
            els.append(_body(_md_inline(line)))
    els.append(_sp(0.1))
    return els


def _render_seasonal_table(sp: dict) -> list:
    """Build a visual 12-month seasonal plan table from peak/low/buy_date/ad_date."""
    peak_str = (sp.get('peak') or '').lower()
    low_str  = (sp.get('low')  or '').lower()
    buy_str  = (sp.get('buy_date') or '').lower()
    ad_str   = (sp.get('ad_date')  or '').lower()

    MONTH_MAP = {
        'январ': 1, 'янв': 1, 'феврал': 2, 'фев': 2, 'март': 3, 'мар': 3,
        'апрел': 4, 'апр': 4, 'мая': 5, 'май': 5, 'июн': 6,
        'июл': 7, 'август': 8, 'авг': 8, 'сентябр': 9, 'сен': 9,
        'октябр': 10, 'окт': 10, 'ноябр': 11, 'ноя': 11, 'декабр': 12, 'дек': 12,
    }

    def _parse(text: str) -> set:
        found = set()
        for abbr, num in sorted(MONTH_MAP.items(), key=lambda x: -len(x[0])):
            if abbr in text:
                found.add(num)
        return found

    peak_m = _parse(peak_str)
    low_m  = _parse(low_str)
    buy_m  = _parse(buy_str)
    ad_m   = _parse(ad_str)

    MNAMES = ['Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн',
              'Июл', 'Авг', 'Сен', 'Окт', 'Ноя', 'Дек']

    rows = [['Месяц', 'Статус', 'Рекомендация']]
    status_list = []
    for i, mname in enumerate(MNAMES, 1):
        if i in peak_m:
            status = '🔥 Пик'
            action = 'Активные продажи, реклама +30–50%'
        elif i in low_m:
            status = '📉 Спад'
            action = 'Анализ сезона, подготовка, снижение стока'
        else:
            status = '→ Норма'
            action = 'Поддержание стока, умеренная реклама'
        notes = []
        if i in buy_m:
            notes.append('📦 закупка')
        if i in ad_m:
            notes.append('📢 старт рекламы')
        if notes:
            action += f'  ({", ".join(notes)})'
        rows.append([mname, status, action])
        status_list.append(status)

    t = Table(rows, colWidths=[0.65*inch, 0.82*inch, COL_W - 1.47*inch])
    cmds = [
        ('BACKGROUND',    (0, 0), (-1, 0), C_NAVY),
        ('TEXTCOLOR',     (0, 0), (-1, 0), WHITE),
        ('FONTNAME',      (0, 0), (-1, 0), FB),
        ('FONTSIZE',      (0, 0), (-1, -1), 8.5),
        ('FONTNAME',      (0, 1), (-1, -1), FN),
        ('TEXTCOLOR',     (0, 1), (-1, -1), C_TEXT),
        ('GRID',          (0, 0), (-1, -1), 0.4, C_BORDER),
        ('TOPPADDING',    (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING',   (0, 0), (-1, -1), 7),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 7),
        ('ROWBACKGROUNDS',(0, 1), (-1, -1), [WHITE, C_TABLE_ODD]),
    ]
    for ri, st in enumerate(status_list, 1):
        if '🔥' in st:
            cmds += [('BACKGROUND', (0, ri), (-1, ri), HexColor('#f0fdf4')),
                     ('TEXTCOLOR',  (1, ri), (1, ri), C_GREEN),
                     ('FONTNAME',   (1, ri), (1, ri), FB)]
        elif '📉' in st:
            cmds += [('BACKGROUND', (0, ri), (-1, ri), HexColor('#fef2f2')),
                     ('TEXTCOLOR',  (1, ri), (1, ri), C_RED)]
    t.setStyle(TableStyle(cmds))

    result = [t]
    buy_label = sp.get('buy_date') or ''
    ad_label  = sp.get('ad_date') or ''
    if buy_label or ad_label:
        result.append(_sp(0.05))
    if buy_label:
        result.append(_p(f'<b>Когда закупать:</b> {buy_label} — за 45–60 дней до пика',
                         size=8.5, space_before=2))
    if ad_label:
        result.append(_p(f'<b>Старт рекламы:</b> {ad_label} — за 2–3 недели до пика',
                         size=8.5, space_before=2))
    result.append(_sp(0.1))
    return result


def _sec_seasonal_plan(r: dict) -> list:
    """Сезонный план — простая 4-строчная таблица для Basic."""
    sp = r.get('seasonal_plan') or {}
    rows = [
        ['Пик продаж',    str(sp.get('peak', '—'))],
        ['Период спада',  str(sp.get('low',  '—'))],
        ['Когда закупать', str(sp.get('buy_date', '—'))],
        ['Старт рекламы', str(sp.get('ad_date', '—'))],
    ]
    t = Table(rows, colWidths=[2.2*inch, COL_W - 2.2*inch])
    t.setStyle(TableStyle([
        # Left column: always navy
        ('BACKGROUND',    (0, 0), (0, -1), C_NAVY),
        ('TEXTCOLOR',     (0, 0), (0, -1), WHITE),
        ('FONTNAME',      (0, 0), (0, -1), FB),
        # Right column: alternating rows
        ('BACKGROUND',    (1, 0), (1, -1), C_TABLE_ODD),
        ('BACKGROUND',    (1, 1), (1, 1), WHITE),
        ('BACKGROUND',    (1, 3), (1, 3), WHITE),
        ('FONTNAME',      (1, 0), (1, -1), FN),
        ('TEXTCOLOR',     (1, 0), (1, -1), C_TEXT),
        ('FONTSIZE',      (0, 0), (-1, -1), 9.5),
        ('TOPPADDING',    (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING',   (0, 0), (-1, -1), 10),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 10),
        ('GRID',          (0, 0), (-1, -1), 0.4, C_BORDER),
    ]))
    return [_h2('Сезонный план'), _hr(), t, _sp(0.1)]


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
        C_DARK2  = HexColor('#0f1e35')
        C_GOLD_B = HexColor('#f59e0b')
        C_BLUE_L = HexColor('#eff6ff')
        C_GRAY_L = HexColor('#f8fafc')

        els.append(_sp(0.2))

        # ── Главный заголовок «20%» ────────────────────────────────────────────
        tri_s = ParagraphStyle('_b20t', fontName=FB, fontSize=9,
                               textColor=C_DARK2, leading=13, alignment=TA_CENTER)
        h_s   = ParagraphStyle('_b20h', fontName=FB, fontSize=20,
                               textColor=WHITE, leading=26, alignment=TA_CENTER)
        sub_s = ParagraphStyle('_b20s', fontName=FN, fontSize=9,
                               textColor=HexColor('#cbd5e1'), leading=13, alignment=TA_CENTER)
        hdr_blk = Table([
            [Paragraph('◆  ВЫ ВИДЕЛИ ТОЛЬКО 20% АНАЛИЗА  ◆', tri_s)],
            [Spacer(1, 6)],
            [Paragraph('Хотите знать, выгодна ли эта ниша для вас лично?', h_s)],
            [Spacer(1, 4)],
            [Paragraph('PDF Standard покажет реальные цифры с учётом вашей схемы работы', sub_s)],
        ], colWidths=[COL_W])
        hdr_blk.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (0,0), C_GOLD_B),
            ('BACKGROUND',    (0,1), (0,-1), C_DARK2),
            ('TOPPADDING',    (0,0), (-1,-1), 8),
            ('BOTTOMPADDING', (0,0), (-1,-1), 8),
            ('LEFTPADDING',   (0,0), (-1,-1), 16),
            ('RIGHTPADDING',  (0,0), (-1,-1), 16),
        ]))
        els.append(hdr_blk)
        els.append(_sp(0.12))

        # ── Две колонки: что уже есть vs что откроет Standard ─────────────────
        col_head_s = ParagraphStyle('_bch', fontName=FB, fontSize=8.5,
                                    textColor=WHITE, leading=12, alignment=TA_CENTER)
        col_item_s = ParagraphStyle('_bci', fontName=FN, fontSize=8.5,
                                    textColor=C_TEXT, leading=13)
        hw = COL_W / 2 - 4

        def _col_items(items, icon):
            rows_inner = []
            for item in items:
                rows_inner.append([Paragraph(f'{icon}  {item}', col_item_s)])
            t = Table(rows_inner, colWidths=[hw])
            t.setStyle(TableStyle([
                ('TOPPADDING',    (0,0), (-1,-1), 4),
                ('BOTTOMPADDING', (0,0), (-1,-1), 4),
                ('LEFTPADDING',   (0,0), (-1,-1), 0),
                ('RIGHTPADDING',  (0,0), (-1,-1), 0),
            ]))
            return t

        left_head = Table([[Paragraph('✓  Что вы уже знаете', col_head_s)]],
                          colWidths=[hw])
        left_head.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), C_GRAY),
            ('TOPPADDING', (0,0), (-1,-1), 8), ('BOTTOMPADDING', (0,0), (-1,-1), 8),
            ('LEFTPADDING', (0,0), (-1,-1), 10), ('RIGHTPADDING', (0,0), (-1,-1), 10),
        ]))
        left_items = _col_items([
            'Ключевые показатели ниши',
            'Топ-5 товаров ниши',
            'Мастер-анализ AI',
            '2 графика ниши',
        ], '•')
        left_col = Table([[left_head], [left_items]], colWidths=[hw])
        left_col.setStyle(TableStyle([
            ('BACKGROUND',    (0,1), (-1,-1), C_GRAY_L),
            ('TOPPADDING',    (0,0), (-1,-1), 0), ('BOTTOMPADDING', (0,0), (-1,-1), 0),
            ('LEFTPADDING',   (0,0), (-1,-1), 10), ('RIGHTPADDING', (0,0), (-1,-1), 10),
            ('TOPPADDING',    (0,1), (-1,-1), 8), ('BOTTOMPADDING', (0,1), (-1,-1), 10),
            ('BOX',           (0,0), (-1,-1), 0.5, C_BORDER),
        ]))

        right_head = Table([[Paragraph('+ Что откроет PDF Standard', col_head_s)]],
                           colWidths=[hw])
        right_head.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), C_ACCENT),
            ('TOPPADDING', (0,0), (-1,-1), 8), ('BOTTOMPADDING', (0,0), (-1,-1), 8),
            ('LEFTPADDING', (0,0), (-1,-1), 10), ('RIGHTPADDING', (0,0), (-1,-1), 10),
        ]))
        right_items = _col_items([
            'Юнит-экономика — прибыль с каждой единицы',
            'Рекламная стратегия с прогнозом KPI',
            'Топ-20 товаров ниши',
            'Все 6 графиков ниши',
            'Конкуренты, цены, точка входа',
        ], '+')
        right_col = Table([[right_head], [right_items]], colWidths=[hw])
        right_col.setStyle(TableStyle([
            ('BACKGROUND',    (0,1), (-1,-1), C_BLUE_L),
            ('TOPPADDING',    (0,0), (-1,-1), 0), ('BOTTOMPADDING', (0,0), (-1,-1), 0),
            ('LEFTPADDING',   (0,0), (-1,-1), 10), ('RIGHTPADDING', (0,0), (-1,-1), 10),
            ('TOPPADDING',    (0,1), (-1,-1), 8), ('BOTTOMPADDING', (0,1), (-1,-1), 10),
            ('BOX',           (0,0), (-1,-1), 0.5, C_BORDER),
        ]))

        two_cols = Table([[left_col, Spacer(8, 1), right_col]],
                         colWidths=[hw, 8, hw])
        two_cols.setStyle(TableStyle([
            ('TOPPADDING',    (0,0), (-1,-1), 0),
            ('BOTTOMPADDING', (0,0), (-1,-1), 0),
            ('LEFTPADDING',   (0,0), (-1,-1), 0),
            ('RIGHTPADDING',  (0,0), (-1,-1), 0),
            ('VALIGN',        (0,0), (-1,-1), 'TOP'),
        ]))
        els.append(two_cols)
        els.append(_sp(0.12))

        # ── Фраза про юнит-экономику ───────────────────────────────────────────
        unit_s = ParagraphStyle('_bue', fontName=FB, fontSize=9.5,
                                textColor=HexColor('#92400e'), leading=14, alignment=TA_CENTER)
        unit_sub_s = ParagraphStyle('_bues', fontName=FN, fontSize=8.5,
                                    textColor=HexColor('#78350f'), leading=13, alignment=TA_CENTER)
        unit_blk = Table([
            [Paragraph('В Standard вы получите юнит-экономику — расчёт реальной прибыли с каждой единицы товара.', unit_s)],
            [Spacer(1, 3)],
            [Paragraph('Без этого расчёта входить в нишу — значит работать вслепую.', unit_sub_s)],
        ], colWidths=[COL_W - 32])
        unit_blk.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), HexColor('#fffbeb')),
            ('TOPPADDING',    (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('LEFTPADDING',   (0,0), (-1,-1), 0),
            ('RIGHTPADDING',  (0,0), (-1,-1), 0),
        ]))
        unit_wrap = Table([[unit_blk]], colWidths=[COL_W])
        unit_wrap.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), HexColor('#fffbeb')),
            ('BOX',           (0,0), (-1,-1), 2, C_GOLD_B),
            ('TOPPADDING',    (0,0), (-1,-1), 12),
            ('BOTTOMPADDING', (0,0), (-1,-1), 12),
            ('LEFTPADDING',   (0,0), (-1,-1), 16),
            ('RIGHTPADDING',  (0,0), (-1,-1), 16),
        ]))
        els.append(unit_wrap)
        els.append(_sp(0.12))

        # ── CTA ────────────────────────────────────────────────────────────────
        cta_s = ParagraphStyle('_bcta', fontName=FB, fontSize=13,
                               textColor=C_DARK2, leading=18, alignment=TA_CENTER)
        cta_sub_s = ParagraphStyle('_bctas', fontName=FN, fontSize=9,
                                   textColor=C_DARK2, leading=13, alignment=TA_CENTER)
        cta_blk = Table([
            [Paragraph('▶  Откройте WBAnalyzer и нажмите PDF Standard', cta_s)],
            [Paragraph('и получите полный расчёт прямо сейчас', cta_sub_s)],
        ], colWidths=[COL_W])
        cta_blk.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), C_GOLD_B),
            ('TOPPADDING',    (0,0), (-1,-1), 12),
            ('BOTTOMPADDING', (0,0), (-1,-1), 12),
            ('LEFTPADDING',   (0,0), (-1,-1), 12),
            ('RIGHTPADDING',  (0,0), (-1,-1), 12),
        ]))
        els.append(cta_blk)

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
            [Paragraph('Остался последний шаг —', head_s)],
            [Paragraph('готовый план старта в нише', head_s)],
            [Spacer(1, 4)],
            [Paragraph('PDF Deep · 5 дополнительных разделов · Готовые данные для старта прямо сейчас', sub_s)],
        ], colWidths=[COL_W])
        head_blk.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), C_DEEP),
            ('TOPPADDING',    (0,0), (-1,-1), 14),
            ('BOTTOMPADDING', (0,0), (-1,-1), 14),
            ('LEFTPADDING',   (0,0), (-1,-1), 12),
            ('RIGHTPADDING',  (0,0), (-1,-1), 12),
        ]))
        els.append(head_blk)

        # ── Фраза про карточку товара ──────────────────────────────────────────
        key_s   = ParagraphStyle('_up_key', fontName=FB, fontSize=9.5,
                                  textColor=C_DEEP, leading=14, alignment=TA_CENTER)
        key_sub = ParagraphStyle('_up_ks', fontName=FN, fontSize=8.5,
                                  textColor=HexColor('#4c1d95'), leading=13, alignment=TA_CENTER)
        key_blk = Table([
            [Paragraph('В Deep вы получите готовую карточку товара — скопируйте напрямую на WB.', key_s)],
            [Spacer(1, 3)],
            [Paragraph(
                'Плюс контакты поставщиков с ценами и список документов — '
                'всё что нужно чтобы начать торговать на следующей неделе.',
                key_sub)],
        ], colWidths=[COL_W - 32])
        key_blk.setStyle(TableStyle([
            ('TOPPADDING',    (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3),
            ('LEFTPADDING',   (0,0), (-1,-1), 0), ('RIGHTPADDING',  (0,0), (-1,-1), 0),
        ]))
        key_wrap = Table([[key_blk]], colWidths=[COL_W])
        key_wrap.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), C_PURP2),
            ('BOX',           (0,0), (-1,-1), 2, C_PURP),
            ('TOPPADDING',    (0,0), (-1,-1), 12),
            ('BOTTOMPADDING', (0,0), (-1,-1), 12),
            ('LEFTPADDING',   (0,0), (-1,-1), 16),
            ('RIGHTPADDING',  (0,0), (-1,-1), 16),
        ]))
        els.append(key_wrap)
        els.append(_sp(0.1))

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
            [Paragraph('▶  Откройте WBAnalyzer и нажмите PDF Deep', cta_s)],
            [Paragraph('и получите готовый план старта прямо сейчас', cta_sub)],
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

    # Компактный словарь — две колонки, 8pt
    gl_s = ParagraphStyle('_gl_toc', fontName=FN, fontSize=8, textColor=C_TEXT, leading=11)
    terms = [
        ('FBO',             'хранение и отгрузка со склада WB'),
        ('FBS',             'хранение у продавца, отгрузка при заказе'),
        ('ДРР',             'доля рекламных расходов = реклама / выручка'),
        ('CPM',             'цена 1 000 показов рекламы'),
        ('CTR',             'кликабельность = клики / показы'),
        ('CR',              'конверсия = заказы / посетители'),
        ('ROI',             'возврат инвестиций = прибыль / вложения'),
        ('MOQ',             'минимальный заказ у поставщика'),
        ('Оборачиваемость', 'дней до продажи товара со склада'),
        ('Маржа',           'прибыль / выручка × 100%'),
    ]
    left_t  = terms[:5]
    right_t = terms[5:]
    half    = (COL_W - 0.1 * inch) / 2
    g_rows  = []
    for lt, rt in zip(left_t, right_t):
        g_rows.append([
            Paragraph(f'<b>{lt[0]}</b>', gl_s), Paragraph(lt[1], gl_s),
            Paragraph(f'<b>{rt[0]}</b>', gl_s), Paragraph(rt[1], gl_s),
        ])
    gt = Table(g_rows, colWidths=[0.85 * inch, half - 0.85 * inch,
                                   0.85 * inch, half - 0.85 * inch])
    gt.setStyle(TableStyle([
        ('TOPPADDING',    (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING',   (0, 0), (-1, -1), 5),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 5),
        ('ROWBACKGROUNDS',(0, 0), (-1, -1), [WHITE, C_TABLE_ODD]),
        ('LINEABOVE',     (0, 0), (-1, 0),  0.4, C_BORDER),
        ('LINEBELOW',     (0, -1), (-1, -1), 0.4, C_BORDER),
        ('LINEBEFORE',    (2, 0), (2, -1),  0.4, C_BORDER),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    els.append(_p('Основные термины', size=9, bold=True, color=C_NAVY,
                   space_before=10, space_after=4))
    els.append(gt)
    els.append(_sp(0.05))
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


def _sec_30days(niche: dict = None, fm: dict = None) -> list:
    """90-дневный план запуска — на светлом фоне (NoBar template)."""
    niche = niche or {}
    fm    = fm or {}

    # Числа из ниши
    niche_name   = str(niche.get('name') or 'нише')
    avg_price    = float(niche.get('avg_price') or fm.get('avg_price') or 1500)
    buyout_pct   = float(niche.get('buyout_pct') or fm.get('buyout_pct') or 0.75)
    turnover     = float(niche.get('turnover') or fm.get('turnover') or 60)
    sellers_cnt  = int(niche.get('sellers_with_sales') or fm.get('sellers_with_sales') or 150)
    monthly_rev  = float(niche.get('revenue') or 0)
    target_sales = max(5, int(monthly_rev / avg_price / sellers_cnt * 0.5)) if (monthly_rev and avg_price and sellers_cnt) else 25
    target_rev   = int(target_sales * avg_price * buyout_pct)

    test_batch   = int(fm.get('test_batch_units') or 20)
    test_cost    = int(fm.get('test_batch_cost') or int(test_batch * avg_price * 0.35))
    ad_budget_mo = int(fm.get('monthly_ad_budget') or 45000)
    ad_budget_wk = int(ad_budget_mo / 4)
    reorder_qty  = int(test_batch * 2.5)
    reorder_cost = int(reorder_qty * avg_price * 0.35)

    C_BLUE_H = HexColor('#1e40af')
    C_STEP   = [HexColor('#dbeafe'), HexColor('#dcfce7'),
                HexColor('#fef9c3'), HexColor('#ede9fe'), HexColor('#fee2e2')]
    C_STEP_H = [HexColor('#1e40af'), HexColor('#15803d'),
                HexColor('#92400e'), HexColor('#6d28d9'), HexColor('#b91c1c')]

    els = [
        _h2('90-дневный план запуска'),
        _hr(),
        _p(
            f'Детальная дорожная карта входа в нишу <b>{niche_name}</b>. '
            f'Цель к концу 90 дней — {target_sales} продаж/мес и выручка '
            f'{_rub(target_rev)}/мес после выкупа ({int(buyout_pct*100)}%).',
            size=9.5, color=C_TEXT, space_before=2, space_after=6,
        ),
    ]

    # 5 блоков: заголовок, срок, задачи, метрики успеха
    blocks = [
        {
            'idx': 0,
            'label': 'БЛОК 1 — Подготовка и разведка',
            'period': 'Дни 1–14',
            'tasks': [
                f'Зарегистрировать ИП (ОКВЭД 47.91) или оформить самозанятость — нужен личный кабинет WB.',
                f'Собрать топ-20 конкурентов в нише «{niche_name}»: цена, отзывы, контент карточки.',
                f'Найти 3–5 поставщиков на 1688.com / Alibaba / через российских посредников.',
                f'Запросить образцы (по 1–2 шт от каждого) и прайс-лист с MOQ.',
                f'Рассчитать юнит-экономику: целевая цена ≈ {_rub(avg_price)}, '
                f'выкуп {int(buyout_pct*100)}%, тестовая партия {test_batch} шт = {_rub(test_cost)}.',
            ],
            'kpi': f'Выбран поставщик, подписан контракт, известна точная себестоимость.',
        },
        {
            'idx': 1,
            'label': 'БЛОК 2 — Тестовая партия',
            'period': 'Дни 15–30',
            'tasks': [
                f'Заказать тестовую партию: {test_batch} шт, бюджет ≈ {_rub(test_cost)}.',
                f'Оплатить доставку (карго или СДЭК из Китая, ~{int(turnover)} дней оборот).',
                f'Получить сертификат / декларацию соответствия (если требуется для ниши).',
                f'Создать карточку товара: 7–9 инфографических фото + видео 30 с.',
                f'Написать SEO-описание: главное ключевое слово в заголовке, синонимы в описании.',
            ],
            'kpi': f'Товар поступил на склад WB FBO, карточка создана и прошла модерацию.',
        },
        {
            'idx': 2,
            'label': 'БЛОК 3 — Старт продаж и первые отзывы',
            'period': 'Дни 31–45',
            'tasks': [
                f'Запустить автоматическую рекламу (Аукцион) с бюджетом {_rub(ad_budget_wk)}/нед.',
                f'Настроить СПП-скидку 30–40% для разгона позиции в первые 2 недели.',
                f'Организовать выкупы-самовыкупы (не менее {min(5,test_batch)} шт) для стартового буста.',
                f'Мониторить ДРР: цель ≤30% в первый месяц, снижать к 15% к 3-му месяцу.',
                f'Оперативно отвечать на вопросы покупателей (статус карточки влияет на ранжирование).',
            ],
            'kpi': f'5+ отзывов с рейтингом ≥4.5, позиция в топ-100 по ключевому запросу.',
        },
        {
            'idx': 3,
            'label': 'БЛОК 4 — Оптимизация и масштаб',
            'period': 'Дни 46–75',
            'tasks': [
                f'Заказать дозаказ: {reorder_qty} шт (~{_rub(reorder_cost)}) до нулевого остатка.',
                f'Расширить ключевые запросы — добавить низкочастотники в описание и SEO-поля.',
                f'Перейти с авто-рекламы на ручную ставку: выделить топ-5 конверсионных ключей.',
                f'Добавить второй цвет / вариацию — расширяет охват без нового артикула.',
                f'Запустить акцию «Дни рождения» или скидку в коллекции для роста CTR.',
            ],
            'kpi': f'{int(target_sales * 0.6)}+ продаж/мес, ДРР ≤25%, остатки > 30 дней оборота.',
        },
        {
            'idx': 4,
            'label': 'БЛОК 5 — Закрепление позиции',
            'period': 'Дни 76–90',
            'tasks': [
                f'Достичь {target_sales}+ продаж/мес — плановый показатель для 5% доли ниши.',
                f'Выручка ≥ {_rub(target_rev)}/мес после выкупа. Проверить P&L.',
                f'Подать заявку в «Витрину» или «Новинки» для дополнительного трафика WB.',
                f'Собрать {min(50, target_sales * 3)}+ отзывов, добиться рейтинга карточки ≥4.7.',
                f'Запланировать масштабирование: следующая ниша или ещё один артикул той же категории.',
            ],
            'kpi': f'Самоокупаемый канал продаж, ROI > 0%, маржинальность ≥{int((avg_price * buyout_pct * 0.25) / (avg_price * buyout_pct) * 100)}%.',
        },
    ]

    for b in blocks:
        i = b['idx']
        # Заголовок блока
        hdr_t = Table([[_p(f'{b["label"]}', size=9.5, bold=True,
                           color=WHITE, align=TA_LEFT)]],
                      colWidths=[COL_W * 0.6])
        period_t = Table([[_p(b['period'], size=9.5, bold=True,
                               color=WHITE, align=TA_RIGHT)]],
                         colWidths=[COL_W * 0.4])
        hdr_row = Table([[hdr_t, period_t]],
                        colWidths=[COL_W * 0.6, COL_W * 0.4])
        hdr_row.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), C_STEP_H[i]),
            ('TOPPADDING',    (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('LEFTPADDING',   (0, 0), (-1, -1), 10),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 10),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        els.append(_sp(0.08))
        els.append(hdr_row)

        # Задачи
        task_rows = []
        for j, task in enumerate(b['tasks']):
            task_rows.append([_p(f'☐  {task}', size=8.5, color=C_TEXT)])
        body_t = Table(task_rows, colWidths=[COL_W])
        body_t.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), C_STEP[i]),
            ('TOPPADDING',    (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING',   (0, 0), (-1, -1), 16),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 10),
            ('LINEBELOW',     (0, 0), (-1, -2), 0.3, HexColor('#e5e7eb')),
        ]))
        els.append(body_t)

        # Метрика успеха
        kpi_t = Table(
            [[_p(f'✓ Критерий успеха: {b["kpi"]}', size=8, bold=True, color=C_STEP_H[i])]],
            colWidths=[COL_W]
        )
        kpi_t.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), HexColor('#f9fafb')),
            ('TOPPADDING',    (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING',   (0, 0), (-1, -1), 16),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 10),
            ('BOX',           (0, 0), (-1, -1), 0.5, C_STEP_H[i]),
        ]))
        els.append(kpi_t)

    # KPI-таблица 3 месяца
    els.append(_sp(0.2))
    els.append(_h3('Целевые KPI по месяцам'))
    s2 = int(target_sales * 0.6)
    s3 = target_sales
    r1 = int(s2 * 0.4 * avg_price * buyout_pct)
    r2 = int(s2 * avg_price * buyout_pct)
    r3 = int(s3 * avg_price * buyout_pct)
    kpi_rows = [
        ['Показатель', 'Месяц 1 (дни 1–30)', 'Месяц 2 (дни 31–60)', 'Месяц 3 (дни 61–90)'],
        ['Продажи, шт/мес',     f'5–10',       f'{int(s2*0.7)}–{s2}',  f'{s2}–{s3}'],
        ['Выручка после выкупа', _rub(r1),      _rub(r2),               _rub(r3)],
        ['ДРР (доля рекламы)',   '≤ 35%',       '≤ 25%',                '≤ 20%'],
        ['Отзывы, шт',          '3–5',          '20–30',                f'≥ {min(50, s3*3)}'],
        ['Позиция по ключу',     'топ 500',      'топ 200',              'топ 50'],
        ['Бюджет рекламы',      _rub(ad_budget_mo), _rub(ad_budget_mo), _rub(int(ad_budget_mo * 1.2))],
    ]
    kpi_t2 = _tbl(kpi_rows,
                  col_widths=[1.65*inch, 1.45*inch, 1.45*inch,
                               COL_W - 4.55*inch])
    kpi_t2.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, 0), C_BLUE_H),
        ('FONTSIZE',      (0, 0), (-1, -1), 8.5),
        ('TOPPADDING',    (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING',   (0, 0), (-1, -1), 7),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 7),
        ('GRID',          (0, 0), (-1, -1), 0.4, C_BORDER),
        ('ROWBACKGROUNDS',(0, 1), (-1, -1), [WHITE, C_TABLE_ODD]),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('FONTNAME',      (0, 0), (-1, 0), FB),
        ('FONTNAME',      (0, 1), (0, -1), FB),
    ]))
    els.append(kpi_t2)
    els.append(_p(
        f'* Расчёт на основе данных ниши: средняя цена {_rub(avg_price)}, '
        f'выкуп {int(buyout_pct*100)}%, тестовая партия {test_batch} шт.',
        size=7.5, color=C_GRAY, space_before=3,
    ))
    els.append(_sp(0.15))
    return els


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
    if level in ('basic', 'standard'):
        gold = HexColor('#f59e0b')
        grey_s = ParagraphStyle('_fgr', fontName=FN, fontSize=8,
                                 textColor=HexColor('#94a3b8'), leading=12)
        head_s = ParagraphStyle('_fh2', fontName=FB, fontSize=11, textColor=WHITE,
                                 alignment=TA_CENTER, leading=16)
        bull_s = ParagraphStyle('_fbl', fontName=FB, fontSize=10, textColor=WHITE,
                                 leading=15, leftIndent=4)
        cta_s  = ParagraphStyle('_fcta', fontName=FB, fontSize=9.5, textColor=gold,
                                 alignment=TA_CENTER, leading=14)
        tri_s  = ParagraphStyle('_ftri', fontName=FB, fontSize=10, textColor=gold,
                                 alignment=TA_CENTER, leading=14)

        els.append(_sp(0.45))

        if level == 'basic':
            tri_text   = '▲  ВЫ ВИДЕЛИ ТОЛЬКО НАЧАЛО  ▲'
            main_head  = 'Перейдите на PDF Standard — полный анализ ниши'
            items_data = [
                ('Все 5 графиков с подробными описаниями',
                 'Динамика выручки, сезонность, распределение цен,\nтренд ниши и прогноз на 3 месяца вперёд'),
                ('Топ-20 товаров ниши с полной аналитикой',
                 'Названия, цены, выручка, количество отзывов\nи прямые ссылки на каждый товар'),
                ('Развёрнутый мастер-анализ AI',
                 'Конкретные имена конкурентов, их слабые места,\nточные ценовые диапазоны для входа\nи пошаговая стратегия с цифрами'),
                ('Юнит-экономика в 3 сценариях',
                 'FBO Беларусь / FBS / FBO Россия —\nприбыль и ROI по каждому варианту работы'),
                ('Рекламная стратегия с прогнозом KPI',
                 'Бюджеты по фазам, прогноз CPM, CTR и CR\nна первые 2 месяца работы'),
            ]
            cta_text = 'Откройте WBAnalyzer и нажмите кнопку «PDF Standard»'
        else:  # standard
            tri_text   = '▲  ОСТАЛСЯ ПОСЛЕДНИЙ ШАГ  ▲'
            main_head  = 'PDF Deep — готовый план старта в нише'
            items_data = [
                ('Глубокий анализ каждого конкурента',
                 'Топ-игроки по отдельности: выручка, слабые места,\nкакие запросы они не закрывают — ваши точки входа'),
                ('Поиск поставщиков: готовые данные',
                 'Цены с Alibaba и 1688, MOQ, маржа и ROI\nпо каждой площадке — бери и звони'),
                ('Документы и сертификаты для WB',
                 'Полный список что нужно оформить,\nсколько стоит и где получить'),
                ('Стратегия поставок FBS/FBO',
                 'Расчёт первой партии, план закупок\nпо месяцам под сезонные пики'),
                ('Готовый AI-текст карточки товара',
                 'SEO-заголовок, описание, буллеты,\nрекомендации по фото и видео —\nпросто скопируй и загрузи на WB'),
            ]
            cta_text = 'Откройте WBAnalyzer и нажмите кнопку «PDF Deep»'

        # Строим блок
        inner_rows = [
            [Paragraph(tri_text, tri_s)],
            [Spacer(1, 5)],
            [Paragraph(main_head, head_s)],
            [Spacer(1, 12)],
        ]
        for title, desc in items_data:
            inner_rows.append([Paragraph(f'✓  {title}', bull_s)])
            for line in desc.split('\n'):
                inner_rows.append([Paragraph(f'   {line}', grey_s)])
            inner_rows.append([Spacer(1, 6)])
        inner_rows.append([Spacer(1, 4)])
        inner_rows.append([Paragraph(cta_text, cta_s)])

        blk = Table(inner_rows, colWidths=[COL_W - 40])
        blk.setStyle(TableStyle([
            ('TOPPADDING',    (0,0), (-1,-1), 2),
            ('BOTTOMPADDING', (0,0), (-1,-1), 2),
            ('LEFTPADDING',   (0,0), (-1,-1), 0),
            ('RIGHTPADDING',  (0,0), (-1,-1), 0),
        ]))
        wrap = Table([[blk]], colWidths=[COL_W])
        wrap.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), HexColor('#0f1e35')),
            ('TOPPADDING',    (0,0), (-1,-1), 20),
            ('BOTTOMPADDING', (0,0), (-1,-1), 20),
            ('LEFTPADDING',   (0,0), (-1,-1), 20),
            ('RIGHTPADDING',  (0,0), (-1,-1), 20),
            ('BOX',           (0,0), (-1,-1), 1.5, gold),
        ]))
        els.append(wrap)

    return els


# ── Главная функция ───────────────────────────────────────────────────────────

def generate(level: str, niche: dict, chart_items: list = None) -> bytes:
    """
    Генерирует PDF-отчёт: запускает агентов параллельно, затем делегирует в render().

    Args:
        level:       'basic' | 'standard' | 'deep'
        niche:       window.currentNiche из браузера
        chart_items: список товаров из MPStats (для графиков)
    Returns:
        PDF bytes
    """
    print(f'[PDF] Генерация уровень={level}, ниша={niche.get("name","")}')
    t0 = time.time()

    from concurrent.futures import ThreadPoolExecutor, as_completed

    task_map = {'master': lambda n: _run_master(n, level)}
    if level in ('standard', 'deep'):
        task_map['unit'] = _run_unit
        task_map['ads']  = _run_ads
    if level == 'deep':
        task_map['deep']        = _run_deep
        task_map['competitors'] = _run_competitors
        task_map['supplier']    = _run_supplier
        task_map['docs']        = _run_docs
        task_map['warehouse']   = _run_warehouse
        task_map['content']     = _run_content

    agents = {}
    content_text = ''
    print(f'[PDF] Запускаем {len(task_map)} агентов параллельно...')

    with ThreadPoolExecutor(max_workers=max(len(task_map), 1)) as pool:
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

    print(f'[PDF] Агенты готовы за {time.time()-t0:.1f}s, рендерим...')
    return render(level, niche, agents, content_text, chart_items or [])


# ── Валидатор качества PDF ────────────────────────────────────────────────────

def validate_pdf(level: str, niche: dict, agents: dict) -> list[str]:
    """
    Проверяет результаты агентов ПЕРЕД рендерингом.
    Возвращает список предупреждений (пустой список = всё OK).
    Вызывается из app.py ДО pdf_auto.render().
    """
    warnings = []

    # 1. Ниша: критические поля
    niche_name = (niche.get('display_name') or niche.get('name') or '').strip()
    if not niche_name:
        warnings.append('CRITICAL: нет названия ниши — PDF будет без заголовка')
    if not niche.get('avg_price') or float(niche.get('avg_price', 0)) == 0:
        warnings.append('WARN: avg_price = 0 — финансовая модель будет нулевой')
    if not niche.get('revenue') and not niche.get('revenue_annual'):
        warnings.append('WARN: нет данных о выручке ниши')

    # 2. Мастер-агент — критичен для всех уровней
    master = agents.get('master') or {}
    if not master:
        warnings.append('CRITICAL: master-агент вернул пустой ответ — отчёт не имеет смысла')
    else:
        if not master.get('market_analysis'):
            warnings.append('WARN: master.market_analysis пустой')
        if not master.get('final_recommendation'):
            warnings.append('WARN: master.final_recommendation пустой')
        if not master.get('final_verdict'):
            warnings.append('WARN: master.final_verdict пустой — не будет вердикта ВХОДИТЬ/НЕ ВХОДИТЬ')

    # 3. Standard-level: юнит-экономика и реклама
    if level in ('standard', 'deep'):
        unit = agents.get('unit') or {}
        if not unit or not unit.get('scenarios'):
            warnings.append('WARN: unit-агент пустой — раздел «Юнит-экономика» будет пропущен')

        ads = agents.get('ads') or {}
        if not ads or not ads.get('budget'):
            warnings.append('WARN: ads-агент пустой — раздел «Рекламная стратегия» будет пропущен')

    # 4. Deep-level: все уникальные разделы
    if level == 'deep':
        for agent_name, section_name in [
            ('deep',        'Глубокий анализ'),
            ('competitors', 'Анализ конкурентов'),
            ('supplier',    'Поставщики'),
            ('docs',        'Документы'),
            ('warehouse',   'Стратегия поставок'),
        ]:
            a = agents.get(agent_name) or {}
            if not a:
                warnings.append(f'WARN: {agent_name}-агент пустой — раздел «{section_name}» будет пропущен')

    # 5. Соответствие ниши: нет случайных "test" / "debug" значений
    low_name = niche_name.lower()
    for suspicious in ('test', 'debug', 'example', 'тест', 'пример'):
        if suspicious in low_name:
            warnings.append(f'WARN: подозрительное название ниши: «{niche_name}» — возможно тестовый запуск')
            break

    return warnings


def log_pdf_quality(level: str, niche_name: str, warnings: list[str]):
    """Логирует результат валидации."""
    if not warnings:
        print(f'[QA] ✅ {level.upper()} «{niche_name}» — проверка пройдена')
        return
    critical = [w for w in warnings if w.startswith('CRITICAL')]
    warn_only = [w for w in warnings if w.startswith('WARN')]
    status = '⛔ КРИТИЧЕСКИЕ ОШИБКИ' if critical else '⚠️  ПРЕДУПРЕЖДЕНИЯ'
    print(f'[QA] {status} {level.upper()} «{niche_name}»:')
    for w in warnings:
        print(f'[QA]   {w}')


# ── Раздельные точки входа (sequential mode) ──────────────────────────────────

_AGENT_FNS = {
    'master':      (_run_master,      'dict'),
    'unit':        (_run_unit,        'dict'),
    'ads':         (_run_ads,         'dict'),
    'deep':        (_run_deep,        'dict'),
    'competitors': (_run_competitors, 'dict'),
    'supplier':    (_run_supplier,    'dict'),
    'docs':        (_run_docs,        'dict'),
    'warehouse':   (_run_warehouse,   'dict'),
    'content':     (_run_content,     'str'),
}


def run_agent(name: str, niche: dict, level: str = 'standard', agents: dict = None):
    """Запускает ОДИН агент. Вызывается из /pdf-stream."""
    entry = _AGENT_FNS.get(name)
    if entry is None:
        return {'error': f'unknown agent: {name}'}
    fn, ret_type = entry
    if name == 'master':
        result = fn(niche, level)
    elif name == 'deep' and agents:
        master_verdict = (agents.get('master') or {}).get('final_verdict', '')
        result = fn(niche, master_verdict=master_verdict)
    elif name == 'docs' and agents:
        result = fn(niche, supplier_data=agents.get('supplier'))
    else:
        result = fn(niche)
    if ret_type == 'str':
        return {'text': result if isinstance(result, str) else ''}
    return result if isinstance(result, dict) else {}


def _sec_browser_charts(charts: dict, level: str, niche: dict = None) -> list:
    """Вставляет графики из браузера — полная ширина, одна на строку, с пояснением."""
    if not charts:
        return []
    n = niche or {}
    revenue   = float(n.get('revenue_annual', 0)) or float(n.get('revenue', 0)) * 12
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
            # Пустой или испорченный canvas — пропускаем (< 3 КБ = пустое изображение)
            if len(img_bytes) < 3000:
                print(f'[PDF] chart {chart_id} too small ({len(img_bytes)}b), skipping')
                continue
            img_buf = io.BytesIO(img_bytes)
            img_buf.seek(0)
            img = Image(img_buf, width=COL_W, height=2.5*inch)
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

    Структура документа (нарастающая):
      BASIC zone (синяя полоса):    Метрики → Графики → Топ-20
      STANDARD zone (жёлтая полоса): Мастер-анализ → Юнит-экономика → Реклама
      DEEP zone (фиолетовая полоса): Конкуренты → Поставщики → Документы → Склад → Карточка
    """
    global _CURRENT_LEVEL, _FM_CANONICAL
    _CURRENT_LEVEL = level
    _rc = _compute_finance(niche)
    _FM_CANONICAL = {
        'test_batch_units':  _rc['test_units'],
        'test_batch_cost':   _rc['test_batch_cost'],
        'monthly_ad_budget': _rc['monthly_ad_budget'],
        'breakeven_units':   _rc['breakeven_units'],
        'roi_3months':       _rc['roi_3months'],
        'payback_months':    _rc['payback_months'],
    }

    t0 = time.time()
    items     = chart_items or []
    top_limit = 5 if level == 'basic' else 20

    niche_name = (niche.get('display_name') or niche.get('name') or 'Анализ ниши')[:55]
    lname      = LEVEL_NAMES.get(level, level.upper())
    accent     = LEVEL_ACCENT.get(level, C_ACCENT)

    buf = io.BytesIO()

    HEADER_ZONE = 0.68 * inch
    FOOTER_ZONE = 0.52 * inch
    SIDEBAR_X   = 0.09 * inch
    SIDEBAR_W   = 0.11 * inch   # ~7.9pt — минимум по спецификации 4-6pt
    C_PURPLE    = HexColor('#7c3aed')
    C_BASIC_BAR = HexColor('#1e40af')
    C_STD_BAR   = HexColor('#b45309')

    # ── Callbacks для PageTemplate ─────────────────────────────────────────────
    def _on_dark(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_COVER_BG)
        canvas.rect(0, 0, W, H, fill=1, stroke=0)
        canvas.restoreState()

    def _make_content_cb(bar_color, bar_label=''):
        """Фабрика колбека страницы: шапка + подвал + цветная полоса зоны."""
        def _cb(canvas, doc):
            canvas.saveState()
            # Верхний колонтитул
            hY = H - 0.38 * inch
            canvas.setFont(FB, 7.5)
            canvas.setFillColor(C_NAVY)
            canvas.drawString(MARGIN, hY, f'WBAnalyzer  ·  {niche_name}')
            canvas.setFont(FN, 7.5)
            canvas.setFillColor(accent)
            canvas.drawRightString(W - MARGIN, hY, lname)
            canvas.setStrokeColor(accent)
            canvas.setLineWidth(0.7)
            canvas.line(MARGIN, H - HEADER_ZONE + 0.04 * inch,
                        W - MARGIN, H - HEADER_ZONE + 0.04 * inch)
            # Нижний колонтитул
            fY = 0.22 * inch
            canvas.setFont(FN, 7)
            canvas.setFillColor(C_GRAY)
            canvas.drawString(MARGIN, fY,
                              f'© {PLATFORM_YEAR} WBAnalyzer · {PLATFORM_URL}')
            canvas.drawRightString(W - MARGIN, fY, f'Стр. {doc.page - 1}')
            canvas.setStrokeColor(C_BORDER)
            canvas.setLineWidth(0.4)
            canvas.line(MARGIN, fY + 0.13 * inch, W - MARGIN, fY + 0.13 * inch)
            # Цветная зонная полоса на левом поле
            if bar_color is not None:
                bh = H - HEADER_ZONE - FOOTER_ZONE
                canvas.setFillColor(bar_color)
                canvas.roundRect(SIDEBAR_X, FOOTER_ZONE, SIDEBAR_W, bh, 1.5,
                                 fill=1, stroke=0)
                if bar_label:
                    canvas.setFillColor(colors.white)
                    canvas.setFont(FB, 5.5)
                    canvas.saveState()
                    canvas.translate(SIDEBAR_X + SIDEBAR_W / 2, FOOTER_ZONE + bh / 2)
                    canvas.rotate(90)
                    canvas.drawCentredString(0, -1.5, bar_label)
                    canvas.restoreState()
            canvas.restoreState()
        return _cb

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
        PageTemplate(id='Cover',    frames=[full_frame],    onPage=_on_dark),
        PageTemplate(id='Basic',    frames=[content_frame], onPage=_make_content_cb(C_BASIC_BAR, '▌ BASIC')),
        PageTemplate(id='Standard', frames=[content_frame], onPage=_make_content_cb(C_STD_BAR,   '▌ STANDARD')),
        PageTemplate(id='Deep',     frames=[content_frame], onPage=_make_content_cb(C_PURPLE,    '▌ DEEP')),
        PageTemplate(id='NoBar',    frames=[content_frame], onPage=_make_content_cb(None)),
        PageTemplate(id='Finale',   frames=[full_frame],    onPage=_on_dark),
    ])

    # ── Контент ───────────────────────────────────────────────────────────────
    els = []

    # Обложка (тёмная страница)
    els += _sec_cover(niche, level)
    els.append(NextPageTemplate('Basic'))
    els.append(PageBreak())

    # ── BASIC: Метрики → Графики → Топ-20 ───────────────────────────────────
    if level == 'deep':
        # Содержание и блок ценности — на первой контентной странице (Basic zone)
        els += _sec_toc_deep([
            ('Ключевые показатели ниши',    2),
            ('Графики ниши',                3),
            ('Топ-20 товаров ниши',         4),
            ('Мастер-анализ AI',            5),
            ('Юнит-экономика',              7),
            ('Рекламная стратегия',         8),
            ('Анализ конкурентов (топ-10)', 9),
            ('Поиск поставщиков',          11),
            ('Документы и сертификаты',    12),
            ('Стратегия поставок',         13),
            ('Создание карточки товара',   14),
            ('Итоговый вывод',             15),
            ('Словарь терминов',           16),
        ])

    els += _sec_metrics(niche)
    els += _sec_verdict_placard(agents.get('master') or {})

    browser_charts = dict(charts or {})
    if browser_charts:
        els += _sec_browser_charts(browser_charts, level, niche)
    elif items:
        # Fallback: генерируем все 5 графиков локально из данных MPStats items
        _chart_els = [_h2('Графики ниши'), _hr()]
        _added = 0
        # 1. Динамика выручки
        dc1 = _chart_monthly_bar(items, 'revenue', label='Динамика выручки ниши (₽/мес)')
        if dc1:
            _chart_els += [_p('Динамика выручки', size=10, bold=True, color=C_NAVY,
                               align=TA_CENTER, space_before=8, space_after=2),
                           _p('Изменение суммарной выручки топ товаров по месяцам.',
                              size=8, color=C_GRAY, space_before=0, space_after=4),
                           dc1, _sp(0.1)]
            _added += 1
        # 2. Сезонность заказов
        dc2 = _chart_monthly_bar(items, 'sales', label='Сезонность заказов (шт/мес)')
        if dc2:
            _chart_els += [_p('Сезонность заказов', size=10, bold=True, color=C_NAVY,
                               align=TA_CENTER, space_before=8, space_after=2),
                           _p('Количество заказов по месяцам — ключ к планированию закупок.',
                              size=8, color=C_GRAY, space_before=0, space_after=4),
                           dc2, _sp(0.1)]
            _added += 1
        # 3. Распределение цен
        if level in ('standard', 'deep') and len(items) >= 4:
            dc3 = _chart_price_bar(items)
            if dc3:
                _chart_els += [_p('Распределение цен', size=10, bold=True, color=C_NAVY,
                                   align=TA_CENTER, space_before=8, space_after=2),
                               _p(f'Количество товаров в каждом ценовом диапазоне. Средний чек ниши — {_rub(float(niche.get("avg_price") or 0))}.',
                                  size=8, color=C_GRAY, space_before=0, space_after=4),
                               dc3, _sp(0.1)]
                _added += 1
        if _added > 0:
            els += _chart_els

    # Таблица топ-20 + сопроводительные графики по товарам
    els += _sec_top_products(items, limit=top_limit, level=level)

    # Дополнительные графики рядом с топ-товарами (если нет browser charts)
    if not browser_charts and items and level in ('standard', 'deep'):
        if len(items) >= 4:
            dl = _chart_revenue_line(items)
            if dl:
                els += [_h3('Топ товары по выручке'), dl, _sp(0.1)]
        els += _chart_sellers_table(items)

    # ── BASIC-only: Master + сезонный план ─────────────────────────────────
    if level == 'basic':
        els += _sec_master(agents.get('master') or {}, niche=niche)
        els += _sec_seasonal_plan(agents.get('master') or {})
        els += _sec_conclusion(level, agents)
        els += _sec_upsell(level)
        els.append(NextPageTemplate('Finale'))
        els.append(PageBreak())
        els += _sec_finale(level, agents)

    # ── STANDARD: Мастер → Юнит → Реклама (→ Вывод → Глоссарий → Финал) ──
    elif level == 'standard':
        els.append(NextPageTemplate('Standard'))
        els.append(PageBreak())
        els += _sec_master(agents.get('master') or {}, niche=niche)
        els += _sec_unit(agents.get('unit') or {})
        els += _sec_ads(agents.get('ads') or {})
        els += _sec_conclusion(level, agents)
        els += _sec_upsell(level)
        els.append(NextPageTemplate('NoBar'))
        els += _sec_glossary()
        els.append(NextPageTemplate('Finale'))
        els.append(PageBreak())
        els += _sec_finale(level, agents)

    # ── DEEP: Standard-секции → Deep-секции → Вывод → 30 дней → Финал ─────
    else:
        # Standard zone: Master, Unit, Ads
        els.append(NextPageTemplate('Standard'))
        els.append(PageBreak())
        els += _sec_master(agents.get('master') or {}, niche=niche)
        els += _sec_unit(agents.get('unit') or {})
        els += _sec_ads(agents.get('ads') or {})

        # Deep zone: Competitors, Supplier, Docs, Warehouse, Content
        els.append(NextPageTemplate('Deep'))
        els.append(PageBreak())
        els += _sec_competitors_merged(
            agents.get('competitors') or {},
            agents.get('deep') or {}
        )
        els += _sec_supplier(agents.get('supplier') or {})
        els += _sec_docs(agents.get('docs') or {})
        els += _sec_warehouse(agents.get('warehouse') or {})
        els += _sec_content(content_text)
        els += _sec_conclusion(level, agents)

        # 90-day plan (NoBar, light template) — before glossary
        els.append(NextPageTemplate('NoBar'))
        fm_data = agents.get('unit') or {}
        els.append(PageBreak())
        els += _sec_30days(niche=niche, fm=fm_data)

        # Glossary — last content section before Finale
        els += _sec_glossary()

        # Finale (dark template)
        els.append(NextPageTemplate('Finale'))
        els.append(PageBreak())
        els += _sec_finale(level, agents)

    doc.build(els)
    buf.seek(0)
    pdf = buf.getvalue()
    print(f'[PDF-RENDER] Готово за {time.time()-t0:.1f}s, размер={len(pdf)} байт')
    return pdf
