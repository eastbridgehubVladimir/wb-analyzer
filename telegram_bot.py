#!/usr/bin/env python3
"""
WBAnalyzer Telegram Bot — MVP с инфраструктурой для оплат
Запуск: cd ~/wb-saas && venv311/bin/python3 telegram_bot.py
"""
import asyncio
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
DB_URL   = os.getenv("DATABASE_URL")
SUPPORT  = os.getenv("TELEGRAM_SUPPORT_USERNAME", "")
ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))

if not TOKEN:
    sys.exit("❌ TELEGRAM_BOT_TOKEN не задан в .env")
if not DB_URL:
    sys.exit("❌ DATABASE_URL не задан в .env")

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup, Message,
    ReplyKeyboardRemove, WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

sys.path.insert(0, os.path.dirname(__file__))
import pdf_auto

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

executor = ThreadPoolExecutor(max_workers=4)

PRICES  = {"standard": 2500, "deep": 6000}
PRICES_STR = {"standard": "2 500 ₽", "deep": "6 000 ₽"}

# ── БД: ниши ───────────────────────────────────────────────────────────────────

_SELECT_COLS = """
    id, name, products, products_with_sales, sellers, sellers_with_sales,
    revenue, potential_revenue, lost_revenue, lost_revenue_pct, orders,
    buyout_pct, turnover, profit_pct, avg_rating, rank, commission, avg_price,
    data_updated_at, revenue_with_buyout,
    COALESCE(display_name, name) AS display_name, mpstats_path
"""

def _row_to_niche(row) -> dict:
    return {
        'id':                  row[0],
        'name':                row[1],
        'full':                row[1],
        'products':            int(row[2]  or 0),
        'products_with_sales': int(row[3]  or 0),
        'sellers':             int(row[4]  or 0),
        'sellers_with_sales':  int(row[5]  or 0),
        'revenue':             float(row[6]  or 0),
        'potential_revenue':   float(row[7]  or 0) if row[7] else None,
        'lost_revenue':        float(row[8]  or 0) if row[8] else None,
        'lost_revenue_pct':    float(row[9]  or 0),
        'orders':              int(row[10] or 0),
        'buyout_pct':          float(row[11] or 0),
        'turnover':            float(row[12] or 0),
        'profit_pct':          float(row[13] or 0),
        'avg_rating':          float(row[14] or 0),
        'rank':                row[15],
        'commission':          float(row[16] or 0),
        'avg_price':           float(row[17] or 0),
        'data_updated_at':     row[18].strftime('%d.%m.%Y') if row[18] else None,
        'revenue_with_buyout': float(row[19] or 0),
        'display_name':        row[20],
        'mpstats_path':        row[21],
        'revenue_annual':      (float(row[19] or 0) or float(row[6] or 0)) * 12,
    }

def _search_niches(query: str) -> list:
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute(f"""
        SELECT {_SELECT_COLS} FROM niches
        WHERE (LOWER(name) ILIKE LOWER(%s) OR LOWER(COALESCE(display_name,name)) ILIKE LOWER(%s))
          AND revenue IS NOT NULL
        ORDER BY revenue DESC LIMIT 5
    """, (f"%{query}%", f"%{query}%"))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [_row_to_niche(r) for r in rows]

def _get_niche_by_id(niche_id: int):
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute(f"SELECT {_SELECT_COLS} FROM niches WHERE id = %s", (niche_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return _row_to_niche(row) if row else None

# ── БД: лиды ──────────────────────────────────────────────────────────────────

def _log_lead(user_id: int, username, niche_name, action: str, ref_source: str = 'direct'):
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO telegram_leads "
            "(telegram_user_id, telegram_username, niche_searched, action, ref_source)"
            " VALUES (%s, %s, %s, %s, %s)",
            (user_id, username, niche_name, action, ref_source)
        )
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        log.warning(f"lead log failed: {e}")

# ── БД: заказы ────────────────────────────────────────────────────────────────

def _create_order(user_id: int, username, niche_name: str,
                  level: str, amount: int, ref_source: str = 'direct') -> int:
    """Создаёт запись в orders, возвращает order_id."""
    status = 'free' if amount == 0 else 'pending'
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO orders "
        "(telegram_user_id, telegram_username, niche_name, pdf_level, amount, payment_status, ref_source)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (user_id, username, niche_name, level, amount, status, ref_source)
    )
    order_id = cur.fetchone()[0]
    conn.commit()
    cur.close(); conn.close()
    return order_id

def _mark_pdf_sent(order_id: int):
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute("UPDATE orders SET pdf_sent = TRUE WHERE id = %s", (order_id,))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        log.warning(f"mark_pdf_sent failed: {e}")

def _get_order(order_id: int) -> dict | None:
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, telegram_user_id, niche_name, pdf_level, payment_status, pdf_sent"
        " FROM orders WHERE id = %s", (order_id,)
    )
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return None
    return {
        'id': row[0], 'telegram_user_id': row[1], 'niche_name': row[2],
        'pdf_level': row[3], 'payment_status': row[4], 'pdf_sent': row[5],
    }

# ── БД: статистика ────────────────────────────────────────────────────────────

def _get_stats() -> dict:
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(DISTINCT telegram_user_id) FROM telegram_leads")
    total_users = cur.fetchone()[0]

    cur.execute("SELECT COUNT(DISTINCT telegram_user_id) FROM telegram_leads WHERE created_at >= NOW() - INTERVAL '1 day'")
    today_users = cur.fetchone()[0]

    cur.execute("SELECT COUNT(DISTINCT telegram_user_id) FROM telegram_leads WHERE created_at >= NOW() - INTERVAL '7 days'")
    week_users = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM orders WHERE pdf_level='basic' AND pdf_sent=TRUE")
    basic_sent = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM orders WHERE pdf_level='standard' AND payment_status='pending'")
    std_pending = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM orders WHERE pdf_level='deep' AND payment_status='pending'")
    deep_pending = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM orders WHERE payment_status='paid'")
    paid = cur.fetchone()[0]

    cur.execute("""
        SELECT niche_searched, COUNT(*) as cnt
        FROM telegram_leads
        WHERE niche_searched IS NOT NULL
          AND created_at >= NOW() - INTERVAL '7 days'
        GROUP BY niche_searched ORDER BY cnt DESC LIMIT 5
    """)
    top_niches = cur.fetchall()

    cur.execute("""
        SELECT ref_source, COUNT(DISTINCT telegram_user_id) as cnt
        FROM telegram_leads
        GROUP BY ref_source ORDER BY cnt DESC LIMIT 10
    """)
    sources = cur.fetchall()

    conn.close()
    return {
        'total_users': total_users, 'today_users': today_users, 'week_users': week_users,
        'basic_sent': basic_sent, 'std_pending': std_pending, 'deep_pending': deep_pending,
        'paid': paid, 'top_niches': top_niches, 'sources': sources,
    }

# ── Генерация PDF ─────────────────────────────────────────────────────────────

def _generate_pdf(level: str, niche: dict) -> bytes:
    return pdf_auto.generate(level, niche)

# ── Форматирование ─────────────────────────────────────────────────────────────

def _fmt_money(v: float) -> str:
    if v >= 1_000_000_000: return f"{v/1_000_000_000:.1f} млрд ₽"
    if v >= 1_000_000:     return f"{v/1_000_000:.0f} млн ₽"
    return f"{v/1_000:.0f} тыс ₽"

def _preview_text(n: dict) -> str:
    rev = n.get('revenue_annual') or n.get('revenue_with_buyout', 0) * 12
    return (
        f"📊 <b>{n['display_name']}</b>\n\n"
        f"💰 Выручка ниши:    <b>{_fmt_money(rev)}/год</b>\n"
        f"🛒 Заказов/месяц:  <b>{n['orders']:,}</b>\n"
        f"👥 Продавцов:       <b>{n['sellers_with_sales']}</b> активных из {n['sellers']}\n"
        f"✅ Выкуп:           <b>{n['buyout_pct']*100:.0f}%</b>\n"
        f"📅 Данные на: {n.get('data_updated_at', '—')}"
    )

def _preview_kb(niche_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Basic PDF (бесплатно)", callback_data=f"pdf:basic:{niche_id}")],
        [
            InlineKeyboardButton(text="📊 Standard PDF", callback_data=f"pdf:standard:{niche_id}"),
            InlineKeyboardButton(text="📈 Deep PDF",     callback_data=f"pdf:deep:{niche_id}"),
        ],
    ])

def _list_kb(niches: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=n['display_name'], callback_data=f"select:{n['id']}")]
        for n in niches
    ])

# ── Handlers ───────────────────────────────────────────────────────────────────

router = Router()

# Храним ref_source для каждого user_id в памяти (сбрасывается при рестарте бота)
_user_ref: dict[int, str] = {}

@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    args = message.text.split(maxsplit=1)
    ref_source = args[1].strip() if len(args) > 1 else 'direct'
    _user_ref[user.id] = ref_source

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        executor, _log_lead, user.id, user.username, None, 'start', ref_source
    )

    # Убираем любой стейл reply keyboard из предыдущих сессий бота
    rm = await message.answer("👋", reply_markup=ReplyKeyboardRemove())
    await rm.delete()

    builder = InlineKeyboardBuilder()
    builder.button(
        text="📊 Открыть WBAnalyzer",
        web_app=WebAppInfo(url="https://wb-analyzer-production.up.railway.app/miniapp")
    )

    await message.answer(
        "👋 Добро пожаловать в *WBAnalyzer*!\n\n"
        "Анализируй любую нишу Wildberries за 2 минуты "
        "и получай готовый PDF-отчёт.\n\n"
        "📄 *Basic* — бесплатно\n"
        "Вердикт AI, ключевые метрики, 2 графика\n\n"
        "📊 *Standard* — 2 500 ₽\n"
        "Мастер-анализ AI, 5 графиков, топ-20 товаров, "
        "юнит-экономика, реклама, сезонный план\n\n"
        "📈 *Deep* — 6 000 ₽\n"
        "Всё из Standard + конкуренты, поставщики, "
        "FBO/FBS стратегия, карточка товара, 90-дн. план\n\n"
        "👇 Нажми чтобы начать:",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )

@router.message(Command("help"))
async def cmd_help(message: Message):
    support_line = f"\n❓ Вопросы и заказы: @{SUPPORT}" if SUPPORT else ""
    await message.answer(
        "📖 <b>Как пользоваться:</b>\n\n"
        "1️⃣ Напиши название товара/ниши\n"
        "2️⃣ Выбери нишу из списка (если нашлось несколько)\n"
        "3️⃣ Посмотри превью с ключевыми метриками\n"
        "4️⃣ Скачай <b>Basic PDF</b> бесплатно\n"
        "   или закажи <b>Standard / Deep</b> для глубокого анализа\n\n"
        "📄 Basic — базовые метрики ниши\n"
        "📊 Standard — полный анализ + юнит-экономика + реклама\n"
        "📈 Deep — всё выше + конкуренты + поставщики + стратегия"
        + support_line,
        parse_mode="HTML"
    )

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        return  # тихо игнорируем — не сообщаем что команда существует

    loop = asyncio.get_event_loop()
    s = await loop.run_in_executor(executor, _get_stats)

    top = "\n".join(
        f"  {i+1}. {row[0]} — {row[1]} запросов"
        for i, row in enumerate(s['top_niches'])
    ) or "  нет данных"

    src = "\n".join(
        f"  {row[0] or 'direct'}: {row[1]} польз."
        for row in s['sources']
    ) or "  нет данных"

    await message.answer(
        "📊 <b>Статистика WBAnalyzer</b>\n\n"
        "<b>Пользователи:</b>\n"
        f"  Всего уникальных: {s['total_users']}\n"
        f"  Сегодня новых: {s['today_users']}\n"
        f"  За 7 дней: {s['week_users']}\n\n"
        "<b>Заказы:</b>\n"
        f"  Basic PDF выдано: {s['basic_sent']}\n"
        f"  Standard кликов (pending): {s['std_pending']}\n"
        f"  Deep кликов (pending): {s['deep_pending']}\n"
        f"  Оплачено: {s['paid']}\n\n"
        f"<b>Топ ниш за 7 дней:</b>\n{top}\n\n"
        f"<b>Источники трафика:</b>\n{src}",
        parse_mode="HTML"
    )

@router.message(F.text & ~F.text.startswith("/"))
async def handle_search(message: Message):
    query = message.text.strip()
    if len(query) < 2:
        await message.answer("Введите не менее 2 символов для поиска.")
        return

    user = message.from_user
    ref = _user_ref.get(user.id, 'direct')
    loop = asyncio.get_event_loop()

    niches = await loop.run_in_executor(executor, _search_niches, query)
    await loop.run_in_executor(executor, _log_lead, user.id, user.username, query, 'search', ref)

    if not niches:
        await message.answer(
            f"🔍 По запросу «<b>{query}</b>» ничего не найдено.\n\n"
            "Попробуйте: <i>смартфоны, детские игрушки, кофемашины</i>",
            parse_mode="HTML"
        )
        return

    if len(niches) == 1:
        n = niches[0]
        await loop.run_in_executor(executor, _log_lead, user.id, user.username, n['name'], 'preview_shown', ref)
        await message.answer(_preview_text(n), reply_markup=_preview_kb(n['id']), parse_mode="HTML")
    else:
        await message.answer(
            f"🔍 По запросу «<b>{query}</b>» найдено {len(niches)} ниши — выберите:",
            reply_markup=_list_kb(niches),
            parse_mode="HTML"
        )

@router.callback_query(F.data.startswith("select:"))
async def cb_select(callback: CallbackQuery):
    niche_id = int(callback.data.split(":")[1])
    user = callback.from_user
    ref = _user_ref.get(user.id, 'direct')
    loop = asyncio.get_event_loop()
    n = await loop.run_in_executor(executor, _get_niche_by_id, niche_id)
    if not n:
        await callback.answer("Ниша не найдена", show_alert=True)
        return
    await loop.run_in_executor(executor, _log_lead, user.id, user.username, n['name'], 'preview_shown', ref)
    await callback.message.edit_text(_preview_text(n), reply_markup=_preview_kb(niche_id), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("pdf:"))
async def cb_pdf(callback: CallbackQuery):
    parts = callback.data.split(":")
    level, niche_id = parts[1], int(parts[2])
    user = callback.from_user
    ref = _user_ref.get(user.id, 'direct')
    loop = asyncio.get_event_loop()

    n = await loop.run_in_executor(executor, _get_niche_by_id, niche_id)
    if not n:
        await callback.answer("Ниша не найдена", show_alert=True)
        return

    if level in ("standard", "deep"):
        # Создаём запись заказа (pending) и показываем заглушку
        await loop.run_in_executor(
            executor, _create_order,
            user.id, user.username, n['name'], level, PRICES[level], ref
        )
        await loop.run_in_executor(executor, _log_lead, user.id, user.username, n['name'], f"{level}_clicked", ref)
        await callback.answer()
        await callback.message.answer(
            f"💳 <b>{level.capitalize()} PDF</b> — {PRICES_STR[level]}\n\n"
            "Оплата временно недоступна — мы дорабатываем платёжную систему.\n\n"
            f"Напишите <b>@{SUPPORT}</b> — передадим отчёт вручную в течение нескольких часов.",
            parse_mode="HTML"
        )
        return

    # Basic PDF: создаём заказ (free) → генерируем → отправляем → отмечаем pdf_sent
    await callback.answer("Генерирую PDF...")
    order_id = await loop.run_in_executor(
        executor, _create_order,
        user.id, user.username, n['name'], 'basic', 0, ref
    )
    await loop.run_in_executor(executor, _log_lead, user.id, user.username, n['name'], 'basic_generating', ref)

    progress = await callback.message.answer("⏳ Генерирую Basic PDF, подождите ~1 минуту...")
    try:
        pdf_bytes = await loop.run_in_executor(executor, _generate_pdf, "basic", n)
        fname = f"WBAnalyzer_{n['name'].replace('/', '-')}_Basic.pdf"

        await loop.run_in_executor(executor, _mark_pdf_sent, order_id)
        await loop.run_in_executor(executor, _log_lead, user.id, user.username, n['name'], 'basic_downloaded', ref)

        await callback.message.answer_document(
            BufferedInputFile(pdf_bytes, filename=fname),
            caption=(
                f"📄 <b>Basic PDF:</b> {n['display_name']}\n\n"
                "Хотите глубокий анализ? Закажите Standard или Deep отчёт."
            ),
            parse_mode="HTML"
        )
        await progress.delete()
    except Exception as e:
        log.error(f"PDF error for {n['name']}: {e}")
        await progress.edit_text("❌ Ошибка при генерации PDF. Попробуйте позже.")

# ── Функция выдачи PDF после оплаты (заготовка для эквайринга) ────────────────

async def fulfill_order(order_id: int):
    """
    Вызывается когда платёж подтверждён провайдером (ЮKassa/CloudPayments).
    Сейчас не используется — эквайринг подключается позже.

    Порядок подключения:
    1. Вебхук провайдера → /webhook/payment в app.py
    2. app.py обновляет orders.payment_status = 'paid'
    3. app.py вызывает эту функцию через HTTP или напрямую если бот и сервер в одном процессе

    Логика:
    1. Найти заказ по order_id
    2. Проверить payment_status = 'paid' и pdf_sent = FALSE
    3. Найти нишу по niche_name
    4. Сгенерировать PDF нужного уровня
    5. Отправить пользователю в Telegram
    6. Обновить pdf_sent = TRUE
    """
    loop = asyncio.get_event_loop()
    order = await loop.run_in_executor(executor, _get_order, order_id)
    if not order:
        log.error(f"fulfill_order: заказ {order_id} не найден")
        return
    if order['payment_status'] != 'paid' or order['pdf_sent']:
        log.warning(f"fulfill_order: заказ {order_id} не готов к выдаче")
        return

    niches = await loop.run_in_executor(executor, _search_niches, order['niche_name'])
    if not niches:
        log.error(f"fulfill_order: ниша '{order['niche_name']}' не найдена")
        return
    n = niches[0]

    try:
        pdf_bytes = await loop.run_in_executor(executor, _generate_pdf, order['pdf_level'], n)
        fname = f"WBAnalyzer_{n['name'].replace('/', '-')}_{order['pdf_level'].capitalize()}.pdf"
        bot = Bot(token=TOKEN)
        await bot.send_document(
            order['telegram_user_id'],
            BufferedInputFile(pdf_bytes, filename=fname),
            caption=f"✅ Ваш {order['pdf_level'].capitalize()} PDF готов!\n{n['display_name']}"
        )
        await bot.session.close()
        await loop.run_in_executor(executor, _mark_pdf_sent, order_id)
        log.info(f"fulfill_order: PDF отправлен для заказа {order_id}")
    except Exception as e:
        log.error(f"fulfill_order error: {e}")

# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    log.info("WBAnalyzer bot запущен, polling...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
