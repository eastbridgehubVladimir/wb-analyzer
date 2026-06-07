"""
Сервис генерации PDF отчётов для анализа ниш.
"""
import os
from io import BytesIO
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph,
    Spacer, Image, PageBreak, HRFlowable, KeepTogether,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from services.pdf_charts import chart_price_distribution, chart_top_revenue
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT

# ── Шрифты ────────────────────────────────────────────────────────────────────
try:
    import matplotlib
    _fd = os.path.join(os.path.dirname(matplotlib.__file__), 'mpl-data', 'fonts', 'ttf')
    pdfmetrics.registerFont(TTFont('DejaVu',     os.path.join(_fd, 'DejaVuSans.ttf')))
    pdfmetrics.registerFont(TTFont('DejaVu-Bold', os.path.join(_fd, 'DejaVuSans-Bold.ttf')))
    FN = 'DejaVu'
    FB = 'DejaVu-Bold'
except Exception:
    FN, FB = 'Helvetica', 'Helvetica-Bold'

# ── Цвета ─────────────────────────────────────────────────────────────────────
C_DARK   = colors.HexColor('#1F2937')
C_GRAY   = colors.HexColor('#374151')
C_LGRAY  = colors.HexColor('#6B7280')
C_LIGHT  = colors.HexColor('#F9FAFB')
C_BORDER = colors.HexColor('#E5E7EB')
C_GREEN  = colors.HexColor('#10B981')
C_YELLOW = colors.HexColor('#F59E0B')
C_RED    = colors.HexColor('#EF4444')
C_BLUE   = colors.HexColor('#3B82F6')
C_WHITE  = colors.white


# ── Форматирование ────────────────────────────────────────────────────────────
def _rub(n: float) -> str:
    n = float(n or 0)
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.1f} млрд ₽"
    if n >= 1_000_000:     return f"{n/1_000_000:.1f} млн ₽"
    if n >= 1_000:         return f"{n/1_000:.0f} тыс ₽"
    return f"{n:.0f} ₽"


def _pct(n: float) -> str:
    """Конвертирует дробь 0-1 в проценты."""
    return f"{float(n or 0) * 100:.0f}%"


def _pct_raw(n: float) -> str:
    """Отображает значение уже в процентах (не умножает на 100)."""
    return f"{float(n or 0):.1f}%"


def _verdict_hex(v: str) -> str:
    return {'BUY': '10B981', 'TEST': 'F59E0B', 'SKIP': 'EF4444'}.get(v.upper(), '6B7280')


# ── Генерация аналитического текста ──────────────────────────────────────────
def _market_paragraphs(m: dict, category: str) -> list[str]:
    annual   = float(m.get('annual_revenue', 0))
    monthly  = float(m.get('monthly_revenue_estimate', 0))
    orders   = int(m.get('orders_per_month', 0))
    sellers  = int(m.get('active_sellers', 0))
    sws      = int(m.get('sellers_with_sales', 0))
    price    = float(m.get('median_price', 0))
    buyout   = float(m.get('buyout_pct', 0))
    profit   = float(m.get('profit_pct', 0))
    turnover = float(m.get('turnover', 0))
    comp     = str(m.get('competition_level', '')).upper()
    lost     = float(m.get('lost_revenue_pct', 0))
    rating   = float(m.get('avg_rating', 0))

    paras = []

    # Размер рынка
    if annual >= 2e9:
        size = f"крупный рынок — годовая выручка {_rub(annual)}"
    elif annual >= 500e6:
        size = f"рынок среднего размера с годовой выручкой {_rub(annual)}"
    else:
        size = f"относительно небольшой рынок с годовой выручкой {_rub(annual)}"
    paras.append(
        f"Ниша «{category}» представляет {size}. "
        f"Среднемесячный объём продаж оценивается в {_rub(monthly)} "
        f"({orders} заказов в месяц)."
    )

    # Конкуренция
    active_pct = round(sws / sellers * 100) if sellers else 0
    if comp == 'SATURATED':
        comp_text = (
            f"Рынок перенасыщен: {sellers} продавцов, из которых реальные продажи "
            f"делают только {sws} ({active_pct}%). Войти без чёткого УТП крайне сложно — "
            f"большинство игроков конкурируют по цене, маржа минимальна."
        )
    elif comp == 'HIGH':
        comp_text = (
            f"Высокая конкуренция: {sellers} продавцов, активных {sws} ({active_pct}%). "
            f"Необходимы дифференциация и качественная карточка товара."
        )
    elif comp == 'MEDIUM':
        comp_text = (
            f"Умеренная конкуренция: {sellers} продавцов, активных {sws} ({active_pct}%). "
            f"Есть пространство для нового игрока при правильном позиционировании."
        )
    else:
        comp_text = (
            f"Низкая конкуренция: всего {sellers} продавцов, активных {sws} ({active_pct}%). "
            f"Благоприятные условия для входа."
        )
    paras.append(comp_text)

    # Цена и качество ниши
    buyout_label  = 'отличный' if buyout >= 0.8 else ('хороший' if buyout >= 0.65 else 'низкий, высоки затраты на возвраты')
    profit_label  = 'высокая' if profit >= 0.35 else ('средняя' if profit >= 0.2 else 'низкая — важна оптимизация логистики')
    paras.append(
        f"Средняя цена товара — {_rub(price)}. "
        f"Выкупаемость заказов {_pct(buyout)} ({buyout_label}). "
        f"Маржинальность до себестоимости {_pct(profit)} ({profit_label})."
    )

    # Оборачиваемость
    if turnover > 0:
        if turnover <= 45:
            tv_label = "очень быстрые продажи — нужен постоянный запас на складе"
        elif turnover <= 90:
            tv_label = "умеренная скорость продаж, стандартная для категории"
        elif turnover <= 180:
            tv_label = "медленные продажи — требуется осторожность с объёмом закупки"
        else:
            tv_label = "очень медленные продажи, высокий риск заморозки капитала"
        paras.append(f"Оборачиваемость товарного запаса — {turnover:.0f} дней ({tv_label}).")

    # Упущенная выручка
    if lost > 0:
        if lost > 0.3:
            paras.append(
                f"Упущенная выручка ниши составляет {_pct(lost)} — это сигнал о дефиците: "
                f"покупатели хотят купить, но товаров нет. Для нового продавца это возможность "
                f"быстро занять долю рынка при наличии стабильного товарного запаса."
            )
        elif lost > 0.1:
            paras.append(
                f"Умеренная упущенная выручка ({_pct(lost)}) говорит о периодическом дефиците. "
                f"Поддержание стабильного остатка даст конкурентное преимущество."
            )

    # Рейтинг
    if rating > 0:
        if rating >= 4.5:
            paras.append(f"Средний рейтинг товаров в нише — {rating:.1f}★ — покупатели удовлетворены качеством. Новый товар должен с самого начала нацеливаться на рейтинг ≥ 4.5.")
        elif rating >= 4.0:
            paras.append(f"Средний рейтинг {rating:.1f}★ — есть недовольные покупатели. Улучшение качества или упаковки может стать значимым УТП.")
        else:
            paras.append(f"Низкий средний рейтинг {rating:.1f}★ — покупатели часто разочарованы. Качественный товар с хорошим сервисом быстро выделится на фоне конкурентов.")

    return paras


def _competition_analysis(m: dict) -> list[str]:
    sellers  = int(m.get('active_sellers', 0))
    sws      = int(m.get('sellers_with_sales', 0))
    comp     = str(m.get('competition_level', '')).upper()
    price    = float(m.get('median_price', 0))
    top20    = float(m.get('top_20pct_revenue_share', 0))

    paras = []

    if top20 > 0:
        paras.append(
            f"Топ-20% продавцов контролируют {_pct(top20)} выручки ниши. "
            f"{'Рынок сильно монополизирован — войти и конкурировать с лидерами очень сложно.' if top20 > 0.8 else 'Распределение выручки относительно равномерное — есть пространство для новых игроков.'}"
        )

    inactive = sellers - sws
    if sellers > 0:
        paras.append(
            f"Из {sellers} зарегистрированных продавцов {inactive} ({round(inactive/sellers*100)}%) "
            f"не имеют продаж. Это может означать, что конкуренты уже ушли из ниши из-за убыточности, "
            f"либо ещё не успели запустить товары — нужно изучить конкретных игроков."
        )

    if price > 0:
        low  = price * 0.7
        high = price * 1.3
        paras.append(
            f"Рекомендуемый ценовой диапазон для нового игрока: {_rub(low)} – {_rub(high)} "
            f"(±30% от медианы {_rub(price)}). Цена ниже минимума создаёт ценовую войну, "
            f"выше максимума — нужно обосновать премиальность."
        )

    return paras


def _entry_recommendations(m: dict, verdict: str, score: int) -> list[str]:
    comp    = str(m.get('competition_level', '')).upper()
    turnover = float(m.get('turnover', 0))
    buyout   = float(m.get('buyout_pct', 0))
    profit   = float(m.get('profit_pct', 0))
    lost     = float(m.get('lost_revenue_pct', 0))
    price    = float(m.get('median_price', 0))

    recs = []

    if verdict == 'BUY':
        recs.append("Ниша привлекательна для входа. Рекомендуется начать с тестовой партии 50-100 единиц, оценить отклик рынка и масштабировать при положительной динамике.")
    elif verdict == 'TEST':
        recs.append("Рекомендуется тестовая закупка минимальной партией (30-50 единиц) без существенных вложений в рекламу на первом этапе. Цель — проверить гипотезы и оценить реальный спрос.")
    else:
        recs.append("Вход в нишу сопряжён с высокими рисками. Если принято решение войти — только минимальная партия (10-20 единиц) для тестирования без крупных рекламных бюджетов.")

    if turnover > 90:
        recs.append(f"Оборачиваемость {turnover:.0f} дней означает: закупайте малыми партиями и часто. Не замораживайте большой объём на первых поставках.")

    if buyout < 0.65:
        recs.append("Низкий выкуп указывает на проблемы с описанием, фото или несоответствием ожиданий. Уделите особое внимание карточке товара и первым отзывам.")

    if profit < 0.2:
        recs.append("Низкая маржинальность требует жёсткого контроля логистических затрат. Рассмотрите отгрузку на ближайший к покупателям склад WB для снижения себестоимости доставки.")

    if lost > 0.2:
        recs.append(f"Высокая упущенная выручка ({_pct(lost)}) — держите буферный запас на складе WB, иначе потеряете позиции в выдаче.")

    if price > 5000:
        recs.append(f"При средней цене {_rub(price)} покупатели ожидают качественные фото, видео и подробные отзывы. Инвестируйте в профессиональную съёмку и работу с первыми покупателями.")

    recs.append("Запустите SEO-оптимизацию карточки с первого дня: используйте высокочастотные запросы в заголовке, средне- и низкочастотные — в описании.")
    recs.append("Первые 2-4 недели — самое важное время для ранжирования. Обеспечьте стабильные продажи (даже выкуп среди знакомых) чтобы алгоритм WB начал продвигать карточку.")

    return recs


def _risk_assessment(m: dict, verdict: str) -> list[tuple[str, str, str]]:
    """Возвращает список (риск, уровень, рекомендация)."""
    risks = []
    comp    = str(m.get('competition_level', '')).upper()
    turnover = float(m.get('turnover', 0))
    buyout   = float(m.get('buyout_pct', 0))
    lost     = float(m.get('lost_revenue_pct', 0))
    sellers  = int(m.get('active_sellers', 0))

    if comp in ('SATURATED', 'HIGH'):
        risks.append(('Конкурентный', 'Высокий', f'{sellers} продавцов — сложно выделиться без сильного УТП'))
    else:
        risks.append(('Конкурентный', 'Низкий', 'Умеренная конкуренция, есть место для нового игрока'))

    if turnover > 180:
        risks.append(('Ликвидности', 'Высокий', f'Оборачиваемость {turnover:.0f} дн — деньги заморожены надолго'))
    elif turnover > 90:
        risks.append(('Ликвидности', 'Средний', f'Оборачиваемость {turnover:.0f} дн — контролируйте объём закупки'))
    else:
        risks.append(('Ликвидности', 'Низкий', 'Быстрые продажи, средства возвращаются быстро'))

    if buyout < 0.6:
        risks.append(('Возвратов', 'Высокий', f'Выкуп {_pct(buyout)} — высокие расходы на обратную логистику'))
    elif buyout < 0.75:
        risks.append(('Возвратов', 'Средний', f'Выкуп {_pct(buyout)} — следите за причинами возвратов'))
    else:
        risks.append(('Возвратов', 'Низкий', f'Хороший выкуп {_pct(buyout)}'))

    if verdict == 'SKIP':
        risks.append(('Общий', 'Высокий', 'Комплексная оценка не рекомендует вход'))
    elif verdict == 'TEST':
        risks.append(('Общий', 'Средний', 'Рекомендуется ограниченное тестирование'))
    else:
        risks.append(('Общий', 'Низкий', 'Ниша привлекательна для входа'))

    return risks


# ── Основной генератор ────────────────────────────────────────────────────────
class PDFReportGenerator:
    def __init__(self):
        self.styles = getSampleStyleSheet()
        self._setup_styles()

    def _setup_styles(self):
        def add(name, **kw):
            if name not in self.styles:
                self.styles.add(ParagraphStyle(name=name, **kw))

        add('WBTitle',    fontSize=22, textColor=C_DARK,  alignment=TA_CENTER, fontName=FB, spaceAfter=4)
        add('WBSubtitle', fontSize=12, textColor=C_GRAY,  alignment=TA_CENTER, fontName=FN, spaceAfter=4)
        add('WBH2',       fontSize=13, textColor=C_DARK,  fontName=FB, spaceBefore=14, spaceAfter=6)
        add('WBH3',       fontSize=11, textColor=C_GRAY,  fontName=FB, spaceBefore=8,  spaceAfter=4)
        add('WBBody',     fontSize=10, textColor=C_GRAY,  fontName=FN, spaceAfter=6, leading=15)
        add('WBBullet',   fontSize=10, textColor=C_GRAY,  fontName=FN, spaceAfter=4, leftIndent=14, leading=14)
        add('WBSmall',    fontSize=9,  textColor=C_LGRAY, fontName=FN, spaceAfter=2)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _hr(self):
        return HRFlowable(width='100%', thickness=0.5, color=C_BORDER, spaceAfter=8, spaceBefore=2)

    def _sp(self, h=0.15):
        return Spacer(1, h * inch)

    def _h2(self, t):  return Paragraph(t, self.styles['WBH2'])
    def _h3(self, t):  return Paragraph(t, self.styles['WBH3'])
    def _body(self, t): return Paragraph(t, self.styles['WBBody'])
    def _small(self, t): return Paragraph(t, self.styles['WBSmall'])
    def _bullet(self, t): return Paragraph(f"• {t}", self.styles['WBBullet'])

    def _metric_table(self, rows: list) -> Table:
        t = Table(rows, colWidths=[3.3*inch, 1.8*inch, 1.5*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,0), C_GRAY),
            ('TEXTCOLOR',     (0,0), (-1,0), C_WHITE),
            ('FONTNAME',      (0,0), (-1,0), FB),
            ('FONTNAME',      (0,1), (-1,-1), FN),
            ('FONTSIZE',      (0,0), (-1,-1), 10),
            ('ALIGN',         (0,0), (-1,-1), 'LEFT'),
            ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
            ('BOTTOMPADDING', (0,0), (-1,0), 8),
            ('TOPPADDING',    (0,1), (-1,-1), 5),
            ('BOTTOMPADDING', (0,1), (-1,-1), 5),
            ('GRID',          (0,0), (-1,-1), 0.5, C_BORDER),
            ('ROWBACKGROUNDS',(0,1), (-1,-1), [C_WHITE, C_LIGHT]),
        ]))
        return t

    # ── sections ──────────────────────────────────────────────────────────────

    def _sec_header(self, data, level):
        category = data.get('category', '')
        verdict  = (data.get('verdict') or 'N/A').upper()
        score    = data.get('score', 0)
        hex_v    = _verdict_hex(verdict)
        lv_color = {'basic':'#94A3B8','standard':'#38BDF8','deep':'#8B5CF6'}.get(level,'#38BDF8')
        lv_label = {'basic':'Basic','standard':'Standard','deep':'Deep Dive'}.get(level,'Standard')
        verdict_ru = {'BUY':'Рекомендуем входить','TEST':'Тестовая закупка','SKIP':'Не рекомендуем'}.get(verdict, verdict)

        els = [
            Paragraph("Анализ ниши Wildberries", self.styles['WBTitle']),
            Paragraph(f"<font color='{lv_color}'><b>{lv_label}</b></font>  •  {category}", self.styles['WBSubtitle']),
            self._small(f"Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}"),
            self._sp(0.15), self._hr(),
            Paragraph(
                f"<font color='#{hex_v}'><b>{verdict}</b></font>  <b>{score}/100</b>  —  {verdict_ru}",
                self.styles['WBH2']
            ),
        ]
        summary = data.get('summary','')
        if summary and summary != category:
            els.append(self._body(summary))
        els.append(self._sp(0.1))
        return els

    def _sec_metrics(self, m, level):
        els = [self._h2("Ключевые метрики"), self._hr()]

        # Комиссия: проверяем масштаб (хранится как % или дробь?)
        commission = float(m.get('commission', 0))
        comm_str = _pct_raw(commission) if commission > 1 else _pct(commission)

        rows = [['Метрика', 'Значение', 'Оценка']]
        rows += [
            ['Выручка ниши (год)',          _rub(m.get('annual_revenue', 0)), ''],
            ['Выручка ниши (месяц, оценка)',_rub(m.get('monthly_revenue_estimate', 0)), ''],
            ['Заказов в месяц',             str(int(m.get('orders_per_month', 0))), ''],
            ['Заказов в день (среднее)',     f"{float(m.get('avg_orders_per_day',0)):.1f}", ''],
            ['Продавцов всего',             str(int(m.get('active_sellers', 0))), ''],
            ['Из них с продажами',          str(int(m.get('sellers_with_sales', 0))), ''],
            ['Средняя цена',                _rub(m.get('median_price', 0)), ''],
            ['Уровень конкуренции',         str(m.get('competition_level','')).upper(), ''],
        ]

        if level in ('standard', 'deep'):
            bp = float(m.get('buyout_pct', 0))
            pp = float(m.get('profit_pct', 0))
            tv = float(m.get('turnover', 0))
            lp = float(m.get('lost_revenue_pct', 0))
            ar = float(m.get('avg_rating', 0))

            rows += [
                ['Выкуп',                   _pct(bp),   'отличный' if bp>=0.8 else ('хороший' if bp>=0.65 else 'низкий')],
                ['Маржинальность (до себест.)', _pct(pp), 'высокая' if pp>=0.35 else ('средняя' if pp>=0.2 else 'низкая')],
                ['Оборачиваемость',         f"{tv:.0f} дн", 'быстро' if tv<=45 else ('умеренно' if tv<=90 else 'медленно')],
            ]
            if commission:
                rows.append(['Комиссия WB', comm_str, ''])
            if lp:
                rows.append(['Упущенная выручка', _pct(lp),
                             'высокий потенциал' if lp>0.3 else ('умеренный' if lp>0.1 else 'низкий')])
            if ar:
                rows.append(['Средний рейтинг', f"{ar:.1f} ★", ''])

        els.append(self._metric_table(rows))
        els.append(self._sp(0.2))
        return els

    def _sec_market_analysis(self, m, category):
        """Развёрнутый текстовый анализ рынка."""
        paras = _market_paragraphs(m, category)
        if not paras:
            return []
        els = [self._h2("Анализ рынка"), self._hr()]
        for p in paras:
            els.append(self._body(p))
        els.append(self._sp(0.1))
        return els

    def _sec_competition(self, m):
        paras = _competition_analysis(m)
        if not paras:
            return []
        els = [self._h2("Конкурентная среда"), self._hr()]
        for p in paras:
            els.append(self._body(p))
        els.append(self._sp(0.1))
        return els

    def _sec_ai_insights(self, ai, level):
        insights   = list(ai.get('insights') or [])
        hypotheses = list(ai.get('hypotheses') or [])
        analysis   = str(ai.get('analysis') or '')
        if not (insights or hypotheses or analysis):
            return []

        els = [self._h2("AI-аналитика"), self._hr()]

        if insights:
            els.append(self._h3("Ключевые наблюдения:"))
            limit = 3 if level == 'basic' else len(insights)
            for item in insights[:limit]:
                els.append(self._bullet(str(item)))
            els.append(self._sp(0.1))

        if hypotheses and level in ('standard', 'deep'):
            els.append(self._h3("Гипотезы для тестирования:"))
            limit = 3 if level == 'standard' else len(hypotheses)
            for item in hypotheses[:limit]:
                els.append(self._bullet(str(item)))
            els.append(self._sp(0.1))

        if analysis and level == 'deep':
            els.append(self._h3("Детальный анализ:"))
            for para in str(analysis).split('\n'):
                para = para.strip()
                if para:
                    els.append(self._body(para))
            els.append(self._sp(0.1))

        return els

    def _sec_recommendations(self, m, verdict, score):
        recs = _entry_recommendations(m, verdict, score)
        if not recs:
            return []
        els = [self._h2("Рекомендации по входу в нишу"), self._hr()]
        for r in recs:
            els.append(self._bullet(r))
        els.append(self._sp(0.1))
        return els

    def _sec_risks(self, m, verdict):
        risks = _risk_assessment(m, verdict)
        if not risks:
            return []
        els = [self._h2("Оценка рисков"), self._hr()]

        rows = [['Риск', 'Уровень', 'Комментарий']]
        for name, level_r, comment in risks:
            rows.append([name, level_r, comment])

        t = Table(rows, colWidths=[1.5*inch, 1.2*inch, 3.9*inch])
        risk_colors = {'Высокий': colors.HexColor('#FEE2E2'), 'Средний': colors.HexColor('#FEF3C7'), 'Низкий': colors.HexColor('#D1FAE5')}
        style_cmds = [
            ('BACKGROUND',    (0,0), (-1,0), C_GRAY),
            ('TEXTCOLOR',     (0,0), (-1,0), C_WHITE),
            ('FONTNAME',      (0,0), (-1,0), FB),
            ('FONTNAME',      (0,1), (-1,-1), FN),
            ('FONTSIZE',      (0,0), (-1,-1), 10),
            ('ALIGN',         (0,0), (-1,-1), 'LEFT'),
            ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
            ('BOTTOMPADDING', (0,0), (-1,0), 8),
            ('TOPPADDING',    (0,1), (-1,-1), 5),
            ('BOTTOMPADDING', (0,1), (-1,-1), 5),
            ('GRID',          (0,0), (-1,-1), 0.5, C_BORDER),
        ]
        for i, (_, level_r, _) in enumerate(risks, start=1):
            bg = risk_colors.get(level_r, C_LIGHT)
            style_cmds.append(('BACKGROUND', (0, i), (-1, i), bg))
        t.setStyle(TableStyle(style_cmds))
        els.append(t)
        els.append(self._sp(0.2))
        return els

    def _sec_strategy(self, m, verdict, category):
        """Пошаговая стратегия входа — только для Deep."""
        price = float(m.get('median_price', 0))
        turnover = float(m.get('turnover', 0))

        steps = [
            ("Шаг 1. Исследование (1-2 недели)",
             f"Изучите топ-20 конкурентов в нише «{category}»: их карточки, отзывы, "
             f"ценовые диапазоны, жалобы покупателей. Найдите 3-5 слабых мест которые "
             f"можно закрыть вашим товаром."),
            ("Шаг 2. Закупка и подготовка (2-4 недели)",
             f"Тестовая партия {'20-30' if verdict=='SKIP' else '50-100'} единиц. "
             f"Подготовьте карточку: профессиональные фото (минимум 6 изображений), "
             f"видео, заполните все характеристики, SEO-заголовок и описание."),
            ("Шаг 3. Запуск и сбор отзывов (4-8 недель)",
             f"Первые {'2-4' if turnover<=45 else '4-8'} недели — фокус на получении "
             f"первых 10-20 отзывов. Используйте программу лояльности WB и работайте "
             f"с каждым возвратом индивидуально."),
            ("Шаг 4. Реклама (после 10+ отзывов)",
             f"Запустите автоматическую кампанию в поиске с дневным бюджетом "
             f"{_rub(price * 0.1)}-{_rub(price * 0.2)} (1-2% от цены товара в день). "
             f"Следите за ДРР: цель — не выше 15-20%."),
            ("Шаг 5. Масштабирование",
             f"При положительной динамике (рейтинг ≥4.5, ДРР ≤20%, стабильные продажи) "
             f"увеличивайте закупку и расширяйте ассортимент вариаций товара."),
        ]

        els = [PageBreak(), self._h2("Стратегия входа в нишу"), self._hr()]
        for title, text in steps:
            els.append(KeepTogether([
                self._h3(title),
                self._body(text),
                self._sp(0.1),
            ]))
        return els

    def _sec_charts(self, data, level):
        products = data.get('products', [])
        if not products:
            return []
        els = []
        try:
            els += [self._h2("Распределение цен в нише"), self._hr()]
            els.append(Image(chart_price_distribution(products), width=6*inch, height=2.5*inch))
            els.append(self._sp(0.15))
        except Exception:
            pass
        if level in ('standard', 'deep') and len(products) >= 5:
            top_n = 10 if level == 'standard' else 20
            try:
                els += [self._h2(f"Топ-{top_n} товаров по выручке"), self._hr()]
                els.append(Image(chart_top_revenue(products, top_n=top_n), width=6*inch, height=2.5*inch))
                els.append(self._sp(0.15))
            except Exception:
                pass
        return els

    def _sec_products(self, data, level):
        products = data.get('products', [])
        if not products or level == 'basic':
            return []
        max_items = 20 if level == 'standard' else 50
        els = [PageBreak(), self._h2(f"Товары в нише (топ {min(max_items, len(products))})"), self._hr()]

        rows = [['SKU', 'Название', 'Бренд', 'Цена', '★', 'Отзывов']]
        for p in products[:max_items]:
            rows.append([
                str(p.get('wb_sku', '')),
                str(p.get('name', ''))[:36],
                str(p.get('brand', 'N/A'))[:14],
                f"₽ {p.get('price', 0):,.0f}",
                f"{p.get('rating', 0):.1f}",
                str(p.get('reviews_count', 0)),
            ])

        t = Table(rows, colWidths=[0.8*inch, 2.5*inch, 1.1*inch, 0.9*inch, 0.5*inch, 0.8*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,0), C_GRAY),
            ('TEXTCOLOR',     (0,0), (-1,0), C_WHITE),
            ('FONTNAME',      (0,0), (-1,0), FB),
            ('FONTNAME',      (0,1), (-1,-1), FN),
            ('FONTSIZE',      (0,0), (-1,0), 9),
            ('FONTSIZE',      (0,1), (-1,-1), 8),
            ('ALIGN',         (0,0), (-1,-1), 'CENTER'),
            ('ALIGN',         (1,0), (1,-1), 'LEFT'),
            ('ALIGN',         (2,0), (2,-1), 'LEFT'),
            ('BOTTOMPADDING', (0,0), (-1,0), 7),
            ('TOPPADDING',    (0,1), (-1,-1), 4),
            ('BOTTOMPADDING', (0,1), (-1,-1), 4),
            ('GRID',          (0,0), (-1,-1), 0.4, C_BORDER),
            ('ROWBACKGROUNDS',(0,1), (-1,-1), [C_WHITE, C_LIGHT]),
        ]))
        els.append(t)
        return els

    def _sec_cta(self, level):
        if level != 'basic':
            return []
        return [
            self._sp(0.3), self._hr(),
            self._body(
                "<b>Хотите полный анализ?</b> Standard-отчёт включает развёрнутый анализ "
                "рынка, конкурентной среды, AI-гипотезы и рекомендации по входу. "
                "Deep Dive — полная стратегия входа, оценка рисков и таблица товаров."
            ),
        ]

    def _sec_dimensions(self, data):
        dims = data.get('dimensions', [])
        if not dims:
            return []
        els = [self._h2("Оценка по параметрам"), self._hr()]
        rows = [['Параметр', 'Балл', 'Комментарий']]
        for d in dims:
            mx = d.get('max_score', 100) or 100
            rows.append([str(d.get('name','')), f"{d.get('score',0)}/{mx}", str(d.get('reason',''))[:55]])
        t = Table(rows, colWidths=[2.0*inch, 0.8*inch, 3.8*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0),(-1,0), C_GRAY), ('TEXTCOLOR',(0,0),(-1,0),C_WHITE),
            ('FONTNAME',(0,0),(-1,0),FB), ('FONTNAME',(0,1),(-1,-1),FN),
            ('FONTSIZE',(0,0),(-1,-1),10), ('ALIGN',(0,0),(-1,-1),'LEFT'),
            ('GRID',(0,0),(-1,-1),0.5,C_BORDER),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[C_WHITE,C_LIGHT]),
            ('TOPPADDING',(0,1),(-1,-1),5), ('BOTTOMPADDING',(0,0),(-1,-1),6),
        ]))
        els.append(t)
        els.append(self._sp(0.15))
        return els

    # ── агентские секции ──────────────────────────────────────────────────────

    def _sec_supplier(self, ag: dict) -> list:
        s = ag.get('supplier') or {}
        if not s:
            return []
        els = [PageBreak(), self._h2("Поиск поставщиков и цены закупки"), self._hr()]

        taobao = float(s.get('price_taobao_usd', 0))
        alibaba = float(s.get('price_alibaba_usd', 0))
        moq = int(s.get('moq', 0))
        margin = float(s.get('real_margin_pct', 0))
        roi = float(s.get('roi_pct', 0))
        profit_unit = float(s.get('profit_per_unit_rub', 0))
        summary_text = str(s.get('summary', ''))

        rows = [['Параметр', 'Значение']]
        if taobao: rows.append(['Цена 1688 / Taobao', f"${taobao:.1f}"])
        if alibaba: rows.append(['Цена Alibaba (опт)', f"${alibaba:.1f}"])
        if moq: rows.append(['MOQ (мин. партия)', f"{moq} шт."])
        if margin: rows.append(['Маржа (реальная)', f"{margin:.0f}%"])
        if roi: rows.append(['ROI', f"{roi:.0f}%"])
        if profit_unit: rows.append(['Прибыль/ед.', _rub(profit_unit)])

        t = Table(rows, colWidths=[3.2*inch, 3.4*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,0), C_GRAY),
            ('TEXTCOLOR',     (0,0), (-1,0), C_WHITE),
            ('FONTNAME',      (0,0), (-1,0), FB),
            ('FONTNAME',      (0,1), (-1,-1), FN),
            ('FONTSIZE',      (0,0), (-1,-1), 10),
            ('ALIGN',         (0,0), (-1,-1), 'LEFT'),
            ('GRID',          (0,0), (-1,-1), 0.5, C_BORDER),
            ('ROWBACKGROUNDS',(0,1), (-1,-1), [C_WHITE, C_LIGHT]),
            ('TOPPADDING',    (0,1), (-1,-1), 5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ]))
        els.append(t)
        if summary_text:
            els.append(self._sp(0.15))
            for para in summary_text.split('. '):
                para = para.strip()
                if para:
                    els.append(self._body(para + '.'))
        els.append(self._sp(0.2))

        links = s.get('search_links') or []
        if links:
            els.append(self._h3("Площадки для поиска поставщиков:"))
            for lnk in links:
                platform = str(lnk.get('platform', ''))
                desc = str(lnk.get('description', ''))
                url = str(lnk.get('url', ''))
                els.append(self._bullet(f"<b>{platform}</b> — {desc}  ({url})"))
            els.append(self._sp(0.1))

        return els

    def _sec_advertising(self, ag: dict) -> list:
        a = ag.get('ad') or {}
        if not a:
            return []
        els = [PageBreak(), self._h2("Рекламная стратегия WB"), self._hr()]

        load_level = str(a.get('load_level', '')).lower()
        load_label = {'low': 'Низкая', 'medium': 'Умеренная', 'high': 'Высокая'}.get(load_level, load_level)
        load_hex = {'low': '10B981', 'medium': 'F59E0B', 'high': 'EF4444'}.get(load_level, 'F59E0B')

        els.append(KeepTogether([
            self._h3("Рекламная нагрузка в нише:"),
            Paragraph(f"<b><font color='#{load_hex}'>{load_label}</font></b>", self.styles['WBBody']),
            self._body(str(a.get('load_analysis', ''))),
            self._sp(0.1),
        ]))

        strategy_type = str(a.get('strategy_type', ''))
        strategy_detail = str(a.get('strategy_detail', ''))
        if strategy_type or strategy_detail:
            els.append(self._h3(f"Стратегия: {strategy_type}"))
            els.append(self._body(strategy_detail))
            steps = a.get('strategy_steps') or []
            for i, step in enumerate(steps, 1):
                els.append(self._bullet(f"Шаг {i}: {step}"))
            els.append(self._sp(0.1))

        budget = a.get('budget') or {}
        if budget:
            els.append(self._h3("Рекомендуемый рекламный бюджет:"))
            rows = [['Период', 'Бюджет']]
            if budget.get('start_rub'): rows.append(['Старт (1-й месяц)', _rub(budget['start_rub'])])
            if budget.get('growth_rub'): rows.append(['Рост (2-й месяц)', _rub(budget['growth_rub'])])
            if budget.get('sustain_rub'): rows.append(['Удержание', _rub(budget['sustain_rub'])])
            t = Table(rows, colWidths=[2.8*inch, 3.8*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0,0),(-1,0), C_GRAY), ('TEXTCOLOR',(0,0),(-1,0),C_WHITE),
                ('FONTNAME',(0,0),(-1,0),FB), ('FONTNAME',(0,1),(-1,-1),FN),
                ('FONTSIZE',(0,0),(-1,-1),10), ('ALIGN',(0,0),(-1,-1),'LEFT'),
                ('GRID',(0,0),(-1,-1),0.5,C_BORDER),
                ('ROWBACKGROUNDS',(0,1),(-1,-1),[C_WHITE,C_LIGHT]),
                ('TOPPADDING',(0,1),(-1,-1),5), ('BOTTOMPADDING',(0,0),(-1,-1),6),
            ]))
            els.append(t)
            if budget.get('comment'):
                els.append(self._small(str(budget['comment'])))
            els.append(self._sp(0.15))

        forecast = a.get('forecast') or {}
        cpm = a.get('cpm_forecast') or {}
        if forecast or cpm:
            els.append(self._h3("Прогноз результатов:"))
            if cpm:
                els.append(self._body(
                    f"CPM старт: {_rub(cpm.get('start_rub', 0))}  |  "
                    f"CPM через 2 мес: {_rub(cpm.get('month2_rub', 0))}"
                ))
                if cpm.get('comment'):
                    els.append(self._small(str(cpm['comment'])))
            m1 = (forecast.get('month1') or {}).get('metrics') or []
            m2 = (forecast.get('month2') or {}).get('metrics') or []
            if m1:
                els.append(self._h3("Месяц 1 — ожидаемые показатели:"))
                for m in m1:
                    els.append(self._bullet(str(m)))
            if m2:
                els.append(self._h3("Месяц 2 — ожидаемые показатели:"))
                for m in m2:
                    els.append(self._bullet(str(m)))
            els.append(self._sp(0.1))

        return els

    def _sec_content_card(self, ag: dict) -> list:
        raw = str(ag.get('content_raw', '') or '')
        if not raw.strip():
            return []
        els = [PageBreak(), self._h2("Карточка товара — контент от AI"), self._hr()]

        current_section = None
        for line in raw.split('\n'):
            line = line.strip()
            if not line:
                continue
            if line and line[0].isdigit() and len(line) > 2 and line[1] == '.':
                current_section = line
                els.append(self._h3(line))
            elif line.startswith('-') or line.startswith('•'):
                els.append(self._bullet(line.lstrip('-•').strip()))
            elif line.startswith('**') and line.endswith('**'):
                els.append(self._h3(line.strip('*')))
            else:
                els.append(self._body(line))

        els.append(self._sp(0.1))
        return els

    def _sec_documents(self, ag: dict) -> list:
        d = ag.get('docs') or {}
        if not d:
            return []
        els = [PageBreak(), self._h2("Документы и сертификация"), self._hr()]

        complexity = str(d.get('complexity', 'medium')).lower()
        comp_label = {'low': 'Простая', 'medium': 'Умеренная', 'high': 'Сложная'}.get(complexity, complexity)
        comp_color = {'low': C_GREEN, 'medium': C_YELLOW, 'high': C_RED}.get(complexity, C_YELLOW)
        comp_reason = str(d.get('complexity_reason', ''))
        summary_text = str(d.get('summary', ''))

        els.append(self._h3(f"Сложность оформления: {comp_label}"))
        if comp_reason:
            els.append(self._body(comp_reason))
        if summary_text:
            els.append(self._body(summary_text))
        els.append(self._sp(0.1))

        wb_docs = list(d.get('wb_docs') or [])
        if wb_docs:
            els.append(self._h3("Документы для Wildberries:"))
            rows = [['Документ', 'Стоимость', 'Срок', 'Обязательность']]
            for doc in wb_docs:
                req = 'Обязательно' if doc.get('required') else 'Желательно'
                rows.append([
                    str(doc.get('name', ''))[:40],
                    str(doc.get('cost', '—'))[:16],
                    str(doc.get('duration', '—'))[:14],
                    req,
                ])
            t = Table(rows, colWidths=[2.6*inch, 1.2*inch, 1.1*inch, 1.7*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0,0),(-1,0), C_GRAY), ('TEXTCOLOR',(0,0),(-1,0),C_WHITE),
                ('FONTNAME',(0,0),(-1,0),FB), ('FONTNAME',(0,1),(-1,-1),FN),
                ('FONTSIZE',(0,0),(-1,0), 9), ('FONTSIZE',(0,1),(-1,-1), 8),
                ('ALIGN',(0,0),(-1,-1),'LEFT'),
                ('GRID',(0,0),(-1,-1),0.5,C_BORDER),
                ('ROWBACKGROUNDS',(0,1),(-1,-1),[C_WHITE,C_LIGHT]),
                ('TOPPADDING',(0,1),(-1,-1),4), ('BOTTOMPADDING',(0,0),(-1,-1),5),
            ]))
            els.append(t)
            els.append(self._sp(0.15))

            for doc in wb_docs:
                if doc.get('description'):
                    els.append(self._small(f"• {doc.get('name','')}: {doc.get('description','')}"))
            els.append(self._sp(0.1))

        customs_docs = list(d.get('customs_docs') or [])
        if customs_docs:
            els.append(self._h3("Документы для таможни (ввоз из Китая):"))
            for doc in customs_docs:
                els.append(self._bullet(f"<b>{doc.get('name','')}</b> — {doc.get('description','')}"))
            els.append(self._sp(0.1))

        risks = list(d.get('risks') or [])
        if risks:
            els.append(self._h3("Риски и как их избежать:"))
            for r in risks:
                risk_text = str(r.get('risk', ''))
                solution = str(r.get('solution', ''))
                els.append(self._bullet(f"<b>{risk_text}</b>"))
                if solution:
                    els.append(Paragraph(f"   → {solution}", self.styles['WBSmall']))
            els.append(self._sp(0.1))

        total_cost = str(d.get('total_cost_byn') or d.get('total_cost') or '')
        total_dur = str(d.get('total_duration', ''))
        if total_cost or total_dur:
            rows2 = [['Итоговые затраты', 'Общий срок']]
            rows2.append([total_cost or '—', total_dur or '—'])
            t2 = Table(rows2, colWidths=[3.3*inch, 3.3*inch])
            t2.setStyle(TableStyle([
                ('BACKGROUND',(0,0),(-1,0),C_GRAY), ('TEXTCOLOR',(0,0),(-1,0),C_WHITE),
                ('FONTNAME',(0,0),(-1,0),FB), ('FONTNAME',(0,1),(-1,-1),FN),
                ('FONTSIZE',(0,0),(-1,-1),10),
                ('ALIGN',(0,0),(-1,-1),'CENTER'),
                ('GRID',(0,0),(-1,-1),0.5,C_BORDER),
                ('TOPPADDING',(0,1),(-1,-1),7), ('BOTTOMPADDING',(0,0),(-1,-1),7),
            ]))
            els.append(t2)
            els.append(self._sp(0.1))

        return els

    # ── главный метод ─────────────────────────────────────────────────────────

    def generate_pdf(self, data: dict, level: str = "standard") -> BytesIO:
        buf = BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=A4,
            rightMargin=0.55*inch, leftMargin=0.55*inch,
            topMargin=0.7*inch,   bottomMargin=0.55*inch,
        )
        m       = data.get('metrics', {})
        ai      = data.get('ai_insights', {})
        verdict = (data.get('verdict') or 'TEST').upper()
        score   = data.get('score', 0)
        cat     = data.get('category', '')

        ag = data.get('agents', {}) or {}

        els = []
        els += self._sec_header(data, level)
        els += self._sec_metrics(m, level)

        if level in ('standard', 'deep'):
            els += self._sec_market_analysis(m, cat)
            els += self._sec_competition(m)

        els += self._sec_ai_insights(ai, level)

        if level in ('standard', 'deep'):
            els += self._sec_recommendations(m, verdict, score)

        if level == 'deep':
            els += self._sec_risks(m, verdict)
            els += self._sec_strategy(m, verdict, cat)
            els += self._sec_dimensions(data)
            # Секции агентов
            els += self._sec_supplier(ag)
            els += self._sec_advertising(ag)
            els += self._sec_content_card(ag)
            els += self._sec_documents(ag)

        els += self._sec_charts(data, level)
        els += self._sec_products(data, level)
        els += self._sec_cta(level)

        doc.build(els)
        buf.seek(0)
        return buf


def create_pdf_from_analysis(analysis_response: dict, level: str = "standard") -> BytesIO:
    return PDFReportGenerator().generate_pdf(analysis_response, level=level)
