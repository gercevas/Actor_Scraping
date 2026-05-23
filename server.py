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
_browser_ready = asyncio.Event()

BATCH_CONCURRENCY = 3
sem = asyncio.Semaphore(BATCH_CONCURRENCY)

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['es-ES', 'es', 'en']});
window.chrome = {runtime: {}};
"""


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
    pause_ms: int = Field(default=1000, ge=0, le=10000)


async def _create_stealth_context():
    global _browser
    ctx = await _browser.new_context(
        locale="es-ES",
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        extra_http_headers={"Accept-Language": "es-ES,es;q=0.9,en;q=0.8"},
    )
    await ctx.add_init_script(STEALTH_SCRIPT)
    return ctx


async def _launch_browser():
    global _pw, _browser, _context
    try:
        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        _context = await _create_stealth_context()
        _browser_ready.set()
    except Exception:
        _browser_ready.set()


@app.on_event("startup")
async def startup():
    asyncio.create_task(_launch_browser())


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
    return {"status": "ok", "browser_ready": _browser_ready.is_set()}


async def get_context():
    global _pw, _browser, _context
    await asyncio.wait_for(_browser_ready.wait(), timeout=30)
    if _browser is None or not _browser.is_connected():
        _browser_ready.clear()
        await _launch_browser()
        await asyncio.wait_for(_browser_ready.wait(), timeout=30)
    if _context is None:
        _context = await _create_stealth_context()
    return _context


async def accept_cookies(page) -> None:
    for sel in [
        "#onetrust-accept-btn-handler",
        'button:has-text("Aceptar cookies")',
        'button:has-text("Aceptar")',
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=1000):
                await loc.click(timeout=2000)
                await page.wait_for_timeout(300)
                return
        except Exception:
            pass


async def get_first_result_card(page):
    for sel in [
        '[data-test="search-result-item"]',
        '[data-testid="search-result-item"]',
        '.product-grid-item',
        '.product-item',
        '.search-result-item',
        'article',
    ]:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                return loc.first
        except Exception:
            pass
    return None


async def scrape_casadellibro_isbn(isbn: str) -> Dict[str, Any]:
    global _context
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

    async with sem:
        try:
            ctx = await get_context()
            page = await ctx.new_page()
        except Exception as e:
            _context = None
            return {
                "isbn": isbn,
                "title": None,
                "price_current_eur": None,
                "price_previous_eur": None,
                "url": url,
                "error": f"Error iniciando browser: {e}",
            }

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            await accept_cookies(page)

            # Sale en cuanto aparecen resultados — igual que image_app.py
            try:
                await page.wait_for_selector(
                    'span.x-currency, [data-test="search-result-item"], '
                    '[data-testid="search-result-item"], article',
                    timeout=15000,
                )
            except PlaywrightTimeoutError:
                await page.wait_for_timeout(2000)

            no_results = False
            for txt in ["No se han encontrado resultados", "No se encontraron resultados"]:
                try:
                    if await page.get_by_text(txt).count() > 0:
                        no_results = True
                        break
                except Exception:
                    pass

            if no_results:
                return {
                    "isbn": isbn,
                    "title": "-",
                    "price_current_eur": "-",
                    "price_previous_eur": "-",
                    "url": page.url,
                    "error": "No se encontraron resultados",
                }

            card = await get_first_result_card(page)
            if card is None:
                await page.wait_for_timeout(2000)
                card = await get_first_result_card(page)

            scope = card if card is not None else page

            title = None
            for sel in ['[data-test="result-title"]', '[data-testid="result-title"]', 'h2', 'h3', '.title', 'a[title]']:
                try:
                    loc = scope.locator(sel).first
                    if await loc.count() > 0:
                        txt = (await loc.inner_text(timeout=2000)).strip()
                        if txt:
                            title = txt
                            break
                except Exception:
                    pass

            current_price = None
            previous_price = None

            for sel in [
                '[data-test="result-current-price"] span.x-currency',
                '[data-testid="result-current-price"] span.x-currency',
                '[data-test="result-current-price"]',
                '.price-current',
                'span.x-currency',
            ]:
                try:
                    texts = await scope.locator(sel).all_inner_texts()
                    values = [normalize_price(t) for t in texts if normalize_price(t)]
                    if values:
                        current_price = values[0]
                        break
                except Exception:
                    pass

            for sel in [
                '[data-test="result-previous-price"] span.x-currency',
                '[data-testid="result-previous-price"] span.x-currency',
                '[data-test="result-previous-price"]',
                '.price-old',
                '.old-price',
            ]:
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
                    seen: set = set()
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
        finally:
            await page.close()


@app.get("/casadellibro")
async def casadellibro(
    isbn: str = Query(..., min_length=10, max_length=13),
    row_number: Optional[int] = Query(default=None),
):
    result = await scrape_casadellibro_isbn(isbn)
    if row_number is not None:
        result["row_number"] = row_number
    return result


@app.post("/casadellibro/batch")
async def casadellibro_batch(req: BatchRequest):
    isbns = [clean_isbn(x) for x in req.isbns if clean_isbn(x)]
    if not isbns:
        return {"source": "casadellibro", "count": 0, "success_count": 0, "error_count": 0, "results": []}

    results = await asyncio.gather(*[scrape_casadellibro_isbn(isbn) for isbn in isbns])

    success_count = sum(1 for r in results if not r.get("error"))
    error_count = len(results) - success_count

    return {
        "source": "casadellibro",
        "count": len(results),
        "success_count": success_count,
        "error_count": error_count,
        "results": list(results),
    }


@app.get("/debug")
async def debug(isbn: str = Query(..., min_length=10, max_length=13)):
    async with sem:
        try:
            ctx = await get_context()
            page = await ctx.new_page()
        except Exception as e:
            return {"error": f"Browser: {e}"}

        try:
            url = f"https://www.casadellibro.com/libros?query={isbn}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            await accept_cookies(page)

            html = await page.content()
            text = await page.locator("body").inner_text(timeout=5000)
            current_url = page.url

            price_count = await page.locator("span.x-currency").count()
            article_count = await page.locator("article").count()
            result_item_count = await page.locator('[data-test="search-result-item"]').count()

            return {
                "current_url": current_url,
                "html_length": len(html),
                "body_text_preview": text[:2000],
                "price_elements_found": price_count,
                "article_elements_found": article_count,
                "search_result_items_found": result_item_count,
            }
        except Exception as e:
            return {"error": str(e)}
        finally:
            await page.close()
