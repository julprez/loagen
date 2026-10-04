"""Navegador web: buscar, leer y (si hace falta) renderizar.

Sin dependencias obligatorias y sin claves de API:

* **Buscar** -> DuckDuckGo HTML. Sus enlaces van redirigidos (`/l/?uddg=<url>`), así
  que hay que decodificar el parámetro `uddg`; si no, se devuelven URLs de DuckDuckGo
  en vez de las de los resultados.
* **Wikipedia** -> la API REST da un resumen directo, en español, sin parsear HTML.
* **Leer una página** -> se quita el ruido (script/style/nav) y se extrae el texto.
* **Páginas con JavaScript** -> solo si el texto extraído es sospechosamente pobre y
  hay Playwright instalado, se renderiza con Chromium. Es un extra **opcional**: si
  falla, se devuelve lo que se haya podido extraer.

Todo devuelve texto plano listo para el modelo, con los errores como texto (nunca
lanzando): el modelo tiene que poder leer el fallo y reaccionar.
"""

from __future__ import annotations

import html
import importlib.util
from .policy.network import validate_url
import json
import re
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 20

_RESULT_RX = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S
)
_SNIPPET_RX = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)
_DDG_REDIRECT = re.compile(r"uddg=([^&\"']+)")
_TAG_RX = re.compile(r"(?s)<[^>]+>")
_SCRIPT_RX = re.compile(r"(?is)<(script|style|noscript|svg|template|head)[^>]*>.*?</\1>")
_BLOCK_RX = re.compile(r"(?is)</?(p|div|br|li|tr|h[1-6]|section|article)[^>]*>")


class WebError(RuntimeError):
    pass


class CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _get(url: str, timeout: int = DEFAULT_TIMEOUT, allow_private: bool = False) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        },
    )
    try:
        validate_url(url, allow_private)
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler() if allow_private else CheckedRedirect())
        with opener.open(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read(600_000).decode(charset, "replace")
    except urllib.error.HTTPError as exc:
        raise WebError(f"HTTP {exc.code} al pedir {url}") from exc
    except urllib.error.URLError as exc:
        raise WebError(f"no se pudo conectar ({exc.reason})") from exc
    except (TimeoutError, OSError) as exc:
        raise WebError(f"timeout o error de red ({type(exc).__name__})") from exc


def _clean(fragment: str) -> str:
    return html.unescape(_TAG_RX.sub("", fragment)).strip()


def search(query: str, limit: int = 5, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Busca en DuckDuckGo. Devuelve [{title, url, snippet}] (puede venir vacío)."""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    body = _get(url, timeout)

    titles = _RESULT_RX.findall(body)
    snippets = [_clean(s) for s in _SNIPPET_RX.findall(body)]

    results: list[dict] = []
    for index, (href, title_html) in enumerate(titles):
        real = _real_url(href)
        if not real:
            continue
        results.append({
            "title": _clean(title_html) or real,
            "url": real,
            "snippet": snippets[index] if index < len(snippets) else "",
        })
        if len(results) >= limit:
            break
    return results


def _real_url(href: str) -> str:
    """Deshace la redirección de DuckDuckGo y descarta anuncios."""
    match = _DDG_REDIRECT.search(href)
    if match:
        target = urllib.parse.unquote(match.group(1))
        return target if target.startswith("http") else ""
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("http") and "duckduckgo.com" not in href:
        return href
    return ""


def wikipedia_summary(topic: str, lang: str = "es", timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Resumen de Wikipedia: la vía más rápida y fiable para «¿qué es X?»."""
    title = urllib.parse.quote(topic.strip().replace(" ", "_"))
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
    try:
        data = json.loads(_get(url, timeout))
    except (WebError, ValueError):
        data = {}

    if data.get("extract"):
        return {
            "title": data.get("title", topic),
            "extract": data["extract"],
            "url": (data.get("content_urls", {}).get("desktop", {}) or {}).get("page", url),
        }

    # La búsqueda directa falló: se usa el buscador de Wikipedia.
    api = (f"https://{lang}.wikipedia.org/w/api.php?action=opensearch"
           f"&search={urllib.parse.quote(topic)}&limit=3&format=json")
    try:
        parsed = json.loads(_get(api, timeout))
        if isinstance(parsed, list) and len(parsed) >= 4 and parsed[3]:
            return wikipedia_summary(parsed[1][0], lang, timeout)
    except (WebError, ValueError, IndexError, TypeError):
        pass
    return {}


def page_text(url: str, max_chars: int = 6000, timeout: int = DEFAULT_TIMEOUT,
              render: bool = True, allow_private: bool = False) -> tuple[str, bool]:
    """Devuelve (texto, renderizado_con_navegador)."""
    """Devuelve (texto, renderizado_con_navegador).

    Si el HTML estático da muy poco texto y Playwright está disponible, se renderiza
    con Chromium para ejecutar el JavaScript.
    """
    text = ""
    try:
        text = html_to_text(_get(url, timeout, allow_private=True) if allow_private else _get(url, timeout))
    except WebError as exc:
        if not render:
            raise
        rendered = _render(url, timeout, allow_private)
        if rendered is None:
            raise WebError(f"{exc} (y el render falló: {last_render_error})") from exc
        return rendered[:max_chars], True

    if render and len(text) < 400:
        rendered = _render(url, timeout, allow_private)
        if rendered and len(rendered) > len(text):
            return rendered[:max_chars], True
    return text[:max_chars], False


def html_to_text(raw: str) -> str:
    """Convierte HTML en texto legible, conservando saltos de párrafo."""
    if not raw:
        return ""
    body = _SCRIPT_RX.sub(" ", raw)
    body = _BLOCK_RX.sub("\n", body)
    body = _TAG_RX.sub(" ", body)
    body = html.unescape(body)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in body.splitlines()]
    return "\n".join(ln for ln in lines if ln)


# Chromium en contenedor necesita flags extra. Medido en este equipo: con los flags
# "normales" (--no-sandbox --disable-dev-shm-usage) la página **crashea**
# (`Page.goto: Page crashed`); con --single-process o --no-zygote funciona. En vez de
# fijar uno, se prueban en orden hasta que uno arranca.
_LAUNCH_ARGS: list[list[str]] = [
    ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"],
    ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-zygote"],
    ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
]

# Último fallo de render, para poder contarlo en vez de tragárselo en silencio.
last_render_error = ""


def _render(url: str, timeout: int, allow_private: bool = False) -> str | None:
    """Renderiza con Chromium vía Playwright. Devuelve None si no lo consigue."""
    global last_render_error
    last_render_error = ""
    try:
        sync_playwright = __import__('playwright.sync_api', fromlist=['sync_playwright']).sync_playwright
    except ImportError:
        last_render_error = "Playwright no está instalado"
        return None

    errors: list[str] = []
    for args in _LAUNCH_ARGS:
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=args)
                try:
                    page = browser.new_page(user_agent=UA, locale="es-ES")
                    def route_request(route):
                        try:
                            validate_url(route.request.url, allow_private)
                        except ValueError:
                            route.abort()
                        else:
                            route.continue_()
                    page.route('**/*', route_request)
                    page.goto(url, timeout=min(timeout * 1000, 30_000),
                              wait_until="domcontentloaded")
                    page.wait_for_timeout(700)  # deja que se monte el JavaScript
                    text = html_to_text(page.content())
                    if len(text) < 200:
                        text = page.inner_text("body")
                finally:
                    browser.close()
            if text and text.strip():
                return text
            errors.append("el navegador no devolvió texto")
        except Exception as exc:  # noqa: BLE001 - el navegador es un extra
            first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            errors.append(f"{type(exc).__name__}: {first}")
            continue
    last_render_error = " | ".join(dict.fromkeys(errors))[:300]
    return None


def available_renderer() -> bool:
    return importlib.util.find_spec('playwright') is not None