"""Tool Dispatch: registro tipado con un handler por herramienta.

Reglas de diseño que hacen que un 1.5B no se pierda:

* **Pocas herramientas** y con nombres literales.
* **Descripciones con guía negativa** ("esto NO es para carpetas"): fue lo que
  arregló el error real de confundir `mkdir` con `write_file`.
* **Errores como observación**: todo handler devuelve texto, nunca lanza; el
  modelo ve `ERROR: ...` y se autocorrige en el paso siguiente.
"""

from __future__ import annotations

import fnmatch
import glob as globlib
import json
import os
import re
import itertools
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Protocol
from ..storage.memory import Memory, SourceLedger
from ..cerebro_tool import Cerebro
from ..plan import Plan


class BudgetLike(Protocol):
    def remaining(self) -> float: ...


class ResourcesLike(Protocol):
    def enter_context(self, context): ...

from .. import navegador
from .process import run_bounded, shell_command
from .result import ToolResult
from .schema import validate
from ..policy.network import validate_url


@dataclass
class ToolContext:
    workspace: str
    max_output: int = 4000
    allow_outside: bool = False
    memory: Memory | None = None
    plan: Plan | None = None
    spawn: Callable[[str], str] | None = None  # callable(tarea) -> informe del subagente
    cerebro: Cerebro | None = None  # Cerebro (buscador local del workspace)
    web: bool = True  # permite buscar/leer en la web
    sources: SourceLedger | None = None  # SourceLedger (fuentes citables de la sesión)
    # Intérprete de `bash`. Vacío = el del sistema (`/bin/sh` en POSIX, `cmd.exe` en
    # Windows). Se pasa a subprocess como `executable`: en Windows es la vía para usar
    # Git Bash (`shell = "bash"`) en vez de cmd.exe.
    shell: str = ""
    budget: BudgetLike | None = None
    resources: ResourcesLike | None = None
    clients: dict = field(default_factory=dict)
    allow_private_network: bool = False


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, ToolContext], str | ToolResult]
    mutating: bool = False
    # Nombres alternativos de parámetro -> nombre canónico. Un 1.5B mezcla idiomas
    # (`symbol` por `simbolo`, `q` por `query`): se aceptan en el registro para no
    # gastar un turno en un "faltan parámetros" que el modelo no entiende.
    aliases: dict = field(default_factory=dict)


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


def _clip(text: str, limit: int) -> str:
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [recortado: {len(text) - limit} caracteres más]"


def resolve_path(ctx: ToolContext, path: str) -> str:
    """Resuelve `path` relativo al workspace y bloquea escapes salvo permiso explícito."""
    if not path or not str(path).strip():
        raise ValueError("falta el parámetro 'path'")
    text = str(path)
    # Medido: el modelo a veces mete un script entero en `path`
    # (`"demo\nmkdir demo/x\nwrite_file demo/x"`) y se creaba una carpeta con saltos de
    # línea en el nombre. Una ruta es una ruta: nada de caracteres de control.
    if any(ch in text for ch in "\r\n\t\x00"):
        raise ValueError(
            "la ruta lleva saltos de línea o tabuladores. Pasa UNA sola ruta y haz una "
            "llamada por operación (una para mkdir, otra para write_file)"
        )
    raw = os.path.expanduser(text.strip())
    full = raw if os.path.isabs(raw) else os.path.join(ctx.workspace, raw)
    if os.path.exists(full) and not (os.path.isfile(full) or os.path.isdir(full)):
        raise PermissionError('solo se admiten ficheros y carpetas normales')
    full = os.path.realpath(full)
    if not ctx.allow_outside:
        root = os.path.realpath(ctx.workspace)
        if full != root and not full.startswith(root + os.sep):
            raise PermissionError(
                f"{full} está fuera del workspace ({root}). "
                "Pide permiso al usuario o trabaja dentro del workspace."
            )
    return full


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
def _h_read_file(args: dict, ctx: ToolContext) -> str:
    path = resolve_path(ctx, args.get("path", ""))
    if os.path.isdir(path):
        return f"ERROR: {path} es una carpeta. Usa list_dir para verla."
    if os.path.getsize(path) > 64_000_000:
        return 'ERROR: fichero demasiado grande para read_file (máximo 64 MB)'
    offset = int(args.get("offset") or 1)
    limit = int(args.get("limit") or 400)
    chunk: list[str] = []
    size = 0
    more = False
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for number, line in enumerate(fh, 1):
            if ctx.budget is not None and number % 1000 == 0 and ctx.budget.remaining() <= 0:
                return 'ERROR: presupuesto de tiempo agotado durante lectura'
            if number > 1_000_000:
                return 'ERROR: offset excede el límite de exploración (un millón de líneas)'
            if number < offset:
                continue
            if len(chunk) >= limit or size >= ctx.max_output:
                more = True
                break
            if len(line) > ctx.max_output and not line.endswith('\n'):
                more = True
            item = f"{number:>5}| {line}"
            chunk.append(item[:max(0, ctx.max_output - size)])
            size += len(item)
            if more:
                break
    if not chunk and offset > 1:
        return f"ERROR: offset {offset} fuera de rango."
    return ''.join(chunk) + ('\n... [hay más líneas o contenido]' if more or size > ctx.max_output else '') or '(fichero vacío)'


def _h_write_file(args: dict, ctx: ToolContext) -> str:
    path = resolve_path(ctx, args.get("path", ""))
    if os.path.isdir(path):
        return (f"ERROR: {path} es una CARPETA, no una ruta de fichero. "
                "Escribe dentro, p. ej. '" + os.path.join(path, "fichero.txt") + "'.")
    content = args.get("content")
    if content is None:
        return "ERROR: falta 'content'."
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(content))
    return f"escrito {path} ({len(str(content))} caracteres)"


def _h_edit_file(args: dict, ctx: ToolContext) -> str:
    path = resolve_path(ctx, args.get("path", ""))
    old, new = args.get("old"), args.get("new")
    if not old:
        return "ERROR: falta 'old' (texto exacto a reemplazar)."
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    count = text.count(old)
    if count == 0:
        return "ERROR: 'old' no aparece literalmente en el fichero. Léelo antes con read_file."
    if count > 1 and not args.get("all"):
        return f"ERROR: 'old' aparece {count} veces. Añade más contexto o usa all=true."
    text = text.replace(old, new if new is not None else "")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return f"editado {path} ({count} reemplazo(s))"


def _h_mkdir(args: dict, ctx: ToolContext) -> str:
    path = resolve_path(ctx, args.get("path", ""))
    os.makedirs(path, exist_ok=True)
    return f"carpeta creada: {path}"


def _h_list_dir(args: dict, ctx: ToolContext) -> str:
    path = resolve_path(ctx, args.get("path", "."))
    if not os.path.exists(path):
        return f"ERROR: {path} no existe."
    if not os.path.isdir(path):
        return f"ERROR: {path} no es una carpeta. Usa read_file."
    with os.scandir(path) as scan:
        entries = sorted(entry.name for entry in itertools.islice(scan, 200))
    if not entries:
        return "(carpeta vacía)"
    lines = [
        f"{name}/" if os.path.isdir(os.path.join(path, name)) else name
        for name in entries
    ]
    return _clip("\n".join(lines), ctx.max_output)


def _h_glob(args: dict, ctx: ToolContext) -> str:
    pattern = args.get("pattern") or "**/*"
    full_pattern = resolve_path(ctx, pattern)
    root = os.path.realpath(ctx.workspace)
    rel = []
    for match in itertools.islice(globlib.iglob(full_pattern, recursive=True), 10000):
        if ctx.budget is not None and ctx.budget.remaining() <= 0:
            return 'ERROR: presupuesto de tiempo agotado durante glob'
        try:
            full = resolve_path(ctx, match)
        except PermissionError:
            continue
        if os.path.isfile(full):
            rel.append(os.path.relpath(full, root))
        if len(rel) >= 200:
            break
    rel.sort()
    if not rel:
        return f"(sin coincidencias para {pattern})"
    return _clip("\n".join(rel[:200]), ctx.max_output)


def _h_grep(args: dict, ctx: ToolContext) -> str:
    pattern = args.get("pattern")
    if not pattern:
        return "ERROR: falta 'pattern'."
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return f"ERROR: regex inválida ({exc})."
    base = resolve_path(ctx, args.get("path") or ".")
    file_glob = args.get("glob") or "*"
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            d for d in dirnames
            if d not in (".git", "node_modules", "__pycache__", ".loagen", ".local-agente")
        ]
        for filename in filenames:
            if ctx.budget is not None and ctx.budget.remaining() <= 0:
                return 'ERROR: presupuesto de tiempo agotado durante búsqueda'
            if not fnmatch.fnmatch(filename, file_glob):
                continue
            full = os.path.join(dirpath, filename)
            try:
                full = resolve_path(ctx, full)
                if os.path.getsize(full) > 2_000_000:
                    continue
                with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if rx.search(line):
                            hits.append(f"{os.path.relpath(full, ctx.workspace)}:{lineno}: {line.rstrip()[:200]}")
                            if len(hits) >= 200:
                                break
            except (OSError, UnicodeDecodeError):
                continue
            if len(hits) >= 200:
                break
        if len(hits) >= 200:
            break
    if not hits:
        return f"(sin coincidencias de {pattern!r})"
    return _clip("\n".join(hits), ctx.max_output)


def _h_bash(args: dict, ctx: ToolContext) -> str:
    command = args.get("command")
    if not command:
        return "ERROR: falta 'command'."
    try:
        timeout = float(min(int(args.get('timeout') or 60), 120))
        if ctx.budget is not None:
            timeout = min(timeout, max(.01, ctx.budget.remaining()))
        code, output, truncated = run_bounded(shell_command(command, ctx.shell),
                                             ctx.workspace, timeout, ctx.max_output * 4)
    except subprocess.TimeoutExpired:
        return "ERROR: el comando superó el tiempo límite."
    except OSError as exc:
        # Un `shell` mal configurado (o un POSIX en Windows sin Git Bash) da un error
        # críptico de subprocess: se devuelve como observación para que el modelo/usuario
        # sepa qué arreglar.
        return (f"ERROR: no se pudo usar el shell {ctx.shell!r} ({exc}). "
                "Revisa `shell` en rules.toml o deja vacío para el del sistema.")
    body = output.strip() or '(sin salida)'
    if truncated:
        body = 'ERROR: el comando excedió el límite de salida y fue detenido.\n' + body
    elif code != 0:
        body = f'ERROR: el comando falló (exit {code})\n{body}'
    return _clip(body, ctx.max_output)


def _remember_source(ctx: ToolContext, kind: str, ref: str, title: str = "") -> dict | None:
    """Registra una fuente consultada para poder citarla después."""
    ledger = getattr(ctx, "sources", None)
    if ledger is None:
        return None
    return ledger.add(kind, ref, title)


def _h_fetch_url(args: dict, ctx: ToolContext) -> str:
    """Lee una página concreta. Renderiza con Chromium si el HTML estático no basta."""
    url = args.get("url")
    if not url:
        return "ERROR: falta 'url'."
    if not str(url).startswith(("http://", "https://")):
        return "ERROR: 'url' debe empezar por http:// o https://"
    try:
        validate_url(str(url), ctx.allow_private_network)
        text, rendered = navegador.page_text(
            str(url), max_chars=ctx.max_output,
            render=bool(ctx.web and args.get("render", True)),
            allow_private=ctx.allow_private_network,
        )
    except navegador.WebError as exc:
        return (f"ERROR al leer {url}: {exc}. Prueba con web_search para encontrar otra "
                "fuente.")
    if not text.strip():
        return f"ERROR: {url} no devolvió texto legible (¿requiere JavaScript o login?)."
    entry = _remember_source(ctx, "url", str(url))
    mark = " (renderizado con navegador)" if rendered else ""
    cite = f"\n[FUENTE REGISTRADA #{entry['n']}: cítala como [{entry['n']}]]" if entry else ""
    return f"TEXTO DE {url}{mark}:\n{text}{cite}"


def _h_web_search(args: dict, ctx: ToolContext) -> str:
    """Busca en la web. Es la herramienta para lo que el modelo NO sabe."""
    if not ctx.web:
        return "ERROR: la web está desactivada en esta ejecución."
    query = args.get("query") or args.get("q")
    if not query:
        return "ERROR: falta 'query'."
    try:
        results = navegador.search(str(query), limit=int(args.get("limit") or 5))
    except navegador.WebError as exc:
        return f"ERROR al buscar {query!r}: {exc}"
    if not results:
        return (f"Sin resultados para {query!r}. Reformula con menos palabras o usa "
                "términos en inglés.")
    _remember_source(ctx, "búsqueda", str(query))
    lines = [f"RESULTADOS DE BÚSQUEDA para {query!r}:"]
    for index, item in enumerate(results, 1):
        entry = _remember_source(ctx, "resultado", item["url"], item["title"])
        mark = f" [#{entry['n']}]" if entry else ""
        lines.append(f"{index}. {item['title']}{mark}\n   {item['url']}")
        if item["snippet"]:
            lines.append(f"   {item['snippet'][:220]}")
    lines.append("Para el detalle de uno: fetch_url(url). Para citar: fuentes.")
    return "\n".join(lines)


def _h_wiki(args: dict, ctx: ToolContext) -> str:
    """Resumen de Wikipedia: la vía rápida y fiable para «¿qué es X?»."""
    if not ctx.web:
        return "ERROR: la web está desactivada en esta ejecución."
    topic = str(args.get("topic") or '').strip()
    if not topic:
        return "ERROR: falta 'topic'."
    data = navegador.wikipedia_summary(topic, lang=str(args.get("lang") or "es"))
    if not data:
        return (f"Wikipedia no tiene un artículo claro para {topic!r}. "
                "Prueba con web_search.")
    _remember_source(ctx, "wiki", data["url"], data["title"])
    return (f"WIKIPEDIA — {data['title']}\n{data['extract'][:1200]}\n(fuente: {data['url']})")


def _h_fuentes(args: dict, ctx: ToolContext) -> str:
    """Devuelve las fuentes consultadas, numeradas, para citarlas sin inventar."""
    ledger = getattr(ctx, "sources", None)
    if ledger is None:
        return "ERROR: el registro de fuentes no está disponible en esta ejecución."
    key = args.get("fuente")
    if key:
        return ledger.cite(key)
    text = ledger.render(limit=int(args.get("limite") or 20))
    if not text:
        return ("No has consultado ninguna fuente todavía. Usa web_search o fetch_url "
                "antes de citar nada.")
    return text


def _h_cerebro_buscar(args: dict, ctx: ToolContext) -> str:
    if ctx.cerebro is None:
        return "ERROR: el cerebro local no está disponible en esta ejecución."
    consulta = args.get("consulta") or args.get("query")
    if not consulta:
        return "ERROR: falta 'consulta'."
    # Tope duro de resultados: medido, un 1.5B pidió limite=100 y eso disparó el prompt
    # del paso siguiente a 2.638 tokens (26 s solo de evaluación en CPU).
    try:
        limite = int(args.get("limite") or 6)
    except (TypeError, ValueError):
        limite = 6
    return ctx.cerebro.buscar(str(consulta), max(1, min(limite, 10)))


def _h_cerebro_impacto(args: dict, ctx: ToolContext) -> str:
    if ctx.cerebro is None:
        return "ERROR: el cerebro local no está disponible en esta ejecución."
    simbolo = args.get("simbolo") or args.get("symbol")
    if not simbolo:
        return "ERROR: falta 'simbolo'."
    return ctx.cerebro.impacto(str(simbolo))


def _cerebro_simbolo(args: dict) -> str:
    return str(args.get("simbolo") or args.get("symbol") or "").strip()


def _h_cerebro_defs(args: dict, ctx: ToolContext) -> str:
    """Dónde está definido un símbolo. El índice exige el nombre exacto."""
    if ctx.cerebro is None:
        return "ERROR: el cerebro local no está disponible en esta ejecución."
    simbolo = _cerebro_simbolo(args)
    if not simbolo:
        return "ERROR: falta 'simbolo'."
    return ctx.cerebro.definiciones(simbolo)


def _h_cerebro_callers(args: dict, ctx: ToolContext) -> str:
    """Quién usa un símbolo, en un salto (para el árbol: cerebro_impacto)."""
    if ctx.cerebro is None:
        return "ERROR: el cerebro local no está disponible en esta ejecución."
    simbolo = _cerebro_simbolo(args)
    if not simbolo:
        return "ERROR: falta 'simbolo'."
    return ctx.cerebro.llamadores(simbolo)


def _h_plan_update(args: dict, ctx: ToolContext) -> str:
    if ctx.plan is None:
        return "ERROR: no hay ningún plan activo en esta ejecución."
    key = args.get("index", args.get("id"))
    if key is None:
        return "ERROR: falta 'index' (el número del paso entre corchetes)."
    # Se admite "1,2,3" o [1,2,3]: marcar varios a la vez ahorra turnos.
    marked = ctx.plan.mark_many(key, args.get("status") or "done")
    if not marked:
        available = ", ".join(s.id for s in ctx.plan.steps)
        return f"ERROR: no existe ningún paso '{key}'. Pasos del plan: {available}."
    detail = ", ".join(f"[{s.id}]" for s in marked)
    return f"marcado(s) {detail} como {marked[0].status}. {ctx.plan.status_line()}"


def _h_subagent(args: dict, ctx: ToolContext) -> str:
    task = args.get("task")
    if not task:
        return "ERROR: falta 'task' (la instrucción concreta para el subagente)."
    if ctx.spawn is None:
        return "ERROR: no se pueden crear subagentes desde aquí (profundidad máxima 1)."
    return ctx.spawn(str(task))


def _h_remember(args: dict, ctx: ToolContext) -> str:
    note = args.get("note")
    if not note:
        return "ERROR: falta 'note'."
    if ctx.memory is None:
        return "ERROR: memoria no inicializada."
    ctx.memory.append(str(note))
    return f"guardado en memoria: {str(note)[:80]}"


# --------------------------------------------------------------------------
# Registro
# --------------------------------------------------------------------------
def build_registry() -> dict[str, Tool]:
    tools = [        Tool(
            "read_file",
            "Lee un fichero de texto. No sirve para carpetas.",
            _schema({"path": {"type": "string"}, "offset": {"type": "integer"},
                     "limit": {"type": "integer"}}, ["path"]),
            _h_read_file,
        ),
        Tool(
            "write_file",
            "Escribe un FICHERO de texto (crea carpetas intermedias). Para CARPETA usa mkdir.",
            _schema({"path": {"type": "string"}, "content": {"type": "string"}},
                    ["path", "content"]),
            _h_write_file,
            mutating=True,
        ),
        Tool(
            "edit_file",
            "Reemplaza un texto exacto en un fichero. Lee antes con read_file.",
            _schema({"path": {"type": "string"}, "old": {"type": "string"},
                     "new": {"type": "string"}, "all": {"type": "boolean"}},
                    ["path", "old", "new"]),
            _h_edit_file,
            mutating=True,
        ),
        Tool(
            "mkdir",
            "Crea una CARPETA. Para un FICHERO usa write_file.",
            _schema({"path": {"type": "string"}}, ["path"]),
            _h_mkdir,
            mutating=True,
        ),
        Tool(
            "list_dir",
            "Lista el contenido de una carpeta.",
            _schema({"path": {"type": "string"}}, ["path"]),
            _h_list_dir,
        ),
        Tool(
            "glob",
            "Busca ficheros por patrón glob (p. ej. '**/*.py').",
            _schema({"pattern": {"type": "string"}}, ["pattern"]),
            _h_glob,
        ),
        Tool(
            "grep",
            "Busca una regex en el contenido de los ficheros.",
            _schema({"pattern": {"type": "string"}, "path": {"type": "string"},
                     "glob": {"type": "string"}}, ["pattern"]),
            _h_grep,
        ),
        Tool(
            "bash",
            "Ejecuta un comando de shell en el workspace (código, tests, git).",
            _schema({"command": {"type": "string"}, "timeout": {"type": "integer"}},
                    ["command"]),
            _h_bash,
            mutating=True,
        ),
        Tool(
            "fetch_url",
            "Lee una página web y devuelve su texto (usa una URL de web_search).",
            _schema({"url": {"type": "string"}, "render": {"type": "boolean"}}, ["url"]),
            _h_fetch_url,
        ),
        Tool(
            "web_search",
            "Busca en internet. ÚSALO si no sabes algo o necesitas documentación actual.",
            _schema({"query": {"type": "string"}, "limit": {"type": "integer"}},
                    ["query"]),
            _h_web_search,
            aliases={"q": "query"},
        ),
        Tool(
            "wiki",
            "Resumen de Wikipedia sobre un tema (rápido para «¿qué es X?»).",
            _schema({"topic": {"type": "string"}, "lang": {"type": "string"}}, ["topic"]),
            _h_wiki,
        ),
        Tool(
            "cerebro_buscar",
            "Busca en el índice LOCAL del workspace. Usa 3 keywords exactas, no frases.",
            _schema({"consulta": {"type": "string"}, "limite": {"type": "integer"}},
                    ["consulta"]),
            _h_cerebro_buscar,
            aliases={"query": "consulta"},
        ),
        Tool(
            "cerebro_impacto",
            "¿Qué rompe si cambio este símbolo? Árbol completo. Antes de modificar código.",
            _schema({"simbolo": {"type": "string"}}, ["simbolo"]),
            _h_cerebro_impacto,
            aliases={"symbol": "simbolo"},
        ),
        Tool(
            "cerebro_defs",
            "Dónde está DEFINIDO un símbolo (fichero y línea). Nombre exacto, no una frase.",
            _schema({"simbolo": {"type": "string"}}, ["simbolo"]),
            _h_cerebro_defs,
            aliases={"symbol": "simbolo"},
        ),
        Tool(
            "cerebro_callers",
            "Qué definiciones USAN un símbolo, en un salto (directo, no transitivo).",
            _schema({"simbolo": {"type": "string"}}, ["simbolo"]),
            _h_cerebro_callers,
            aliases={"symbol": "simbolo"},
        ),
        Tool(
            "fuentes",
            "Lista las fuentes web que has consultado, numeradas, para citarlas [n].",
            _schema({"fuente": {"type": "string"}, "limite": {"type": "integer"}},
                    []),
            _h_fuentes,
            aliases={"source": "fuente", "index": "fuente"},
        ),
        Tool(
            "remember",
            "Guarda un dato duradero entre sesiones.",
            _schema({"note": {"type": "string"}}, ["note"]),
            _h_remember,
            mutating=True,
        ),
        Tool(
            "plan_update",
            "Marca pasos del plan: index admite '1,2,3'. status: 'done' o 'doing'.",
            _schema({"index": {"type": "string"}, "status": {"type": "string"}},
                    ["index"]),
            _h_plan_update,
        ),
        Tool(
            "subagent",
            "Delega una subtarea autocontenida en un contexto aislado; devuelve un informe.",
            _schema({"task": {"type": "string"}}, ["task"]),
            _h_subagent,
        ),
    ]
    return {t.name: t for t in tools}


class Registry:
    def __init__(self, ctx: ToolContext, only: set[str] | None = None, extra: dict[str, Tool] | None = None):
        self.ctx = ctx
        self.tools = build_registry()
        self.tools.update(extra or {})
        # Solo se ofrece lo que existe en este contexto: no se le da al modelo una
        # herramienta que devolvería "no hay plan / no hay subagentes".
        if ctx.plan is None:
            self.tools.pop("plan_update", None)
        if ctx.spawn is None:
            self.tools.pop("subagent", None)
        if ctx.cerebro is None or not getattr(ctx.cerebro, "disponible", lambda: False)():
            for name in ("cerebro_buscar", "cerebro_impacto", "cerebro_defs",
                         "cerebro_callers"):
                self.tools.pop(name, None)
        if getattr(ctx, "sources", None) is None:
            self.tools.pop("fuentes", None)
        if not ctx.web:
            for name in ("web_search", "wiki", "fetch_url"):
                self.tools.pop(name, None)
        # `only` recorta aún más: el bucle elige el grupo de la tarea para no enviar al
        # modelo herramientas que no puede necesitar (cada token de prompt ~12,3 ms).
        if only is not None:
            self.tools = {name: tool for name, tool in self.tools.items() if name in only}

    def param_order(self) -> dict[str, list[str]]:
        """Nombre de herramienta -> parámetros obligatorios en orden.

        Lo usa el parser de texto libre: algunos modelos pequeños escriben
        `write_file ruta "contenido"` (argumentos posicionales) en vez de JSON.
        """
        return {
            name: list(tool.parameters.get("required", []))
            for name, tool in self.tools.items()
        }

    def schemas(self) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in self.tools.values()
        ]

    def run(self, name: str, args: dict) -> ToolResult:
        content, error = self._run(name, args)
        return ToolResult(content, not error, truncated='[recortado' in content or '[hay más' in content,
                          artifacts=[str(args['path'])] if not error and name in ('write_file', 'edit_file', 'mkdir') else [])

    def _run(self, name: str, args: dict) -> tuple[str, bool]:
        """Ejecuta y devuelve (observación, fue_error). Nunca lanza."""
        tool = self.tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self.tools))
            return f"ERROR: herramienta '{name}' no existe. Disponibles: {known}", True
        if not isinstance(args, dict):
            return f"ERROR: los argumentos de {name} deben ser un objeto JSON.", True
        if tool.aliases:
            args = dict(args)
            for alias, canonical in tool.aliases.items():
                if alias in args and canonical not in args:
                    args[canonical] = args[alias]
        missing = [k for k in tool.parameters.get("required", []) if k not in args]
        if missing:
            return (f"ERROR: faltan parámetros obligatorios {missing} en {name}. "
                    f"Argumentos recibidos: {json.dumps(args, ensure_ascii=False)}"), True
        try:
            for key, spec in tool.parameters.get('properties', {}).items():
                if key not in args:
                    continue
                value = args[key]
                kind = spec.get('type')
                if name.startswith('mcp_'):
                    continue  # recursive validation below handles external schemas
                valid = ((kind == 'string' and isinstance(value, str)) or
                         (kind == 'integer' and isinstance(value, int) and not isinstance(value, bool)) or
                         (kind == 'boolean' and isinstance(value, bool)) or kind is None)
                # plan_update historically accepts integer IDs and lists.
                if name == 'plan_update' and key == 'index':
                    valid = isinstance(value, (str, int, list))
                if not valid:
                    raise ValueError(f"parámetro {key}: se esperaba {kind}")
                if kind == 'integer' and value < 1:
                    raise ValueError(f"parámetro {key}: debe ser positivo")
            if name.startswith('mcp_'):
                validate(args, tool.parameters)
            out = tool.handler(args, self.ctx)
        except PermissionError as exc:
            return f"ERROR: {exc}", True
        except ValueError as exc:
            # Mensaje limpio, sin el prefijo `ValueError:` que confunde al modelo.
            return f"ERROR: {exc}", True
        except FileNotFoundError as exc:
            return f"ERROR: no existe el fichero {exc.filename}", True
        except IsADirectoryError:
            return "ERROR: la ruta es una carpeta, no un fichero.", True
        except Exception as exc:  # noqa: BLE001 - el modelo debe ver cualquier fallo
            return f"ERROR: {type(exc).__name__}: {exc}", True
        if isinstance(out, ToolResult):
            return out.content, not out.ok
        text = str(out)
        return text, text.startswith("ERROR")
