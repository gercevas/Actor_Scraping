from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import asyncio
import httpx
from pathlib import Path
from typing import Optional, List, Dict, Any

# ─── Carpeta de imágenes ──────────────────────────────────────────────────────
# En Railway: monta un Volume en /app/static para persistencia entre deploys
# En local: se crea automáticamente en ./static/images
IMAGES_DIR = Path("/app/static/images")
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# ─── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="Casa del Libro — Image API")

# Sirve las imágenes como archivos estáticos
# URL pública: https://tu-app.railway.app/static/images/{isbn}.jpg
app.mount("/static", StaticFiles(directory="/app/static"), name="static")

_pw = None
_browser = None
_context = None
_browser_ready = asyncio.Event()
sem = asyncio.Semaphore(1)

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['es-ES', 'es', 'en']});
window.chrome = {runtime: {}};
"""


# ─── Helpers ──────────────────────────────────────────────────────────────────

def clean_isbn(isbn: str) -> str:
    return (isbn or "").strip().replace(" ", "").replace("-", "")


# ─── Modelos ──────────────────────────────────────────────────────────────────

class ImageBatchRequest(BaseModel):
    isbns: List[str] = Field(..., min_length=1, max_length=50)
    pause_ms: int = Field(default=1500, ge=0, le=10000)


# ─── Browser ──────────────────────────────────────────────────────────────────

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


# ─── Core: scraping + descarga de imagen ─────────────────────────────────────

async def fetch_image(isbn: str) -> Dict[str, Any]:
    """
    Busca el ISBN en Casa del Libro, extrae img[data-test='result-picture-image'],
    descarga el JPG y lo guarda en IMAGES_DIR/{isbn}.jpg.

    Si el archivo ya existe en disco devuelve cached=True sin hacer scraping.
    """
    global _context
    isbn = clean_isbn(isbn)

    if not isbn.isdigit() or not (10 <= len(isbn) <= 13):
        return {"isbn": isbn, "image_url": None, "cached": False, "error": "ISBN inválido"}

    dest = IMAGES_DIR / f"{isbn}.jpg"

    # Caché: ya descargada
    if dest.exists():
        return {
            "isbn": isbn,
            "image_url": f"/static/images/{isbn}.jpg",
            "cached": True,
            "error": None,
        }

    url = f"https://www.casadellibro.com/libros?query={isbn}"

    async with sem:
        try:
            ctx = await get_context()
            page = await ctx.new_page()
        except Exception as e:
            _context = None
            return {"isbn": isbn, "image_url": None, "cached": False, "error": f"Browser error: {e}"}

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            await accept_cookies(page)

            try:
                await page.wait_for_selector(
                    'img[data-test="result-picture-image"]', timeout=10000
                )
            except PlaywrightTimeoutError:
                pass

            # Sin resultados
            for txt in ["No se han encontrado resultados", "No se encontraron resultados"]:
                try:
                    if await page.get_by_text(txt).count() > 0:
                        return {"isbn": isbn, "image_url": None, "cached": False, "error": "Sin resultados en Casa del Libro"}
                except Exception:
                    pass

            # Extraer src
            img_src = None
            try:
                loc = page.locator('img[data-test="result-picture-image"]').first
                if await loc.count() > 0:
                    img_src = await loc.get_attribute("src", timeout=5000)
            except Exception:
                pass

            if not img_src:
                return {"isbn": isbn, "image_url": None, "cached": False, "error": "Imagen no encontrada en el DOM"}

            # Descargar
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(img_src, headers={"Referer": "https://www.casadellibro.com/"})
                if r.status_code == 200:
                    dest.write_bytes(r.content)
                    return {
                        "isbn": isbn,
                        "image_url": f"/static/images/{isbn}.jpg",
                        "cached": False,
                        "error": None,
                    }
                else:
                    return {"isbn": isbn, "image_url": None, "cached": False, "error": f"HTTP {r.status_code} descargando imagen"}

        except PlaywrightTimeoutError:
            return {"isbn": isbn, "image_url": None, "cached": False, "error": "Timeout"}
        except Exception as e:
            return {"isbn": isbn, "image_url": None, "cached": False, "error": str(e)}
        finally:
            await page.close()


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "browser_ready": _browser_ready.is_set()}


@app.get(
    "/image",
    summary="Imagen de portada de un libro por ISBN",
)
async def image_single(isbn: str = Query(..., min_length=10, max_length=13)):
    """
    Devuelve la URL de la imagen de portada de un libro.
    Si ya fue descargada antes, la sirve desde caché.

    Ejemplo: GET /image?isbn=9781382008396
    Respuesta: { "isbn": "...", "image_url": "/static/images/9781382008396.jpg", "cached": false }
    """
    return await fetch_image(isbn)


@app.post(
    "/image/batch",
    summary="Imágenes de portada para lista de ISBNs",
)
async def image_batch(req: ImageBatchRequest):
    """
    Descarga imágenes para hasta 50 ISBNs por petición.
    Los ISBNs ya descargados se sirven desde caché sin hacer scraping.
    Usa pause_ms para controlar la velocidad y evitar bloqueos.

    n8n llamará a este endpoint en bucle con lotes de 50 hasta procesar los 3334 ISBNs.
    """
    isbns = [clean_isbn(x) for x in req.isbns if clean_isbn(x)]
    if not isbns:
        return {"count": 0, "success_count": 0, "error_count": 0, "results": []}

    results = []
    for i, isbn in enumerate(isbns):
        result = await fetch_image(isbn)
        results.append(result)
        if i < len(isbns) - 1 and req.pause_ms > 0:
            await asyncio.sleep(req.pause_ms / 1000)

    success_count = sum(1 for r in results if not r.get("error"))
    error_count = len(results) - success_count

    return {
        "count": len(results),
        "success_count": success_count,
        "error_count": error_count,
        "results": results,
    }


@app.get(
    "/image/status",
    summary="Cuántas imágenes están descargadas",
)
async def image_status():
    """
    Útil para monitorizar el progreso del batch desde n8n o manualmente.
    Devuelve cuántos archivos .jpg hay ya en disco.
    """
    files = list(IMAGES_DIR.glob("*.jpg"))
    return {
        "downloaded": len(files),
        "images_dir": str(IMAGES_DIR),
    }