from fastapi import FastAPI, Query
from pydantic import BaseModel, Field
import httpx
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

app = FastAPI(title="Casa del Libro API")

BATCH_CONCURRENCY = 3
sem = asyncio.Semaphore(BATCH_CONCURRENCY)

EMPATHY_SEARCH_URL = "https://api.empathy.co/search/v1/query/cdl/isbnsearch"
EMPATHY_BASE_PARAMS = {
    "internal": "true",
    "instance": "cdl",
    "lang": "es",
    "scope": "desktop",
    "currency": "EUR",
    "store": "ES",
    "start": 0,
    "rows": 16,
}

_http_client: Optional[httpx.AsyncClient] = None


def clean_isbn(isbn: str) -> str:
    return (isbn or "").strip().replace(" ", "").replace("-", "")


def format_price(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return None


def format_date(timestamp: Any) -> Optional[str]:
    if not timestamp:
        return None
    try:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%d/%m/%Y")
    except (TypeError, ValueError, OSError):
        return None


class BatchRequest(BaseModel):
    isbns: List[str] = Field(..., min_length=1, max_length=100)
    pause_ms: int = Field(default=1000, ge=0, le=10000)


@app.on_event("startup")
async def startup():
    global _http_client
    _http_client = httpx.AsyncClient(timeout=15.0)


@app.on_event("shutdown")
async def shutdown():
    global _http_client
    if _http_client:
        await _http_client.aclose()


@app.get("/health")
async def health():
    return {"status": "ok"}


def error_result(isbn: str, error: str, request_url: Optional[str] = None) -> Dict[str, Any]:
    return {
        "isbn": isbn,
        "title": None,
        "editorial": None,
        "encuadernacion": None,
        "anio_edicion": None,
        "fecha_lanzamiento": None,
        "price_current_eur": None,
        "price_previous_eur": None,
        "url": request_url,
        "error": error,
    }


def no_results_result(isbn: str, request_url: str) -> Dict[str, Any]:
    return {
        "isbn": isbn,
        "title": "-",
        "editorial": "-",
        "encuadernacion": "-",
        "anio_edicion": "-",
        "fecha_lanzamiento": "-",
        "price_current_eur": "-",
        "price_previous_eur": "-",
        "url": request_url,
        "error": "No se encontraron resultados",
    }


async def scrape_casadellibro_isbn(isbn: str) -> Dict[str, Any]:
    isbn = clean_isbn(isbn)

    if not isbn.isdigit() or not (10 <= len(isbn) <= 13):
        return error_result(isbn, "ISBN inválido")

    params = {**EMPATHY_BASE_PARAMS, "query": isbn}

    async with sem:
        try:
            resp = await _http_client.get(EMPATHY_SEARCH_URL, params=params)
            request_url = str(resp.request.url)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return error_result(isbn, f"Error consultando Casa del Libro: {e}")

        content = data.get("catalog", {}).get("content", [])
        if not content:
            return no_results_result(isbn, request_url)

        item = next((c for c in content if c.get("ean") == isbn), content[0])
        price = item.get("price") or {}

        return {
            "isbn": isbn,
            "title": item.get("name"),
            "editorial": item.get("editorial"),
            "encuadernacion": item.get("encuadernation"),
            "anio_edicion": item.get("yearPublication"),
            "fecha_lanzamiento": format_date(item.get("dateRelease")),
            "price_current_eur": format_price(price.get("current")),
            "price_previous_eur": format_price(price.get("previous")),
            "url": item.get("url") or request_url,
            "error": None,
        }


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

    results: List[Dict[str, Any]] = []
    for i in range(0, len(isbns), BATCH_CONCURRENCY):
        chunk = isbns[i:i + BATCH_CONCURRENCY]
        chunk_results = await asyncio.gather(*[scrape_casadellibro_isbn(isbn) for isbn in chunk])
        results.extend(chunk_results)
        if req.pause_ms and i + BATCH_CONCURRENCY < len(isbns):
            await asyncio.sleep(req.pause_ms / 1000)

    success_count = sum(1 for r in results if not r.get("error"))
    error_count = len(results) - success_count

    return {
        "source": "casadellibro",
        "count": len(results),
        "success_count": success_count,
        "error_count": error_count,
        "results": results,
    }


@app.get("/debug")
async def debug(isbn: str = Query(..., min_length=10, max_length=13)):
    isbn = clean_isbn(isbn)
    params = {**EMPATHY_BASE_PARAMS, "query": isbn}
    try:
        resp = await _http_client.get(EMPATHY_SEARCH_URL, params=params)
        return {
            "status_code": resp.status_code,
            "request_url": str(resp.request.url),
            "raw_response": resp.json(),
        }
    except Exception as e:
        return {"error": str(e)}
