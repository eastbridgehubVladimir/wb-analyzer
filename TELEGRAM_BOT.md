# WBAnalyzer Telegram Bot

Отдельный процесс — не зависит от основного app.py. Та же БД, тот же pdf_auto.

## Быстрый старт (локально)

```bash
cd ~/wb-saas
venv311/bin/python3 telegram_bot.py
```

Логи пишутся в stdout. Для фонового запуска:

```bash
venv311/bin/python3 telegram_bot.py > /tmp/tgbot.log 2>&1 &
```

## Переменные в .env

```
TELEGRAM_BOT_TOKEN=...        # токен от @BotFather
TELEGRAM_SUPPORT_USERNAME=... # username для заглушки оплаты (без @)
TELEGRAM_ADMIN_ID=            # числовой ID админа — узнать через @userinfobot
DATABASE_URL=...              # та же БД что у app.py
ANTHROPIC_API_KEY=...         # нужен для генерации PDF
PAYMENT_PROVIDER=yukassa      # провайдер (пока не используется)
YUKASSA_SHOP_ID=              # оставить пустым до подключения ЮKassa
YUKASSA_SECRET_KEY=           # оставить пустым до подключения ЮKassa
```

## Деплой на Railway (отдельный сервис)

1. В Railway → New Service → "Empty service"
2. Привязать тот же GitHub репозиторий
3. В настройках сервиса задать **Start Command**:
   ```
   python3 telegram_bot.py
   ```
4. Добавить те же переменные окружения что в .env
5. Railway автоматически установит зависимости из requirements.txt

> Добавить в requirements.txt: `aiogram==3.13.0`

## Что умеет бот

| Команда / действие | Результат |
|--------------------|-----------|
| `/start` | Приветствие + инструкция; если `/start ref_xxx` — сохраняет источник |
| `/start ref_xxx` | Реферальная ссылка: ref_source записывается во все лиды/заказы |
| `/help` | Справка по использованию |
| `/stats` | Статистика (только для TELEGRAM_ADMIN_ID) |
| Любой текст | Поиск ниши → превью метрик |
| Кнопка Basic PDF | Создаёт заказ (free) → генерирует PDF → отправляет |
| Кнопка Standard/Deep | Создаёт заказ (pending) → заглушка оплаты + контакт поддержки |

## Аналитика лидов

Все действия пишутся в таблицу `telegram_leads`:

```sql
SELECT action, COUNT(*) FROM telegram_leads GROUP BY action ORDER BY count DESC;
```

| action | Когда записывается |
|--------|--------------------|
| `start` | Пользователь запустил бота |
| `search` | Пользователь ввёл запрос |
| `preview_shown` | Показано превью ниши |
| `basic_generating` | Начата генерация Basic PDF |
| `basic_downloaded` | PDF успешно отправлен |
| `standard_clicked` | Клик на Standard (заглушка) |
| `deep_clicked` | Клик на Deep (заглушка) |

## Трекинг заказов

Таблица `orders` — все PDF-запросы:

```sql
SELECT pdf_level, payment_status, COUNT(*) FROM orders GROUP BY 1, 2 ORDER BY 3 DESC;
```

Реферальные ссылки формируются как:
```
https://t.me/Wbanalyzer_user_bot?start=ref_НАЗВАНИЕ
```

## Следующие шаги

- [ ] Добавить реальный ID в `TELEGRAM_ADMIN_ID` (через @userinfobot)
- [ ] Подключить реальный эквайринг (ЮKassa / Telegram Payments / USDT)
- [ ] Реализовать `fulfill_order()` после выбора провайдера
- [ ] Добавить Webhook для Railway вместо polling
- [ ] Добавить команду `/orders` для просмотра своих отчётов
