"""Task Graph: planificación previa con dependencias y prioridades.

Traduce la caja «Task Graph» de la capa Knowledge del diagrama a algo que un modelo
de 1.5B aguanta. Tres decisiones importantes:

1. **Se pide una sola vez, antes del bucle.** Una llamada extra al modelo produce el
   plan; si el modelo devuelve algo imparseable, **no hay plan y el bucle sigue
   igual** (degradación limpia: la planificación nunca rompe una ejecución).
2. **El grafo se sanea a la fuerza.** Un 1.5B inventa ids, dependencias hacia
   adelante y ciclos. Aquí las dependencias desconocidas se descartan y los ciclos se
   rompen, así que siempre existe un orden topológico válido.
3. **La estructura va al prefijo congelado; el estado NO.** Los ids, los textos y las
   dependencias son inmutables durante la ejecución, así que la caché de prefijo de
   Ollama sigue siendo válida. El progreso (qué pasos están hechos) viaja como
   observación de la herramienta `plan_update`, nunca dentro del prefijo.

Sobre las dependencias: se usan para **ordenar** la lista y para saber cuál es el
siguiente paso pendiente, pero **no bloquean** las herramientas del modelo. Bloquear
acciones exige una fiabilidad que un 1.5B no tiene; el orden se le muestra y el
harness le recuerda lo que falta.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .providers.llm import iter_json_objects

# OJO: el texto lleva llaves (el ejemplo JSON) y la tarea puede llevar cualquier cosa
# (variables de shell, llaves de plantilla). Por eso NO se usa str.format: se
# sustituye un centinela y la tarea se concatena al final, sin interpolar nada.
PLAN_INSTRUCTIONS = """Divide la tarea en pasos concretos y ejecutables.

Responde SOLO con un objeto JSON, sin texto alrededor, con esta forma exacta:
{"tasks": [{"id": "1", "task": "crear la carpeta informes", "depends_on": []}, {"id": "2", "task": "escribir informes/resumen.md", "depends_on": ["1"]}]}

Reglas:
- Entre 2 y __MAX_STEPS__ pasos.
- Cada paso empieza por un verbo y describe UNA acción concreta.
- "depends_on" solo puede citar ids de pasos ANTERIORES; usa [] en el primero.
- No incluyas pasos abstractos como "pensar" o "analizar la tarea".
- Usa EXACTAMENTE los nombres de fichero, carpetas y rutas de la TAREA: no los
  traduzcas, no los cambies de sitio y no los renombres.
- NO añadas pasos para crear carpetas, ficheros ni nada que la TAREA no mencione. Los
  nombres del ejemplo de arriba son solo eso, un EJEMPLO: los tuyos salen de la TAREA.

TAREA:
"""

_MAX_STEPS_SENTINEL = "__MAX_STEPS__"


def plan_prompt(task: str, max_steps: int) -> str:
    """Prompt de planificación. La tarea se concatena, nunca se interpola."""
    return PLAN_INSTRUCTIONS.replace(_MAX_STEPS_SENTINEL, str(max_steps)) + task

_STATUS_ALIASES = {
    "pending": "pending", "pendiente": "pending", "todo": "pending",
    "doing": "doing", "en curso": "doing", "empezado": "doing", "in progress": "doing",
    "done": "done", "hecho": "done", "completado": "done", "completed": "done",
    "ok": "done", "terminado": "done", "listo": "done",
}
_STATUS_MARKS = {"pending": " ", "doing": "~", "done": "x"}

# Nombres de fichero que aparecen en un texto (`notas.txt`, `src/main.py`). La extensión
# empieza por letra para no confundir versiones (`python3.11`) con ficheros.
_FILE_RX = re.compile(r"[\w-]+\.[A-Za-z][A-Za-z0-9]{0,5}\b")

_CHECKLIST_RX = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+(.+?)\s*$")
_ID_KEYS = ("id", "index", "step", "n", "num")
_TEXT_KEYS = ("task", "desc", "description", "text", "title", "action", "name")
_DEP_KEYS = ("depends_on", "dependencies", "deps", "after", "requires", "needs")
_LIST_KEYS = ("tasks", "steps", "plan", "todo", "items")


@dataclass
class PlanStep:
    id: str
    text: str
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"

    def __str__(self) -> str:
        return f"[{self.id}] {self.text}"


class Plan:
    def __init__(self, steps: list[PlanStep]):
        self.steps = steps

    # -- consultas ---------------------------------------------------------
    def order(self) -> list[PlanStep]:
        """Orden topológico estable; los ciclos residuales se liberan al vuelo."""
        done: set[str] = set()
        out: list[PlanStep] = []
        pool = list(self.steps)
        while pool:
            chosen = next(
                (s for s in pool if set(s.depends_on) <= done), None
            )
            if chosen is None:  # ciclo que sobrevivió al saneo: se rompe aquí
                chosen = pool[0]
                chosen.depends_on = []
            pool.remove(chosen)
            out.append(chosen)
            done.add(chosen.id)
        return out

    def pending(self) -> list[PlanStep]:
        return [s for s in self.order() if s.status != "done"]

    def done(self) -> list[PlanStep]:
        return [s for s in self.order() if s.status == "done"]

    def next_step(self) -> PlanStep | None:
        """Primer paso pendiente cuyas dependencias ya están hechas."""
        finished = {s.id for s in self.steps if s.status == "done"}
        for step in self.order():
            if step.status == "done":
                continue
            if set(step.depends_on) <= finished:
                return step
        return self.pending()[0] if self.pending() else None

    # -- mutación ----------------------------------------------------------
    def mark_many(self, keys: object, status: str) -> list[PlanStep]:
        """Marca varios pasos de una vez.

        Un 1.5B tiende a marcar un paso por turno, aunque haya hecho tres: aceptar
        "1,2,3" o [1, 2, 3] le ahorra turnos y evita que el plan quede desincronizado.
        """
        if isinstance(keys, (list, tuple, set)):
            raw = [str(k) for k in keys]
        else:
            raw = re.split(r"[,\s;]+", str(keys))
        marked: list[PlanStep] = []
        for key in raw:
            if not str(key).strip():
                continue
            step = self.mark(key, status)
            if step is not None and step not in marked:
                marked.append(step)
        return marked

    def mark(self, key: object, status: str) -> PlanStep | None:
        """Marca por id o por posición (1-based): el modelo usa las dos formas."""
        if key is None:
            return None
        raw = str(key).strip().strip("[]")
        target = next((s for s in self.steps if s.id == raw), None)
        if target is None:
            try:
                position = int(raw) - 1
            except (TypeError, ValueError):
                return None
            if 0 <= position < len(self.steps):
                target = self.steps[position]
        if target is None:
            return None
        target.status = _STATUS_ALIASES.get(str(status).strip().lower(), "done")
        return target

    # -- presentación ------------------------------------------------------
    def render(self) -> str:
        """Estructura estática: es lo único que entra en el prefijo congelado."""
        lines = ["PLAN DE TRABAJO (task graph)"]
        for step in self.order():
            deps = f"  (depende de: {', '.join(step.depends_on)})" if step.depends_on else ""
            lines.append(f"  [{step.id}] {step.text}{deps}")
        lines.append(
            "Sigue este orden. Marca el avance con plan_update(index, status) y no "
            "termines mientras queden pasos pendientes."
        )
        return "\n".join(lines)

    def display(self) -> str:
        """Con estados, para la consola."""
        return "\n".join(
            f"  {_STATUS_MARKS.get(s.status, ' ')} [{s.id}] {s.text}"
            + (f"  (depende de: {', '.join(s.depends_on)})" if s.depends_on else "")
            for s in self.order()
        )

    def status_line(self) -> str:
        """Resumen volátil y corto (observaciones y avisos)."""
        hechos = [s.id for s in self.done()]
        pendientes = [s.id for s in self.pending()]
        line = f"plan: {len(hechos)}/{len(self.steps)} hechos"
        if not pendientes:
            return line + "; completo"
        nxt = self.next_step()
        line += f"; pendientes: {', '.join(pendientes)}"
        if nxt:
            line += f"; siguiente: [{nxt.id}] {nxt.text}"
        return line

    def empty(self) -> bool:
        return not self.steps

    def to_dict(self) -> dict:
        return {
            "steps": [
                {"id": s.id, "task": s.text, "depends_on": s.depends_on, "status": s.status}
                for s in self.steps
            ]
        }


# --------------------------------------------------------------------------
# Parseo tolerante
# --------------------------------------------------------------------------
def _coerce_list(raw: object) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in _LIST_KEYS:
            if isinstance(raw.get(key), list):
                return raw[key]
        return [raw]  # un único paso suelto
    return []


def _text_of(item: object) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    for key in _TEXT_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _deps_of(item: object) -> list[str]:
    if not isinstance(item, dict):
        return []
    for key in _DEP_KEYS:
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, (str, int)):
            return [str(value)]
        if isinstance(value, list):
            return [str(v) for v in value if isinstance(v, (str, int))]
    return []


def _id_of(item: object, fallback: str) -> str:
    if isinstance(item, dict):
        for key in _ID_KEYS:
            value = item.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
    return fallback


def _break_cycles(steps: list[PlanStep]) -> None:
    """Kahn: lo que no se puede emitir está en un ciclo y se libera."""
    pending = {s.id: set(s.depends_on) for s in steps}
    emitted: set[str] = set()
    progress = True
    while progress:
        progress = False
        for sid, deps in list(pending.items()):
            if sid not in emitted and deps <= emitted:
                emitted.add(sid)
                progress = True
    for step in steps:
        if step.id not in emitted:
            step.depends_on = []


def _sanitize(raw_steps: list, max_steps: int) -> list[PlanStep]:
    entries: list[tuple[object, PlanStep]] = []
    seen: set[str] = set()
    for item in raw_steps:
        text = _text_of(item)
        if not text:
            continue
        sid = _id_of(item, str(len(entries) + 1))
        if sid in seen:
            sid = str(len(entries) + 1)
        seen.add(sid)
        entries.append((item, PlanStep(id=sid, text=text)))
        if len(entries) >= max_steps:
            break

    known = {step.id for _, step in entries}
    for item, step in entries:
        step.depends_on = [
            dep for dep in _deps_of(item) if dep in known and dep != step.id
        ]
    steps = [step for _, step in entries]
    _break_cycles(steps)
    return steps


def _file_stems(text: str) -> dict[str, set[str]]:
    """Nombres de fichero de un texto, agrupados por extensión y sin ella."""
    out: dict[str, set[str]] = {}
    for match in _FILE_RX.finditer(text or ""):
        stem, _, ext = match.group(0).lower().rpartition(".")
        if stem:
            out.setdefault(ext, set()).add(stem)
    return out


def contradicts_task(task: str, plan: Plan) -> str:
    """¿El plan se refiere a ficheros distintos de los que pidió la tarea?

    Medido: para «escribe en notas.txt el texto hola» el planificador devolvió «crear la
    carpeta demo» y «escribir demo/notes.txt», y el modelo ejecutó ese plan en vez de la
    tarea. El harness no lo detectaba porque verifica el artefacto del plan.

    Un nombre **derivado** del de la tarea es legítimo (`calculadora` ->
    `test_calculadora`); otro nombre con la MISMA extensión y sin relación es otro
    fichero, y eso es lo que rechazamos. Devuelve el motivo, o "" si el plan respeta la
    tarea (o si la tarea no nombra ningún fichero: entonces no hay nada que comprobar).
    """
    wanted = _file_stems(task)
    if not wanted:
        return ""
    for ext, stems in _file_stems(" ".join(s.text for s in plan.steps)).items():
        same_ext = wanted.get(ext)
        if not same_ext:
            continue
        for stem in sorted(stems):
            if stem in same_ext:
                continue
            if any(stem in known or known in stem for known in same_ext):
                continue  # derivado de lo pedido: vale
            pedidos = ", ".join(f"{s}.{ext}" for s in sorted(same_ext))
            return f"el plan habla de '{stem}.{ext}' y la tarea pide '{pedidos}'"
    return ""


def parse_plan(text: str, max_steps: int = 6) -> Plan | None:
    """Extrae un plan del texto del modelo; devuelve None si no hay nada usable."""
    if not text:
        return None

    # Un plan de un solo paso no aporta nada y además dispara un aviso inútil de
    # "te quedan pasos": mejor seguir sin plan. Mismo criterio que el fallback.
    for obj in iter_json_objects(text):
        parsed_steps = _sanitize(_coerce_list(obj), max_steps)
        if len(parsed_steps) >= 2:
            return Plan(parsed_steps)

    # Reserva: lista numerada o con viñetas, línea a línea.
    steps: list[PlanStep] = []
    for line in text.splitlines():
        match = _CHECKLIST_RX.match(line)
        if not match:
            continue
        body = match.group(1).strip()
        if not body:
            continue
        steps.append(PlanStep(id=str(len(steps) + 1), text=body))
        if len(steps) >= max_steps:
            break
    if len(steps) >= 2:
        return Plan(steps)
    return None
