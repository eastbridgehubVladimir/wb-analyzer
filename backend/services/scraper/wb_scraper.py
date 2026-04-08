"""
Парсер Wildberries на Playwright.
Собирает данные о товаре (цена, рейтинг, позиция) со страниц WB.
"""
import asyncio
import logging
from dataclasses import dataclass

from playwright.async_api import async_playwright
from tenacity import retry, stop_after_attempt, wait_exponential

from services.scraper.proxy_rotator import proxy_rotator

logger = logging.getLogger(__name__)


@dataclass
class ScrapedProduct:
    wb_sku: int
    name: str
    brand: str
    price: float
    price_with_card: float | None
    old_price: float | None
    rating: float
    reviews_count: int
    images: list[str]
    attributes: dict


class WBScraper:
    WB_PRODUCT_URL = "https://www.wildberries.ru/catalog/{sku}/detail.aspx"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def scrape_product(self, wb_sku: int) -> ScrapedProduct | None:
        proxy = proxy_rotator.get()
        proxy_config = {"server": proxy} if proxy else None

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    proxy=proxy_config,
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 800},
                )
                page = await context.new_page()
                url = self.WB_PRODUCT_URL.format(sku=wb_sku)

                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_selector(".product-page", timeout=15_000)

                # Извлечение данных через JavaScript
                data = await page.evaluate("""() => {
                    const get = (sel, attr) => {
                        const el = document.querySelector(sel);
                        return el ? (attr ? el.getAttribute(attr) : el.textContent.trim()) : null;
                    };
                    return {
                        name:      get('.product-page__title'),
                        brand:     get('.product-page__brand-name'),
                        price:     get('.price-block__final-price'),
                        oldPrice:  get('.price-block__old-price'),
                        rating:    get('.product-review__rating .address-rate-mini'),
                        reviews:   get('.product-review__count-review'),
                        images:    Array.from(document.querySelectorAll('.photo-zoom__preview img'))
                                        .map(img => img.src).slice(0, 10),
                    };
                }""")

                def parse_price(raw: str | None) -> float | None:
                    if not raw:
                        return None
                    cleaned = raw.replace("\xa0", "").replace(" ", "").replace("₽", "").replace(",", ".")
                    try:
                        return float(cleaned)
                    except ValueError:
                        return None

                return ScrapedProduct(
                    wb_sku=wb_sku,
                    name=data.get("name") or "",
                    brand=data.get("brand") or "",
                    price=parse_price(data.get("price")) or 0.0,
                    price_with_card=None,
                    old_price=parse_price(data.get("oldPrice")),
                    rating=float(data.get("rating") or 0),
                    reviews_count=int((data.get("reviews") or "0").replace(" ", "") or 0),
                    images=data.get("images") or [],
                    attributes={},
                )
            except Exception as exc:
                logger.warning("Ошибка парсинга SKU %s (прокси: %s): %s", wb_sku, proxy, exc)
                if proxy:
                    proxy_rotator.mark_failed(proxy)
                raise
            finally:
                await browser.close()

    async def scrape_search(self, keyword: str, pages: int = 3) -> list[int]:
        """Вернуть список SKU из результатов поиска по ключевому слову."""
        skus: list[int] = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await (await browser.new_context()).new_page()
            for page_num in range(1, pages + 1):
                url = f"https://www.wildberries.ru/catalog/0/search.aspx?search={keyword}&page={page_num}"
                await page.goto(url, wait_until="domcontentloaded")
                await asyncio.sleep(1.5)
                page_skus = await page.evaluate("""() =>
                    Array.from(document.querySelectorAll('[data-nm-id]'))
                         .map(el => parseInt(el.dataset.nmId))
                         .filter(Boolean)
                """)
                skus.extend(page_skus)
            await browser.close()
        return list(set(skus))


wb_scraper = WBScraper()
