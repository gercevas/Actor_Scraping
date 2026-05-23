from fastapi import FastAPI, Query
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import re
import asyncio
from typing import Optional, List, Dict, Any

app = FastAPI(title="Casa del Libro API")

_pw = None
_browser = None
_context = None
sem = asyncio.Semaphore(1)


def normalize_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(\d{1,4}(?:[.,]\d{2})?)", text)
    if not m:
        return None
    return m.group(1).replace(",", ".")


def clean_isbn(isbn: str) -> str:
    return (isbn or "").strip().replace(" ", "").replace("-", "")


class BatchRequest(BaseModel):
    isbns: List[str] = Field(..., min_length=1, max_length=100)
    pause_ms: int = Field(default=2000, ge=0, le=10000)


async def create_context():
    global _browser
    return await _browser.new_context(
        locale="es-ES",
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )


async def get_context():
    global _context
    if _context is None:
        _context = await create_context()
    return _context


@app.on_event("startup")
async def startup():
    global _pw, _browser, _context
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )
    _context = await create_context()


@app.on_event("shutdown")
async def shutdown():
    global _pw, _browser, _context
    try:
        if _context:
            await _context.close()
        if _browser:
            await _browser.close()
        if _pw:
            await _pw.stop()
    except Exception:
        pass


@app.get("/health")
async def health():
    return {"status": "ok"}


async def accept_cookies(page) -> None:
    patterns = [
        re.compile(r"(aceptar|acepto|agree|accept)", re.I),
    ]

    for _ in range(3):
        for pattern in patterns:
            try:
                btn = page.get_by_role("button", name=pattern).first
                if await btn.is_visible(timeout=1500):
                    await btn.click(timeout=3000)
                    await page.wait_for_timeout(700)
                    return
            except Exception:
                pass

        for sel in [
            "#onetrust-accept-btn-handler",
            'button:has-text("Aceptar")',
            'button:has-text("Aceptar cookies")',
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=3000)
                    await page.wait_for_timeout(700)
                    return
            except Exception:
                pass


async def get_first_result_card(page):
    candidate_selectors = [
        '[data-test="search-result-item"]',
        '[data-testid="search-result-item"]',
        '.product-grid-item',
        '.product-item',
        '.search-result-item',
        'article',
    ]

    for sel in candidate_selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass

    return None


async def scrape_casadellibro_isbn(page, isbn: str) -> Dict[str, Any]:
    isbn = clean_isbn(isbn)
    url = f"https://www.casadellibro.com/libros?query={isbn}"

    if not isbn.isdigit() or not (10 <= len(isbn) <= 13):
        return {
            "isbn": isbn,
            "title": None,
            "price_current_eur": None,
            "price_previous_eur": None,
            "url": url,
            "error": "ISBN inválido",
        }

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2500)

        await accept_cookies(page)
        await page.wait_for_timeout(1500)

        try:
            await page.wait_for_selector("body", timeout=5000)
        except PlaywrightTimeoutError:
            pass

        body_text = ""
        try:
            body_text = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            pass

        if "No se han encontrado resultados" in body_text or "No se encontraron resultados" in body_text:
            return {
                "isbn": isbn,
                "title": None,
                "price_current_eur": None,
                "price_previous_eur": None,
                "url": page.url,
                "error": "No se encontraron resultados",
            }

        card = None
        for _ in range(5):
            card = await get_first_result_card(page)
            if card is not None:
                break
            await page.wait_for_timeout(1000)

        scope = card if card is not None else page

        title = None
        title_selectors = [
            '[data-test="result-title"]',
            '[data-testid="result-title"]',
            'h2',
            'h3',
            '.title',
            '.product-title',
            'a[title]',
        ]

        for sel in title_selectors:
            try:
                loc = scope.locator(sel).first
                if await loc.count() > 0:
                    txt = (await loc.inner_text(timeout=2000)).strip()
                    if txt:
                        title = txt
                        break
                attr = await loc.get_attribute("title")
                if attr and attr.strip():
                    title = attr.strip()
                    break
            except Exception:
                pass

        current_price = None
        previous_price = None

        current_price_selectors = [
            '[data-test="result-current-price"] span.x-currency',
            '[data-testid="result-current-price"] span.x-currency',
            '[data-test="result-current-price"]',
            '.price-current',
            '.price',
            'span.x-currency',
        ]

        previous_price_selectors = [
            '[data-test="result-previous-price"] span.x-currency',
            '[data-testid="result-previous-price"] span.x-currency',
            '[data-test="result-previous-price"]',
            '.price-old',
            '.old-price',
            '.price-previous',
        ]

        for sel in current_price_selectors:
            try:
                texts = await scope.locator(sel).all_inner_texts()
                values = [normalize_price(t) for t in texts if normalize_price(t)]
                if values:
                    current_price = values[0]
                    break
            except Exception:
                pass

        for sel in previous_price_selectors:
            try:
                texts = await scope.locator(sel).all_inner_texts()
                values = [normalize_price(t) for t in texts if normalize_price(t)]
                if values:
                    previous_price = values[0]
                    break
            except Exception:
                pass

        if current_price is None:
            try:
                all_prices = await scope.locator("span.x-currency").all_inner_texts()
                values = [normalize_price(t) for t in all_prices if normalize_price(t)]
                seen = set()
                dedup = []
                for v in values:
                    if v not in seen:
                        seen.add(v)
                        dedup.append(v)

                if len(dedup) >= 1:
                    current_price = dedup[0]
                if len(dedup) >= 2:
                    previous_price = dedup[1]
            except Exception:
                pass

        return {
            "isbn": isbn,
            "title": title,
            "price_current_eur": current_price,
            "price_previous_eur": previous_price,
            "url": page.url,
            "error": None,
        }

    except PlaywrightTimeoutError:
        return {
            "isbn": isbn,
            "title": None,
            "price_current_eur": None,
            "price_previous_eur": None,
            "url": url,
            "error": f"Timeout al procesar ISBN {isbn}",
        }
    except Exception as e:
        return {
            "isbn": isbn,
            "title": None,
            "price_current_eur": None,
            "price_previous_eur": None,
            "url": url,
            "error": str(e),
        }


@app.get("/casadellibro")
async def casadellibro(isbn: str = Query(..., min_length=10, max_length=13)):
    ctx = await get_context()
    page = await ctx.new_page()
    try:
        return await scrape_casadellibro_isbn(page, isbn)
    finally:
        await page.close()


BATCH_CONCURRENCY = 3


@app.post("/casadellibro/batch")
async def casadellibro_batch(req: BatchRequest):
    ctx = await get_context()
    isbns = [clean_isbn(x) for x in req.isbns if clean_isbn(x)]
    concurrency_sem = asyncio.Semaphore(BATCH_CONCURRENCY)

    async def scrape_one(isbn: str) -> Dict[str, Any]:
        async with concurrency_sem:
            page = await ctx.new_page()
            try:
                return await scrape_casadellibro_isbn(page, isbn)
            finally:
                await page.close()

    results = await asyncio.gather(*[scrape_one(isbn) for isbn in isbns])

    success_count = sum(1 for r in results if not r.get("error"))
    error_count = len(results) - success_count

    return {
        "source": "casadellibro",
        "count": len(results),
        "success_count": success_count,
        "error_count": error_count,
        "results": list(results),
    }