#!/usr/bin/env python3
"""Genera las capturas del README a partir de la salida REAL de loagen.

No hay mockups ni texto escrito a mano: el script ejecuta los comandos, captura su
salida tal cual (con sus colores ANSI), la pinta dentro de una ventana de terminal en
HTML y la fotografía con Chromium (Playwright). Si loagen cambia, se vuelve a ejecutar
esto y las imágenes quedan al día.

Uso:
    python3 docs/capturas/generar.py
    python3 docs/capturas/generar.py --comando loagen --workspace ~/mi-proyecto
    python3 docs/capturas/generar.py --solo doctor tarea     # solo algunas

Playwright es opcional en loagen, pero aquí es obligatorio (es quien hace las fotos):
    pip install playwright && playwright install chromium
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

# --- ANSI -> HTML ---------------------------------------------------------
# Paleta de terminal oscura. Solo se usan los códigos que emite loagen (negrita, tenue,
# reset y algún color puntual), pero el conversor los acepta todos.
FG = {
    30: "#5c6370", 31: "#e06c75", 32: "#98c379", 33: "#e5c07b", 34: "#61afef",
    35: "#c678dd", 36: "#56b6c2", 37: "#abb2bf", 90: "#5c6370", 91: "#e06c75",
    92: "#98c379", 93: "#e5c07b", 94: "#61afef", 95: "#c678dd", 96: "#56b6c2",
    97: "#ffffff",
}
SGR = re.compile(r"\x1b\[([0-9;]*)m")


def _span(text: str, fg: str | None, bold: bool, dim: bool) -> str:
    if not text:
        return ""
    styles = []
    if fg:
        styles.append(f"color:{fg}")
    if bold:
        styles.append("font-weight:700")
    if dim:
        styles.append("opacity:.62")
    if not styles:
        return text
    return f'<span style="{";".join(styles)}">{text}</span>'


def ansi_to_html(text: str) -> str:
    """Convierte los colores ANSI de la captura en `<span>` con estilos CSS."""
    out: list[str] = []
    pos = 0
    fg: str | None = None
    bold = dim = False
    for match in SGR.finditer(text):
        out.append(_span(html.escape(text[pos:match.start()]), fg, bold, dim))
        pos = match.end()
        codes = [int(c) for c in (match.group(1) or "0").split(";") if c.strip()]
        for code in codes or [0]:
            if code == 0:
                fg, bold, dim = None, False, False
            elif code == 1:
                bold = True
            elif code == 2:
                dim = True
            elif code == 22:
                bold = dim = False
            elif code == 39:
                fg = None
            elif code in FG:
                fg = FG[code]
    out.append(_span(html.escape(text[pos:]), fg, bold, dim))
    return "".join(out)


PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><style>
  html, body {{ margin: 0; background: transparent; }}
  body {{ padding: 26px; font-family: ui-monospace, "DejaVu Sans Mono",
         "Liberation Mono", Menlo, Consolas, monospace; }}
  .win {{ background: #1b1f27; border: 1px solid #2b3140; border-radius: 12px;
          overflow: hidden; box-shadow: 0 14px 44px rgba(0,0,0,.45);
          width: calc({cols}ch + 42px); }}
  .bar {{ display: flex; align-items: center; gap: 8px; padding: 10px 14px;
          background: #232837; border-bottom: 1px solid #2b3140; }}
  .dot {{ width: 11px; height: 11px; border-radius: 50%; }}
  .title {{ margin-left: 8px; color: #8b93a7; font-size: 12px; letter-spacing: .02em; }}
  pre {{ margin: 0; padding: 16px 20px; color: #abb2bf; font-size: 13px;
         line-height: 1.55; white-space: pre-wrap; word-break: break-word; }}
</style></head><body>
<div class="win">
  <div class="bar">
    <span class="dot" style="background:#ff5f57"></span>
    <span class="dot" style="background:#febc2e"></span>
    <span class="dot" style="background:#28c840"></span>
    <span class="title">{title}</span>
  </div>
  <pre>{body}</pre>
</div>
</body></html>
"""


def build_html(title: str, raw: str, cols: int = 96) -> str:
    """Pinta la salida en una ventana de terminal de ancho fijo.

    El ancho es el mismo en todas las capturas para que el README quede alineado; si una
    línea no cabe, se envuelve (como haría una terminal de verdad).
    """
    return PAGE.format(cols=cols, title=html.escape(title),
                       body=ansi_to_html(raw.rstrip("\n")))


# El modelo a veces se descuelga y responde en otro alfabeto. Para una captura de
# documentación eso no vale, así que se reintenta.
NO_LATIN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")


def _plain(text: str) -> str:
    """Texto sin códigos ANSI: las marcas de color rompen cualquier comprobación."""
    return SGR.sub("", text or "")


def looks_presentable(raw: str) -> bool:
    """¿La salida sirve para documentar? (sin otros alfabetos ni restos de error)."""
    clean = _plain(raw)
    if NO_LATIN.search(clean):
        return False
    return "ERROR" not in clean and "Traceback" not in clean


# Tarea de la demo: deliberadamente la más simple que el modelo hace bien. Un 1.5B
# acierta poco en tareas libres (medido: inventa carpetas, se inventa respuestas y a
# veces entra en bucle creando directorios anidados), y una captura de documentación no
# puede enseñar un caso que salió mal.
DEMO_TASK = "escribe en notas.txt el texto hola"
DEMO_FILE = "notas.txt"
DEMO_CLEANUP = ("notas.txt",)


# Tarea multi-paso para la captura del plan: solo lectura a propósito, para que no haya
# artefactos que comprobar y se vea el plan funcionando (construido y usado).
PLAN_TASK = "revisa src/utilidades.py y src/main.py y dime si tests/test_utilidades.py cubre las funciones"

_BAD_MARKERS = ("se rindió", "no está terminado", "reintento guiado",
                "contradice la tarea", "no devolvió un plan", "ERROR")


def plan_task_is_good(raw: str, workspace: str) -> bool:
    """¿La captura enseña el task graph funcionando de verdad?"""
    clean = _plain(raw)
    if NO_LATIN.search(clean) or any(m in clean for m in _BAD_MARKERS):
        return False
    # Se exige ver el plan, que se haya usado (plan_update) y la evidencia del harness.
    return ("\nplan (" in "\n" + clean
            and "plan_update" in clean
            and 'read_file' in clean
            and all(path in clean for path in ('src/utilidades.py', 'src/main.py', 'tests/test_utilidades.py'))
            and 'Estado: completed' in clean)


def task_is_good(raw: str, workspace: str) -> bool:
    """¿La tarea de la captura se completó de verdad?

    Un `exit 0` no basta: el modelo de 1.5B a veces degenera (mete texto multilínea en un
    `path`, responde en otro alfabeto o se queda pidiendo el plan). Una captura de
    documentación tiene que enseñar un caso que salió bien, así que se comprueba el
    ARTEFACTO en disco, no lo que dice el modelo.
    """
    clean = _plain(raw)
    target = os.path.join(workspace, DEMO_FILE)
    if not os.path.isfile(target):
        return False
    try:
        with open(target, encoding="utf-8", errors="replace") as fh:
            if "hola" not in fh.read().lower():
                return False
    except OSError:
        return False
    if NO_LATIN.search(clean) or "no está terminado" in clean:
        return False
    # Nada de capturas a medias: ni avisos del plan al final, ni reintentos, ni bucles.
    for marker in ("no está terminado", "se rindió", "aviso de plan",
                   "reintento guiado", "se cortó contra el tope", "contradice la tarea"):
        if marker in clean:
            return False
    lines = [line for line in clean.splitlines() if line.strip()]
    return bool(lines) and "Continúa AHORA" not in lines[-1]


# --- Capturas -------------------------------------------------------------
class Capture:
    def __init__(self, slug: str, title: str, args: list[str], task: str | None = None,
                 timeout: int = 240, retries: int = 1, cleanup: str | None = None,
                 verify: object = None):
        self.slug = slug
        self.title = title
        self.args = args
        self.task = task
        self.timeout = timeout
        self.retries = retries
        self.cleanup = cleanup  # se borra del workspace antes de cada intento (lista)
        self.verify = verify    # callable(salida, workspace) -> bool


def run(command: list[str], workspace: str, timeout: int = 240):
    """Ejecuta el comando y devuelve (salida combinada, código, segundos). Nunca lanza."""
    env = dict(os.environ, NO_COLOR="", TERM="xterm-256color", COLUMNS="120")
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command, cwd=workspace, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=timeout,
        )
        out, code = proc.stdout, proc.returncode
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        out = partial + f"\n[timeout tras {timeout}s]"
        code = 124
    except OSError as exc:
        out, code = f"ERROR: no se pudo ejecutar {command[0]}: {exc}", 127
    elapsed = time.monotonic() - started
    return out, code, elapsed


def ollama_up(host: str = "http://127.0.0.1:11434") -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=4):
            return True
    except (urllib.error.URLError, OSError):
        return False


def captures(command: list[str], workspace: str) -> list[Capture]:
    base = command + ["-w", workspace]
    return [
        Capture("init", "loagen --init", base + ["--init", "--force"]),
        Capture("doctor", "loagen --doctor", base + ["--doctor"]),
        Capture("herramientas", "loagen --tools", base + ["--tools"]),
        Capture("tarea", f'loagen "{DEMO_TASK}"', base + ["--yes", '--expect-content', 'notas.txt=hola', DEMO_TASK],
                task=DEMO_TASK, retries=6, cleanup=DEMO_CLEANUP, verify=task_is_good),
        Capture("plan", f'loagen --plan "{PLAN_TASK}"', base + ["--yes", "--plan", PLAN_TASK],
                task=PLAN_TASK, retries=4, verify=plan_task_is_good),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comando", default=None,
                        help="ejecutable de loagen (def. `loagen` si está en el PATH)")
    parser.add_argument("--workspace", default=os.getcwd(),
                        help="workspace de la demo (def. el directorio actual)")
    parser.add_argument("--out", default=HERE, help="carpeta de salida de los PNG")
    parser.add_argument("--solo", nargs="*", default=None,
                        help="genera solo estas capturas (init, doctor, herramientas, tarea)")
    parser.add_argument("--cols", type=int, default=96,
                        help="ancho de la ventana en columnas (def. 96)")
    parser.add_argument("--sin-navegador", action="store_true",
                        help="solo escribe el HTML, sin fotografiar (para depurar)")
    args = parser.parse_args(argv)

    command = args.comando
    if not command:
        candidate = os.path.join(REPO, "agente.py")
        command = "loagen" if _on_path("loagen") else f"python3 {candidate}"
    command = shlex.split(command) if isinstance(command, str) else command

    # All demo mutations are confined to a new owned temporary directory.
    demo = tempfile.TemporaryDirectory(prefix='loagen-capturas-')
    workspace = demo.name
    os.makedirs(os.path.join(workspace, 'src'))
    os.makedirs(os.path.join(workspace, 'tests'))
    fixtures = {'src/utilidades.py': 'def saludar():\n    return "hola"\n\ndef normalizar(text):\n    return text.strip()\n',
                'src/main.py': 'from utilidades import saludar\nprint(saludar())\n',
                'tests/test_utilidades.py': 'from src.utilidades import saludar\ndef test_saludar():\n    assert saludar() == "hola"\n'}
    for path, text in fixtures.items():
        with open(os.path.join(workspace, path), 'w', encoding='utf-8') as fh:
            fh.write(text)
    os.makedirs(args.out, exist_ok=True)
    print(f"loagen   : {' '.join(command)}")
    print(f"workspace: {workspace}")
    print(f"salida   : {args.out}\n")

    todo = captures(command, workspace)
    if args.solo:
        want = {s.lower() for s in args.solo}
        if not want or want - {c.slug for c in todo}:
            print('ERROR: captura desconocida o selección vacía', file=sys.stderr)
            return 2
        todo = [c for c in todo if c.slug in want]

    if not ollama_up() and any(c.task for c in todo):
        print('ERROR: Ollama no responde; no se pueden generar las tareas solicitadas.', file=sys.stderr)
        return 2

    playwright = None
    if not args.sin_navegador:
        try:
            from playwright.sync_api import sync_playwright
            playwright = sync_playwright().start()
        except ImportError:
            print("ERROR: falta Playwright. Instálalo con:\n"
                  "  pip install playwright && playwright install chromium",
                  file=sys.stderr)
            return 2
        except Exception as exc:  # navegador no instalado, etc.
            print(f"ERROR: Playwright no arrancó: {exc}", file=sys.stderr)
            return 2

    # Chromium necesita una combinación concreta en muchos entornos (contenedores, VPS,
    # sin GPU). Se prueban en orden hasta que una funcione, igual que hace loagen.
    launch_args = [
        ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"],
        ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-zygote"],
        ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    ]

    status = 0
    for capture in todo:
        raw, code, elapsed, accepted = "", 1, 0.0, False
        for attempt in range(1, capture.retries + 1):
            for item in capture.cleanup or ():
                path = os.path.join(workspace, item)
                shutil.rmtree(path, ignore_errors=True)
                if os.path.isfile(path):
                    os.remove(path)
            raw, code, elapsed = run(capture.args, workspace, capture.timeout)
            first = raw.strip().split("\n")[0][:88] if raw.strip() else "(sin salida)"
            tag = f" (intento {attempt}/{capture.retries})" if capture.retries > 1 else ""
            print(f"[{capture.slug}] exit={code} {elapsed:.1f}s{tag} · {first}")
            accepted = code == 0 and looks_presentable(raw)
            if accepted and capture.verify:
                accepted = capture.verify(raw, workspace)
            if accepted:
                break
            print("    no vale para documentar (error, otro alfabeto, bucle o tarea a"
                  " medias)" + (": se reintenta" if attempt < capture.retries else ""))

        document = build_html(capture.title, raw, cols=args.cols)
        tmp = os.path.join(tempfile.gettempdir(), f"loagen-{capture.slug}.html")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(document)
        if not accepted:
            # Nunca se publica una captura que no cumple el criterio: se aparta con otro
            # nombre para poder mirarla, y el README no la usa.
            rejected = os.path.join(args.out, f"{capture.slug}.rechazada.png")
            print(f"    AVISO: ninguna captura vale; se aparta como "
                  f"{os.path.basename(rejected)} (no la usa el README)", file=sys.stderr)
            status = 1

        if playwright is None:
            print(f"    html: {tmp}")
            if code != 0:
                status = 1
            continue

        png = os.path.join(args.out, f"{capture.slug}.png" if accepted
                           else f"{capture.slug}.rechazada.png")
        shot, error = _shoot(playwright, document, png, launch_args)
        if shot:
            size = os.path.getsize(png)
            print(f"    -> {os.path.relpath(png, REPO)}  ({size // 1024} KB)")
        else:
            print(f"    ERROR al fotografiar: {error}", file=sys.stderr)
            status = 1

    if playwright is not None:
        playwright.stop()
    print("\nlisto." if status == 0 else "\nterminado con avisos.")
    demo.cleanup()
    return status


def _on_path(name: str) -> bool:
    import shutil

    return shutil.which(name) is not None


def _shoot(playwright, document: str, png: str, launch_args) -> tuple[bool, str]:
    errors = []
    for extra in launch_args:
        try:
            browser = playwright.chromium.launch(headless=True, args=extra)
        except Exception as exc:
            errors.append(str(exc)[:200])
            continue
        try:
            page = browser.new_page(viewport={"width": 900, "height": 600},
                                    device_scale_factor=2)
            page.set_content(document, wait_until="load")
            page.screenshot(path=png, full_page=True, omit_background=True)
        except Exception as exc:
            errors.append(str(exc)[:200])
            continue
        finally:
            browser.close()
        return True, ""
    return False, " | ".join(errors) or "sin detalles"


if __name__ == "__main__":
    raise SystemExit(main())
