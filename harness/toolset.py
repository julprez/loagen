"""Selección de herramientas por tarea: menos esquemas enviados, menos latencia.

Por qué existe este módulo, con la medición delante: en CPU la latencia la manda el
**prompt**, no la generación (`prompt eval = 12,3 ms/token` en el log de Ollama frente a
~11,6 tok/s generando). Cada herramienta declarada son ~65-100 tokens de esquema: con 19
herramientas el bloque son ~1.256 tokens y con 14 son ~929. Ollama reutiliza el prefijo,
así que el ahorro se concentra en el arranque de la sesión (y cada vez que el prefijo
cambia), no en cada turno. Medido en dos ejecuciones seguidas de la MISMA tarea: paso 1
1702 -> 1422 tokens (27,9 s -> 20,8 s) y paso 2 1920 -> 1640 (15,2 s -> 12,2 s). Y no es
solo tiempo: cuanto más largo es el catálogo, más probable es que un 1.5B elija la
herramienta equivocada.

Dos decisiones que evitan que esto rompa tareas:

* **Todo lo declarado se puede usar.** El perfil se aplica *antes* de construir el
  registro (`Registry(ctx, only=...)`), así que lo que no se envía no existe para el
  modelo: no hay ninguna herramienta declarada que devuelva «no existe».
* **Nunca recorta a ciegas.** Si la tarea no encaja en ningún perfil, se envía el
  catálogo completo. Recortar por defecto ahorraría tokens y arruinaría tareas; la
  selección es una optimización, no una apuesta.

El clasificador es determinista (regex sobre la tarea), no una llamada extra al modelo:
una llamada de clasificación costaría más segundos que los que ahorra.
"""

from __future__ import annotations

import re

# El conjunto irreducible: sin esto el agente no puede ni mirar el workspace.
CORE = {"read_file", "write_file", "edit_file", "mkdir", "list_dir", "glob", "grep",
        "bash"}

# Coordinación: si hay plan o subagentes, se envían siempre (ocupan poco y evitan que
# el modelo termine con pasos pendientes por no tener la herramienta delante).
COORD = {"plan_update", "subagent"}

# Perfiles por tipo de tarea. Los `extra` se suman al núcleo y a la coordinación.
PROFILES: dict[str, set[str]] = {
    # Mirar y editar código de este workspace. El cerebro es su mejor herramienta.
    "code": {"cerebro_buscar", "cerebro_defs", "cerebro_callers", "cerebro_impacto"},
    # Buscar en internet y citar: navegador + registro de fuentes.
    "research": {"web_search", "wiki", "fetch_url", "fuentes"},
    # Crear/editar ficheros de texto sin tocar código.
    "docs": {"web_search", "wiki", "fetch_url", "fuentes", "remember"},
    # Ejecutar, desplegar, servicios, logs.
    "ops": {"bash", "cerebro_buscar", "remember"},
    # Memoria entre sesiones.
    "memory": {"remember"},
}

_ORDER = ("research", "ops", "code", "docs", "memory")

# Tarea de ejemplo por perfil: la usa `--tools` para mostrar cuántas herramientas se
# enviarían de verdad, y los tests como contrato de clasificación.
EXAMPLES: dict[str, str] = {
    "code": "refactoriza la función load_config y mira quién la llama",
    "research": "busca en internet la última versión de Python y cita las fuentes web",
    "docs": "escribe un README con la guía de instalación del proyecto",
    "ops": "ejecuta los tests y reinicia el servicio si fallan",
    "memory": "recuerda que este proyecto usa Go para el backend",
}

# Señales léxicas. Se aceptan acentos y variantes; se comparan sobre la tarea en
# minúsculas, sin acentos, para que «deplegar»/«desplegar» caigan igual.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "research": (
        "busca", "investiga", "documenta", "ultima version", "novedad", "noticia",
        "web", "internet", "google", "wikipedia", "cita", "fuente", "precio",
        "compara", "enlace", "url", "search", "pregunta a internet",
    ),
    "ops": (
        "ejecuta", "test", "build", "compila", "despliega", "deploy", "reinicia",
        "restart", "servicio", "systemd", "journalctl", "docker", "log", "instala",
        "install", "comando", "shell", "script", "proceso", "git", "commit",
    ),
    "code": (
        "codigo", "funcion", "clase", "metodo", "refactor", "bug", "test unitario",
        "llama", "simbolo", "implementa", "modulo", "endpoint", "handler", "import",
        "python", "svelte", "typescript", "sql", "repo", "fichero", "archivo",
        "renombra", "anad", "borra",
    ),
    "docs": (
        "escrib", "redacta", "readme", "guia", "nota", "apunte", "texto", "resume",
        "informe", "manual", "correo", "articulo", "guion", "borrador", "traduc",
    ),
    "memory": (
        "recuerda", "record", "memoria", "apunta", "guarda este dato",
        "para la proxima", "no olvides",
    ),
}

_ACCENTS = str.maketrans("áéíóúüñ", "aeiouun")

# Las palabras clave casan por **prefijo de palabra**, no por subcadena: «refactor»
# cubre «refactoriza» y «busca» cubre «buscando», pero «go» NO casa dentro de «google»
# ni «nota» dentro de «anotación». Sin esa frontera inicial el clasificador se
# dispararía con texto que no habla de lo que cree.
_KEYWORD_RX: dict[str, re.Pattern] = {}


def _has_keyword(text: str, word: str) -> bool:
    rx = _KEYWORD_RX.get(word)
    if rx is None:
        rx = re.compile(rf"\b{re.escape(_normalize(word))}\w*")
        _KEYWORD_RX[word] = rx
    return rx.search(text) is not None
_FILE_HINTS = re.compile(
    r"\.(go|py|js|ts|svelte|rs|c|cpp|h|hpp|java|rb|php|sh|toml|json|yaml|yml|md|txt|html|css)\b",
    re.I,
)
_CODE_HINTS = re.compile(
    r"\b(func|def|class|struct|interface|package|import|var|const|return|async|await)\b"
)


def _normalize(text: str) -> str:
    return (text or "").lower().translate(_ACCENTS)


# Un acierto único solo decide si el perfil es inequívoco: «busca», «ejecuta» o
# «recuerda» no aparecen por casualidad. «código» y «escribe» sí, así que esos dos
# exigen dos aciertos para decidir.
_STRONG_ALONE = ("research", "ops", "memory")


def classify(task: str, max_hits: int = 2) -> str | None:
    """Perfil (o perfiles unidos con `+`) para esta tarea, o None si no está claro.

    Devuelve None —y por tanto catálogo completo— cuando no hay ninguna señal, cuando
    hay un empate entre perfiles débiles o cuando no caben en `max_hits`. Es
    deliberado: elegir mal cuesta más que no elegir.
    """
    text = _normalize(task)
    if not text.strip():
        return None
    scores: dict[str, int] = {}
    for name, words in _KEYWORDS.items():
        hits = sum(1 for word in words if _has_keyword(text, word))
        if hits:
            scores[name] = hits
    if not scores:
        # Sin palabras clave, dos señales estructurales: extensión de fichero o
        # sintaxis de código en la propia tarea.
        if _FILE_HINTS.search(text) or _CODE_HINTS.search(text):
            return "code"
        return None
    # Un perfil entra si tiene señal fuerte (>=2 aciertos), o si es el ÚNICO perfil con
    # aciertos y ese perfil es inequívoco por sí solo.
    winners = [name for name, score in scores.items() if score >= 2]
    if not winners:
        if len(scores) != 1:
            return None
        only = next(iter(scores))
        if only not in _STRONG_ALONE:
            return None
        winners = [only]
    winners.sort(key=lambda name: (-scores[name], _ORDER.index(name)))
    if len(winners) > max_hits:
        return None
    return "+".join(sorted(winners, key=_ORDER.index))


def selection(task: str, available: set[str], all_names: set[str]) -> set[str] | None:
    """Conjunto de herramientas a enviar, o None para enviarlas todas.

    `available` es lo que el contexto permite de verdad (sin plan, sin cerebro, sin
    web); `all_names` es el catálogo completo antes de recortar.
    """
    profile = classify(task)
    if profile is None:
        return None
    selected = set(CORE)
    for name in profile.split("+"):
        selected |= PROFILES.get(name, set())
    selected |= COORD
    # Nunca se recorta por debajo de un catálogo ya pequeño: si el contexto ha quitado
    # mucho (sin cerebro, sin web), la selección apenas ahorra y sí añade riesgo.
    selected &= available & all_names
    if len(selected) >= len(all_names) or len(selected) < 4:
        return None
    if len(all_names) <= 6:
        return None
    return selected


def groups(task: str) -> set[str]:
    """Nombre legible del grupo elegido (para la traza y el resumen)."""
    profile = classify(task)
    return set(profile.split("+")) if profile else {"completo"}