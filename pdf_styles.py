"""
pdf_styles.py — Единый источник стилей, цветов и шрифтов для PDF-отчётов WBAnalyzer.

Чтобы изменить дизайн — редактируй только этот файл.
"""
from reportlab.lib.colors import HexColor, white

# ── Основная палитра ──────────────────────────────────────────────────────────
C_COVER_BG  = HexColor('#0d1b2a')   # фон обложки и финала
C_COVER_SUB = HexColor('#94a3b8')   # подзаголовок на обложке
C_NAVY      = HexColor('#1a2f5e')   # заголовки разделов
C_ACCENT    = HexColor('#2563eb')   # акцентный синий
C_BLUE2     = HexColor('#3b82f6')   # иконки, ссылки
C_GREEN     = HexColor('#16a34a')   # позитивные показатели
C_RED       = HexColor('#dc2626')   # опасность
C_ORANGE    = HexColor('#ea580c')   # предупреждения
C_AMBER     = HexColor('#d97706')   # среднее
C_GOLD      = HexColor('#b45309')   # Deep акцент
C_TEXT      = HexColor('#1e293b')   # основной текст
C_GRAY      = HexColor('#64748b')   # вторичный текст
C_LIGHT_BG  = HexColor('#f1f5f9')   # фон заголовков секций
C_TABLE_ODD = HexColor('#f8fafc')   # нечётные строки таблиц
C_BORDER    = HexColor('#e2e8f0')   # тонкие линии
C_WARN_BG   = HexColor('#fff7ed')   # фон предупреждений
C_INFO_BG   = HexColor('#eff6ff')   # фон подсказок
C_DARK_CARD = HexColor('#1e293b')   # тёмный фон на финальной странице
WHITE       = white

# ── Цвета по уровням ─────────────────────────────────────────────────────────
LEVEL_ACCENT = {
    'basic':    HexColor('#64748b'),  # серебристо-серый
    'standard': HexColor('#2563eb'),  # синий
    'deep':     HexColor('#b45309'),  # золотой
}

LEVEL_BADGE_BG = {
    'basic':    HexColor('#475569'),  # тёмно-серый
    'standard': HexColor('#1d4ed8'),  # тёмно-синий
    'deep':     HexColor('#78350f'),  # тёмно-золотой
}

LEVEL_NAMES = {
    'basic':    'PDF Basic',
    'standard': 'PDF Standard',
    'deep':     'PDF Deep',
}

LEVEL_SUBTITLES = {
    'basic':    'Базовый анализ · бесплатно',
    'standard': 'Расширенный анализ · профессиональный',
    'deep':     'Полный профессиональный анализ · максимум данных',
}

LEVEL_UPSELL_NEXT = {
    'basic':    ('PDF Standard', 'Расширенный анализ', HexColor('#1d4ed8')),
    'standard': ('PDF Deep',     'Полный анализ',      HexColor('#78350f')),
    'deep':     None,
}

# ── Типографика (размеры шрифтов) ─────────────────────────────────────────────
FS_COVER_LOGO  = 36    # WBAnalyzer на обложке
FS_COVER_NICHE = 28    # название ниши на обложке
FS_H2          = 13    # заголовок раздела
FS_H3          = 10    # подзаголовок
FS_BODY        = 9.5   # основной текст
FS_CAPTION     = 8.5   # подписи к графикам
FS_SMALL       = 8     # мелкий текст

# ── Ссылка на платформу ───────────────────────────────────────────────────────
PLATFORM_URL = 'wb-analyzer-production.up.railway.app'
PLATFORM_YEAR = '2026'
