"""
Sourcing Intelligence System — определяет оптимальные каналы закупки
для любой WB-ниши на основе сигналов, а не страновых стереотипов.

Логика:
  NicheData → SourcingSignals → CountryScores → Top-N Options → AI Narrative
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Optional

# ─────────────────────────────────────────────
#  КАТАЛОГ ПЛАТФОРМ ПО СТРАНАМ
# ─────────────────────────────────────────────

PLATFORMS: dict[str, list[dict]] = {
    "china": [
        {
            "name": "1688.com",
            "url_tpl": "https://s.1688.com/selloffer/offer_search.htm?keywords={cn}",
            "note": "Оптовые цены напрямую от китайских заводов. Нужен аккаунт или агент.",
            "moq": "от 50–500 шт",
        },
        {
            "name": "Alibaba.com",
            "url_tpl": "https://www.alibaba.com/trade/search?SearchText={en}",
            "note": "Международные заводы, есть Trade Assurance, часть поставщиков работает с РФ/РБ напрямую.",
            "moq": "от 100–1000 шт",
        },
        {
            "name": "Made-in-China.com",
            "url_tpl": "https://www.made-in-china.com/products-search/hot-china-products/{en}.html",
            "note": "Альтернатива Alibaba, часто находятся нишевые производители.",
            "moq": "от 100 шт",
        },
        {
            "name": "Pinduoduo / Temu",
            "url_tpl": "https://mobile.yangkeduo.com/search_result.html?search_key={cn}",
            "note": "Мелкий опт и тест партий. Полезно для проверки товара до крупного заказа.",
            "moq": "от 1 шт",
        },
        {
            "name": "Агенты в Китае (Yiwi, Guangzhou)",
            "url_tpl": None,
            "note": "Supplyia, Chinabrands, личные агенты — берут 5–10% комиссию, решают вопросы QC и отправки.",
            "moq": "от 1 упаковки",
        },
    ],
    "turkey": [
        {
            "name": "Toptancı.com.tr",
            "url_tpl": "https://www.toptanci.com.tr/arama?q={tr}",
            "note": "Крупнейший турецкий оптовый агрегатор. Одежда, текстиль, кожа, галантерея.",
            "moq": "от 12–50 шт (по размерной линейке)",
        },
        {
            "name": "Modanisa / B2B Istanbul",
            "url_tpl": "https://www.trendyol.com/sr?q={tr}&stype=1",
            "note": "Тренди.com — турецкий маркетплейс. Смотри на B2B-производителей (ищи «toptan» в названии).",
            "moq": "от 24 шт",
        },
        {
            "name": "Laleli / Merter (Стамбул)",
            "url_tpl": None,
            "note": "Физические рынки в Стамбуле. Laleli — масс-маркет одежда, Merter — более качественный текстиль. Закупка через агента или поездка. Агенты берут 8–12%.",
            "moq": "от 1 упаковки (~6–12 шт)",
        },
        {
            "name": "Made in Turkey (TIM)",
            "url_tpl": "https://www.tim.org.tr/en/search?q={en}",
            "note": "Ассоциация турецких экспортёров — проверенные производители с сертификатами.",
            "moq": "от 100 шт",
        },
        {
            "name": "Alibaba Turkey",
            "url_tpl": "https://www.alibaba.com/trade/search?SearchText={en}&country=TR",
            "note": "Турецкие поставщики на Alibaba — удобно для первого контакта на английском.",
            "moq": "от 50–200 шт",
        },
    ],
    "russia": [
        {
            "name": "Пульс цен (puls-cen.ru)",
            "url_tpl": "https://puls-cen.ru/search/?q={ru}",
            "note": "Крупнейший агрегатор российских оптовых поставщиков. Цены обновляются ежедневно.",
            "moq": "от 1 упаковки",
        },
        {
            "name": "Optom.ru",
            "url_tpl": "https://www.optom.ru/search/?q={ru}",
            "note": "Оптовый каталог производителей РФ. Хорошо для еды, химии, cosметики.",
            "moq": "от 1 коробки",
        },
        {
            "name": "Produced.ru",
            "url_tpl": "https://produced.ru/search?q={ru}",
            "note": "База российских производителей с контактами, ценами, условиями. Фильтр по регионам.",
            "moq": "от 1 партии",
        },
        {
            "name": "Tiu.ru",
            "url_tpl": "https://tiu.ru/search/?text={ru}",
            "note": "Универсальный B2B-маркетплейс РФ. Есть и производители, и дистрибьюторы.",
            "moq": "разный",
        },
        {
            "name": "Авито Бизнес / Авито Опт",
            "url_tpl": "https://www.avito.ru/rossiya?q={ru}&s=104",
            "note": "Небольшие производства и ИП. Хорошо для уникальных и хендмейд позиций.",
            "moq": "от 1 шт",
        },
        {
            "name": "Иваново (текстиль) — прямой контакт",
            "url_tpl": "https://ivanovotextile.ru/search?q={ru}",
            "note": "Иваново = текстильная столица России. Трикотаж, постельное бельё, спецодежда. Поездка или звонок напрямую.",
            "moq": "от 1 рулона / 50 шт",
        },
    ],
    "belarus": [
        {
            "name": "Export.by",
            "url_tpl": "https://export.by/search?q={ru}",
            "note": "Официальный портал белорусских экспортёров. Производители с сертификатами и ценами.",
            "moq": "от 1 коробки",
        },
        {
            "name": "Catalog.by",
            "url_tpl": "https://catalog.by/search?q={ru}",
            "note": "Каталог белорусских предприятий. Еда, химия, текстиль, стройматериалы.",
            "moq": "от 1 партии",
        },
        {
            "name": "Deal.by (B2B раздел)",
            "url_tpl": "https://deal.by/search?q={ru}&b2b=1",
            "note": "Белорусский B2B-маркетплейс. Актуальные оптовые предложения от производителей.",
            "moq": "от 1 упаковки",
        },
        {
            "name": "Прямые производители РБ",
            "url_tpl": None,
            "note": "Ключевые кластеры: Слуцк (сахар, кондитерские), Борисов (стекло, химия), Орша (льняной текстиль), Жлобин (металл, игрушки), Брест (трикотаж, обувь).",
            "moq": "от 1 паллета",
        },
    ],
    "india": [
        {
            "name": "IndiaMART",
            "url_tpl": "https://www.indiamart.com/proddetail/{en}.html",
            "note": "Крупнейшая B2B-платформа Индии. Текстиль, специи, изделия ручной работы, натуральная косметика.",
            "moq": "от 50–500 шт",
        },
        {
            "name": "TradeIndia",
            "url_tpl": "https://www.tradeindia.com/search.html?search_term={en}",
            "note": "Альтернатива IndiaMART. Хорошо для экзотических товаров: натуральные масла, специи, этника.",
            "moq": "от 50 кг / 100 шт",
        },
        {
            "name": "Alibaba India",
            "url_tpl": "https://www.alibaba.com/trade/search?SearchText={en}&country=IN",
            "note": "Индийские поставщики на Alibaba с Trade Assurance.",
            "moq": "от 100 шт",
        },
    ],
    "bangladesh_vietnam": [
        {
            "name": "Alibaba (Bangladesh / Vietnam)",
            "url_tpl": "https://www.alibaba.com/trade/search?SearchText={en}&country=BD",
            "note": "Бангладеш = самая дешёвая базовая одежда (футболки, носки, нижнее бельё). Вьетнам — обувь, сумки, аксессуары.",
            "moq": "от 500–1000 шт",
        },
        {
            "name": "Made in Bangladesh",
            "url_tpl": "https://www.madeinbangladesh.com.bd/search?q={en}",
            "note": "Официальная B2B-площадка Бангладеш. Текстиль и одежда оптом от фабрик.",
            "moq": "от 500 шт",
        },
        {
            "name": "Vietnam Manufacturers",
            "url_tpl": "https://www.vietnamsourcing.com/search?keyword={en}",
            "note": "Вьетнамские производители обуви, кожгалантереи, мебели.",
            "moq": "от 200 шт",
        },
    ],
    "central_asia": [
        {
            "name": "Satu.kz (Казахстан)",
            "url_tpl": "https://satu.kz/c{ru}/",
            "note": "Казахстанские поставщики. Мёд, орехи, сухофрукты, хлопковые изделия. Без таможни (ЕАЭС).",
            "moq": "от 1 коробки",
        },
        {
            "name": "Uzum / Uzbek Cotton",
            "url_tpl": "https://uzum.uz/ru/search?query={ru}",
            "note": "Узбекский хлопок и текстиль — высокое качество сырья, дешевле турецкого. Без таможни (СНГ, льготный режим).",
            "moq": "от 1 рулона",
        },
        {
            "name": "Kyrgyz Textile (Кыргызстан)",
            "url_tpl": None,
            "note": "Бишкек — дешёвый пошив одежды. Часто производят под маркой «Турции» — проверяй состав. Без таможни (ЕАЭС).",
            "moq": "от 50 шт",
        },
    ],
    "parallel_import": [
        {
            "name": "Садовод / Москва (рынки)",
            "url_tpl": None,
            "note": "Параллельный импорт брендовых товаров. Для ниш электроники, косметики, аксессуаров. Нужна юридическая проверка.",
            "moq": "от 1 шт",
        },
        {
            "name": "Bringly / Wildberries Global",
            "url_tpl": None,
            "note": "Легальный параллельный импорт через WB — поставщик заводит товар как «из-за рубежа».",
            "moq": "от 1 шт",
        },
    ],
}

# ─────────────────────────────────────────────
#  КЛЮЧЕВЫЕ СЛОВА ДЛЯ ОПРЕДЕЛЕНИЯ ТИПА НИШИ
# ─────────────────────────────────────────────

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    # Еда отечественного производства (кондитерские, молочные, заготовки)
    "food": [
        "зефир", "мармелад", "конфеты", "шоколад", "пастила", "карамель", "печенье",
        "торт", "пирожное", "халва", "нуга", "драже", "леденцы", "вафли",
        "мёд", "варенье", "джем", "соусы", "маринады", "консервы",
        "творог", "сыр", "молочный", "масло сливочное", "мука", "крупа", "макароны",
        "колбаса", "мясо", "рыба копчёная", "кетчуп", "горчица",
    ],
    # Импортные продукты: чай/кофе/специи/орехи — исторически из Азии и Ближнего Востока
    "food_import": [
        "чай", "кофе", "специи", "пряности", "куркума", "имбирь", "корица",
        "кардамон", "перец молотый", "орехи", "миндаль", "кешью", "фундук",
        "сухофрукты", "финики", "изюм", "курага", "чернослив", "инжир",
        "снеки", "чипсы", "попкорн", "сухарики", "морепродукты", "рыба консервы",
        "кокосовый", "авокадо", "тофу", "мисо", "рисовая", "кунжутный",
    ],
    "clothing": [
        "платье", "блузка", "рубашка", "футболка", "майка", "юбка", "брюки", "джинсы",
        "куртка", "пальто", "шуба", "пиджак", "костюм", "комбинезон", "леггинсы",
        "спортивный костюм", "худи", "свитер", "кардиган", "джемпер", "водолазка",
        "одежда", "трикотаж", "вязаный", "льняной", "хлопковый", "одежда для",
        "нижнее бельё", "бюстгальтер", "трусы", "носки", "колготки", "термобельё",
        "купальник", "пижама", "халат", "детская одежда",
    ],
    "footwear": [
        "кроссовки", "кеды", "туфли", "сапоги", "ботинки", "мокасины", "слипоны",
        "сандалии", "шлёпанцы", "обувь", "балетки", "лоферы", "ботильоны",
    ],
    "chemicals": [
        "накипь", "чистящий", "моющий", "стиральный", "ополаскиватель", "отбеливатель",
        "средство для", "очиститель", "антисептик", "дезинфектор", "гель для",
        "мыло", "жидкость для", "спрей для", "таблетки для", "порошок для",
        "уход за", "полироль", "воск", "смазка",
    ],
    "electronics": [
        "наушники", "колонка", "bluetooth", "зарядка", "кабель", "usb", "power bank",
        "телефон", "смартфон", "планшет", "ноутбук", "умный", "wifi", "led",
        "светодиодный", "лампа умная", "робот-пылесос", "фен", "утюг", "мультиварка",
        "блендер", "миксер", "электрический", "аккумулятор", "батарейки",
    ],
    "beauty": [
        "крем", "сыворотка", "тонер", "маска для", "шампунь", "бальзам для волос",
        "масло для", "лак для ногтей", "помада", "тушь", "тени", "пудра", "тональный",
        "консилер", "хайлайтер", "духи", "парфюм", "косметика", "уход за кожей",
        "скраб", "пилинг", "мицеллярная", "мицелярная",
    ],
    "home_textiles": [
        "постельное", "подушка", "одеяло", "плед", "полотенце", "скатерть",
        "шторы", "занавески", "ковёр", "коврик", "простыня", "наволочка",
        "пододеяльник", "покрывало", "льняное", "текстиль", "ткань",
        "хлопковый материал", "лён", "трикотажное полотно",
    ],
    "bags_accessories": [
        "сумка", "рюкзак", "клатч", "кошелёк", "портмоне", "поясная", "шоппер",
        "чемодан", "дорожная", "кожаный", "искусственная кожа", "пояс", "ремень",
        "шарф", "платок", "перчатки", "шапка", "берет",
    ],
    "handmade_crafts": [
        "ручная работа", "хендмейд", "handmade", "свеча", "аромасвеча",
        "украшения ручной", "бижутерия", "кольцо", "серьги", "браслет", "подвеска",
        "вышивка", "вязаный", "плетёный", "макраме", "декупаж", "мыло ручной",
        "гипсовый", "глиняный", "деревянный декор",
    ],
    "sports": [
        "спортивный", "фитнес", "тренажёр", "коврик для йоги", "гантели", "штанга",
        "велосипед", "самокат", "скейт", "лыжи", "сноуборд", "ролики", "боксёрские",
        "протеин", "спортивное питание", "бутылка для воды спорт",
    ],
    "kids": [
        "детский", "игрушка", "кукла", "машинка", "конструктор", "пазл", "lego",
        "мягкая игрушка", "развивающий", "для малышей", "погремушка", "коляска",
        "детская кроватка", "школьный", "рюкзак школьный", "пенал",
    ],
    "jewelry": [
        "ювелирный", "серебро", "золото", "серебряный", "золотой", "кольцо золотое",
        "серьги серебряные", "цепочка", "кулон", "брошь", "браслет серебряный",
        "уход за украшениями", "полироль для серебра",
    ],
    "garden_dacha": [
        "садовый", "дача", "огород", "рассада", "семена", "удобрение", "горшок",
        "лейка", "секатор", "грабли", "шланг", "теплица", "парник", "компост",
    ],
    "auto": [
        "автомобильный", "автомобиля", "авто", "шины", "диски", "масло моторное",
        "колесо", "ароматизатор авто", "видеорегистратор", "навигатор",
    ],
    "furniture_decor": [
        "стул", "кресло", "стол", "шкаф", "тумба", "полка", "декор", "ваза",
        "рамка", "картина", "зеркало", "светильник", "люстра", "торшер",
    ],
}

# ─────────────────────────────────────────────
#  МАТРИЦА: тип товара + ценовой тир → страна
#  Значение 0–10: насколько вероятен этот источник
# ─────────────────────────────────────────────

SOURCING_MATRIX: dict[str, dict[str, dict[str, float]]] = {
    # тип товара → { ценовой тир → { страна → балл } }
    "food": {
        "economy": {
            "russia":      9.0,
            "belarus":     8.5,
            "central_asia": 6.0,
            "china":       2.0,
        },
        "mid": {
            "russia":      9.0,
            "belarus":     9.0,
            "central_asia": 5.0,
            "china":       1.5,
        },
        "premium": {
            "russia":      8.0,
            "belarus":     8.5,
            "india":       4.0,
            "china":       1.0,
        },
    },
    # Чай, кофе, специи, орехи, сухофрукты — импортные продукты
    "food_import": {
        "economy": {
            "china":        8.5,
            "india":        7.5,
            "central_asia": 7.0,
            "russia":       3.0,   # только переупаковка
        },
        "mid": {
            "china":        8.0,
            "india":        8.0,
            "central_asia": 6.5,
            "russia":       3.5,
        },
        "premium": {
            "india":        8.5,
            "china":        7.0,
            "central_asia": 6.0,
            "russia":       4.0,
        },
    },
    "clothing": {
        "economy": {
            "china":              9.0,
            "bangladesh_vietnam": 7.0,
            "central_asia":       6.5,  # Кыргызстан
            "turkey":             5.0,
            "russia":             4.0,
        },
        "mid": {
            "turkey":       9.0,
            "china":        7.0,
            "russia":       7.0,
            "central_asia": 5.0,  # Узбекистан (хлопок)
            "india":        4.0,
        },
        "premium": {
            "turkey":   8.5,
            "russia":   8.0,
            "india":    5.0,  # натуральные ткани
            "china":    5.0,  # есть качественные фабрики
            "belarus":  4.5,  # трикотаж
        },
    },
    "footwear": {
        "economy": {
            "china":              9.5,
            "bangladesh_vietnam": 7.0,
            "central_asia":       5.0,
        },
        "mid": {
            "china":   8.0,
            "turkey":  7.5,
            "russia":  5.0,
        },
        "premium": {
            "turkey": 8.5,
            "russia": 7.0,
            "china":  6.0,
        },
    },
    "chemicals": {
        "economy": {
            "russia":  9.0,
            "belarus": 9.0,
            "china":   5.0,
        },
        "mid": {
            "russia":  9.5,
            "belarus": 9.0,
            "china":   4.0,
        },
        "premium": {
            "russia":  8.5,
            "belarus": 8.0,
            "china":   3.0,
        },
    },
    "electronics": {
        "economy": {
            "china": 9.8,
        },
        "mid": {
            "china":          9.5,
            "parallel_import": 4.0,
        },
        "premium": {
            "parallel_import": 8.0,
            "china":           6.0,
        },
    },
    "beauty": {
        "economy": {
            "china":   8.0,
            "russia":  7.0,
            "belarus": 6.0,
        },
        "mid": {
            "russia":  8.5,
            "belarus": 7.0,
            "china":   6.0,
            "india":   5.0,  # натуральная аюрведа
        },
        "premium": {
            "russia":          7.5,
            "parallel_import": 7.0,
            "india":           6.0,
            "china":           5.0,
        },
    },
    "home_textiles": {
        "economy": {
            "china":   9.0,
            "russia":  6.0,  # Иваново
            "central_asia": 5.0,
        },
        "mid": {
            "russia":  8.5,  # Иваново, Ярославль
            "china":   7.0,
            "turkey":  6.0,
            "belarus": 6.0,  # лён, трикотаж
            "india":   5.0,
        },
        "premium": {
            "russia":  8.0,
            "turkey":  7.5,
            "india":   6.5,  # натуральный лён, хлопок
            "belarus": 7.0,
            "china":   5.0,
        },
    },
    "bags_accessories": {
        "economy": {
            "china":   9.5,
        },
        "mid": {
            "china":   8.0,
            "turkey":  7.5,
            "russia":  5.0,
        },
        "premium": {
            "turkey": 8.5,
            "russia": 7.0,
            "china":  5.5,
            "india":  5.0,  # кожа ручной работы
        },
    },
    "handmade_crafts": {
        "economy": {
            "russia": 8.0,
            "china":  6.0,
        },
        "mid": {
            "russia": 9.0,
            "india":  6.0,
            "china":  4.0,
        },
        "premium": {
            "russia": 9.5,
            "india":  7.0,
            "china":  2.0,
        },
    },
    "sports": {
        "economy": {
            "china": 9.5,
        },
        "mid": {
            "china":  8.5,
            "russia": 5.0,
        },
        "premium": {
            "china":          7.0,
            "parallel_import": 6.0,
            "russia":          5.0,
        },
    },
    "kids": {
        "economy": {
            "china":   8.0,
            "russia":  7.0,  # строгая сертификация → отечественное надёжнее
        },
        "mid": {
            "russia":  8.5,
            "china":   7.0,
            "belarus": 6.0,
        },
        "premium": {
            "russia":          8.5,
            "parallel_import": 6.0,
            "china":           5.0,
        },
    },
    "jewelry": {
        "economy": {
            "china": 9.0,
        },
        "mid": {
            "china":  7.0,
            "russia": 6.0,
            "india":  5.0,
        },
        "premium": {
            "russia": 8.5,
            "india":  6.0,
            "china":  4.0,
        },
    },
    "garden_dacha": {
        "economy": {
            "china":  8.5,
            "russia": 6.0,
        },
        "mid": {
            "china":  7.5,
            "russia": 7.0,
        },
        "premium": {
            "russia": 7.5,
            "china":  6.0,
        },
    },
    "auto": {
        "economy": {
            "china": 9.5,
        },
        "mid": {
            "china":  8.5,
            "russia": 5.0,
        },
        "premium": {
            "china":          7.0,
            "parallel_import": 6.0,
        },
    },
    "furniture_decor": {
        "economy": {
            "china": 9.0,
        },
        "mid": {
            "china":  8.0,
            "russia": 6.0,
            "india":  4.0,
        },
        "premium": {
            "russia": 7.5,
            "china":  6.5,
            "india":  5.0,
        },
    },
    # Дефолт для неизвестных категорий
    "_default": {
        "economy": {"china": 8.0, "russia": 5.0},
        "mid":     {"china": 7.0, "russia": 6.0, "turkey": 4.0},
        "premium": {"china": 6.0, "russia": 7.0, "turkey": 5.0},
    },
}

# ─────────────────────────────────────────────
#  ТАМОЖНЯ И ЛОГИСТИКА (для unit economics)
# ─────────────────────────────────────────────

# Если в названии ниши прямо указана страна — увеличиваем её приоритет
COUNTRY_NAME_SIGNALS: dict[str, str] = {
    "китайский": "china", "китайская": "china", "китайские": "china", "china": "china",
    "турецкий": "turkey", "турецкая": "turkey", "турецкие": "turkey", "turkey": "turkey",
    "белорусский": "belarus", "белорусская": "belarus", "белорусские": "belarus",
    "беларусь": "belarus", "белоруссия": "belarus",
    "индийский": "india", "индийская": "india", "india": "india",
    "итальянский": "parallel_import", "французский": "parallel_import",
    "японский": "parallel_import", "корейский": "parallel_import",
    "немецкий": "parallel_import", "испанский": "parallel_import",
    "швейцарский": "parallel_import", "австрийский": "parallel_import",
    "американский": "parallel_import", "финский": "parallel_import",
    "узбекский": "central_asia", "казахский": "central_asia",
    "вьетнамский": "bangladesh_vietnam", "бангладешский": "bangladesh_vietnam",
    "российский": "russia", "русский": "russia",
}

LOGISTICS_PARAMS: dict[str, dict] = {
    "china": {
        "customs_pct":    0.10,   # 10% от стоимости (усреднённо с НДС и пошлинами)
        "logistics_rub":  350,    # ₽/кг (авиа ~700, море ~150)
        "lead_time_days": "35–65 дней (море) / 15–25 дней (авиа)",
        "min_order_rub":  15_000,
        "risks": "Качество нужно контролировать (QC агент). Задержки таможни.",
        "certifications": "Декларация соответствия ТР ТС (для большинства категорий).",
    },
    "turkey": {
        "customs_pct":    0.08,
        "logistics_rub":  280,
        "lead_time_days": "10–25 дней",
        "min_order_rub":  20_000,
        "risks": "Нужен агент в Стамбуле или поездка. Сезонность коллекций.",
        "certifications": "Декларация соответствия ТР ТС. Для одежды — маркировка «Честный знак».",
    },
    "russia": {
        "customs_pct":    0.0,
        "logistics_rub":  80,
        "lead_time_days": "3–14 дней",
        "min_order_rub":  5_000,
        "risks": "Цена выше чем в Китае, но нет таможни и рисков задержки.",
        "certifications": "ГОСТ/ТР ТС (обычно уже есть у производителя). Еда — ветсертификаты.",
    },
    "belarus": {
        "customs_pct":    0.0,   # ЕАЭС
        "logistics_rub":  60,
        "lead_time_days": "3–10 дней",
        "min_order_rub":  5_000,
        "risks": "Меньший выбор производителей. Переговоры часто на белорусском.",
        "certifications": "ТР ТС (общий с РФ). Маркировка «Честный знак» для подходящих категорий.",
    },
    "india": {
        "customs_pct":    0.12,
        "logistics_rub":  420,
        "lead_time_days": "30–50 дней",
        "min_order_rub":  25_000,
        "risks": "Коммуникация на английском. Переменное качество, нужна выборочная проверка.",
        "certifications": "Декларация соответствия ТР ТС.",
    },
    "bangladesh_vietnam": {
        "customs_pct":    0.10,
        "logistics_rub":  300,
        "lead_time_days": "30–60 дней",
        "min_order_rub":  30_000,
        "risks": "Высокий MOQ. Нужен агент или поездка. Языковой барьер.",
        "certifications": "ТР ТС + Честный знак для одежды.",
    },
    "central_asia": {
        "customs_pct":    0.0,   # ЕАЭС (Казахстан, Кыргызстан, Армения)
        "logistics_rub":  120,
        "lead_time_days": "7–20 дней",
        "min_order_rub":  8_000,
        "risks": "Качество текстиля нужно проверять. Кыргызстан — осторожно с маркировкой.",
        "certifications": "ТР ТС. Для Узбекистана (не в ЕАЭС) — таможня.",
    },
    "parallel_import": {
        "customs_pct":    0.0,
        "logistics_rub":  0,
        "lead_time_days": "1–7 дней (закупка в РФ)",
        "min_order_rub":  10_000,
        "risks": "Юридические риски. Нет гарантийного обслуживания от бренда. Цена выше.",
        "certifications": "Нужна осторожность: не все параллельно импортированные товары законны.",
    },
}

# ─────────────────────────────────────────────
#  СТРАНОВЫЕ НАЗВАНИЯ
# ─────────────────────────────────────────────

COUNTRY_LABELS: dict[str, str] = {
    "china":              "🇨🇳 Китай",
    "turkey":             "🇹🇷 Турция",
    "russia":             "🇷🇺 Россия (внутреннее)",
    "belarus":            "🇧🇾 Беларусь",
    "india":              "🇮🇳 Индия",
    "bangladesh_vietnam": "🇧🇩 Бангладеш / Вьетнам",
    "central_asia":       "🌏 Центральная Азия (ЕАЭС)",
    "parallel_import":    "📦 Параллельный импорт",
}


# ─────────────────────────────────────────────
#  ОСНОВНОЙ КЛАСС
# ─────────────────────────────────────────────

@dataclass
class SourcingSignals:
    category:     str   # одна из ключей SOURCING_MATRIX
    price_tier:   str   # "economy" | "mid" | "premium"
    avg_price:    float
    is_food:      bool
    needs_cert:   bool  # нужна ли строгая сертификация
    is_handmade:  bool


@dataclass
class SourcingOption:
    country:      str
    label:        str
    score:        float
    platforms:    list[dict]
    logistics:    dict
    rank:         int = 0


@dataclass
class SourcingReport:
    niche_name:   str
    signals:      SourcingSignals
    options:      list[SourcingOption]   # отсортированы по score
    search_terms: dict                   # {lang: term}


def analyze(
    niche_name: str,
    avg_price:  float,
    *,
    revenue:        float = 0,
    sellers:        int   = 0,
    extra_context:  str   = "",
) -> SourcingReport:
    """Основная точка входа. Возвращает полный SourcingReport."""

    signals  = _detect_signals(niche_name, avg_price)
    scores   = _score_countries(signals, niche_name)
    options  = _build_options(scores, top_n=4)
    terms    = _search_terms(niche_name)

    for i, opt in enumerate(options, 1):
        opt.rank = i

    return SourcingReport(
        niche_name=niche_name,
        signals=signals,
        options=options,
        search_terms=terms,
    )


# ─────────────────────────────────────────────
#  ВНУТРЕННИЕ ФУНКЦИИ
# ─────────────────────────────────────────────

def _stem_match(keyword: str, text: str) -> bool:
    """Совпадение с учётом русской морфологии через проверку по словам.
    Keyword совпадает только если является началом отдельного слова в тексте,
    что исключает ложные срабатывания типа 'чай' в 'чайника'."""
    # Короткие ключи (< 5 символов) — только точное совпадение со словом
    # Длинные — допускаем что слово в тексте = keyword + падежное окончание
    STEM_LEN = max(5, len(keyword) - 2)
    stem = keyword[:STEM_LEN]
    words = re.split(r'[\s\-/,]+', text)
    for word in words:
        if len(keyword) < 5:
            # Короткие слова: только точное совпадение
            if word == keyword:
                return True
        else:
            # Длинные: слово начинается со стема
            if word.startswith(stem):
                return True
    return False


def _detect_signals(niche_name: str, avg_price: float) -> SourcingSignals:
    name_lower = niche_name.lower()

    # Определяем категорию (с учётом морфологии через стем-совпадение)
    category = "_default"
    best_match_score = 0
    for cat, keywords in CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if _stem_match(kw, name_lower))
        if score > best_match_score:
            best_match_score = score
            category = cat

    # Ценовой тир (для WB-ниш)
    if avg_price < 500:
        price_tier = "economy"
    elif avg_price < 2000:
        price_tier = "mid"
    else:
        price_tier = "premium"

    is_food    = category in ("food", "food_import")
    is_handmade = category == "handmade_crafts"

    # Категории с повышенными требованиями к сертификации
    strict_cert_cats = {"food", "food_import", "kids", "beauty", "chemicals"}
    needs_cert = category in strict_cert_cats

    return SourcingSignals(
        category=category,
        price_tier=price_tier,
        avg_price=avg_price,
        is_food=is_food,
        needs_cert=needs_cert,
        is_handmade=is_handmade,
    )


def _score_countries(signals: SourcingSignals, niche_name: str = "") -> dict[str, float]:
    matrix = SOURCING_MATRIX.get(signals.category, SOURCING_MATRIX["_default"])
    tier_scores = matrix.get(signals.price_tier, matrix.get("mid", {}))

    scores: dict[str, float] = dict(tier_scores)

    # Дополнительные корректировки по типу товара
    if signals.is_food and signals.category == "food":
        scores["china"] = scores.get("china", 0) * 0.3
        scores["russia"]  = min(10.0, scores.get("russia", 0) * 1.1)
        scores["belarus"] = min(10.0, scores.get("belarus", 0) * 1.1)

    if signals.is_handmade:
        scores["russia"] = min(10.0, scores.get("russia", 0) * 1.2)
        scores["china"]  = scores.get("china", 0) * 0.5

    if signals.price_tier == "economy":
        scores["china"] = min(10.0, scores.get("china", 0) * 1.1)

    # Сигналы страны в названии ниши — сильный буст (стем-матчинг для всех форм слова)
    if niche_name:
        words = re.split(r'[\s\-/,]+', niche_name.lower())
        for word in words:
            for signal_word, country in COUNTRY_NAME_SIGNALS.items():
                stem_len = max(5, len(signal_word) - 2)
                stem = signal_word[:stem_len]
                if word.startswith(stem) or word == signal_word:
                    current = scores.get(country, 3.0)
                    scores[country] = min(10.0, current * 1.4 + 2.0)
                    break

    return {k: v for k, v in scores.items() if v >= 1.0}


def _build_options(scores: dict[str, float], top_n: int = 4) -> list[SourcingOption]:
    ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_n]
    options = []
    for country, score in ranked:
        options.append(SourcingOption(
            country=country,
            label=COUNTRY_LABELS.get(country, country),
            score=round(score, 1),
            platforms=PLATFORMS.get(country, [])[:3],  # топ-3 платформы
            logistics=LOGISTICS_PARAMS.get(country, {}),
        ))
    return options


def _search_terms(niche_name: str) -> dict[str, str]:
    """Возвращает поисковые запросы на разных языках."""
    clean = re.sub(r'\s+', ' ', niche_name.strip())
    return {
        "ru": clean,
        "en": clean,   # AI заменит на правильный перевод
        "cn": clean,   # AI заменит на китайский
        "tr": clean,   # AI заменит на турецкий
    }


# ─────────────────────────────────────────────
#  ГЕНЕРАЦИЯ ТЕКСТОВОГО ПРОМПТА ДЛЯ CLAUDE
# ─────────────────────────────────────────────

def build_sourcing_prompt(report: SourcingReport, niche_data: dict) -> str:
    """Строит промпт для Claude — он сгенерирует конкретные рекомендации
    с переводами запросов, конкретными поставщиками и советами."""

    lp = LOGISTICS_PARAMS

    options_text = ""
    for opt in report.options:
        log = opt.logistics
        options_text += (
            f"\n  #{opt.rank}. {opt.label} (балл {opt.score}/10)\n"
            f"    Таможня: {log.get('customs_pct', 0)*100:.0f}% | "
            f"Логистика: ~{log.get('logistics_rub', 0)} ₽/кг | "
            f"Срок: {log.get('lead_time_days', '?')} | "
            f"Мин. партия от: {log.get('min_order_rub', 0):,.0f} ₽\n"
            f"    Риски: {log.get('risks', '—')}\n"
            f"    Сертификация: {log.get('certifications', '—')}\n"
            f"    Площадки: {', '.join(p['name'] for p in opt.platforms)}\n"
        )

    avg_price = report.signals.avg_price
    category  = report.signals.category
    tier      = report.signals.price_tier

    prompt = (
        f"Ты эксперт по международным закупкам для маркетплейса Wildberries (РФ/РБ).\n"
        f"НИША: {report.niche_name}\n"
        f"Средняя цена в нише: {avg_price:,.0f} ₽ | Тип товара: {category} | Ценовой тир: {tier}\n"
        f"\nСистема определила следующие оптимальные источники закупки:\n{options_text}\n"
        f"\nЗАДАЧА: Напиши конкретные рекомендации по закупке ТОЛЬКО в формате JSON:\n"
        '{\n'
        '  "summary": "2-3 предложения: откуда лучше всего закупать эту нишу и почему",\n'
        '  "top_country": "название страны #1",\n'
        '  "search_queries": {\n'
        '    "ru": "запрос на русском для поиска у российских поставщиков",\n'
        '    "en": "search query in English for Alibaba/international platforms",\n'
        '    "cn": "搜索词 (поисковый запрос на китайском для 1688.com)",\n'
        '    "tr": "Türkçe arama terimi (для турецких платформ, если релевантно)"\n'
        '  },\n'
        '  "supplier_tips": [\n'
        '    "Конкретный совет 1 по поиску поставщиков для этой ниши",\n'
        '    "Конкретный совет 2",\n'
        '    "Конкретный совет 3"\n'
        '  ],\n'
        '  "red_flags": ["Что проверить при выборе поставщика", "Типичный развод в этой нише"],\n'
        '  "first_order_rub": 0,\n'
        '  "certification_note": "Какие документы нужны для продажи на WB"\n'
        '}\n'
        'ONLY JSON, без пояснений.'
    )
    return prompt


# ─────────────────────────────────────────────
#  ГЕНЕРАЦИЯ URL С ПОДСТАВКОЙ ЗАПРОСА
# ─────────────────────────────────────────────

def platform_url(platform: dict, search_terms: dict) -> Optional[str]:
    """Подставляет поисковый запрос в URL шаблон платформы."""
    tpl = platform.get("url_tpl")
    if not tpl:
        return None
    try:
        from urllib.parse import quote
        url = tpl
        if "{cn}" in url:
            url = url.replace("{cn}", quote(search_terms.get("cn", search_terms["ru"])))
        if "{en}" in url:
            url = url.replace("{en}", quote(search_terms.get("en", search_terms["ru"])))
        if "{ru}" in url:
            url = url.replace("{ru}", quote(search_terms["ru"]))
        if "{tr}" in url:
            url = url.replace("{tr}", quote(search_terms.get("tr", search_terms["ru"])))
        return url
    except Exception:
        return None


# ─────────────────────────────────────────────
#  БЫСТРЫЙ ТЕСТ
# ─────────────────────────────────────────────

if __name__ == "__main__":
    test_cases = [
        ("Зефир белорусский",        450.0),
        ("Платье женское летнее",    1200.0),
        ("Средство от накипи",        380.0),
        ("Кроссовки мужские",        2500.0),
        ("Наушники беспроводные",    1800.0),
        ("Постельное бельё льняное", 3200.0),
        ("Детские игрушки набор",    1500.0),
        ("Свеча ароматическая",       650.0),
        ("Сумка кожаная женская",    4500.0),
        ("Крем для лица антивозрастной", 800.0),
    ]

    for name, price in test_cases:
        r = analyze(name, price)
        top = [f"{o.label} ({o.score})" for o in r.options[:3]]
        print(f"\n{'─'*60}")
        print(f"  {name} | {price:.0f}₽ | [{r.signals.category} / {r.signals.price_tier}]")
        print(f"  ТОП-3: {' → '.join(top)}")
