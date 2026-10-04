"""Puesta en marcha: asistente de configuración (`--init`) y diagnóstico (`--doctor`).

La idea es que nadie tenga que leer el código ni adivinar valores: todo se **detecta**
(qué modelos hay en Ollama, si el workspace se puede escribir, dónde está el cerebro, si
hay Playwright, qué shell usar) y lo que no se puede detectar sale de los valores ya
medidos en este proyecto, no de ocurrencias.

Solo usa la biblioteca estándar y `harness/config.py` + `harness/llm.py`, para que
`--init` funcione incluso con Ollama caído (escribe igualmente un `rules.toml` válido).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from dataclasses import dataclass

from .config import (
    DEFAULT_HOST,
    DEFAULT_MODEL,
    MIN_PYTHON,
    TOML_AVAILABLE,
    default_cerebro_root,
    default_config_path,
)

# Niveles del diagnóstico: `error` impide una ejecución normal, `aviso` solo la degrada.
OK = "ok"
INFO = "info"
AVISO = "aviso"
ERROR = "error"


@dataclass
class Check:
    """Una línea del diagnóstico: nivel, etiqueta y explicación."""

    level: str
    label: str
    detail: str


# --------------------------------------------------------------------------
# Detección
# --------------------------------------------------------------------------
def detect_ollama(host: str, timeout: int = 5) -> tuple[list[str], str]:
    """Modelos instalados en Ollama. Devuelve ([], error) si no responde.

    Con Ollama caído el asistente sigue siendo útil: escribe la config con el modelo por
    defecto y lo avisa, en vez de abortar.
    """
    from .providers.llm import Ollama, OllamaError

    try:
        return Ollama(host=host or DEFAULT_HOST, model=DEFAULT_MODEL,
                      timeout=timeout).list_models(), ""
    except OllamaError as exc:
        return [], str(exc)


def detect_render() -> bool:
    """¿Hay Playwright para renderizar páginas con JavaScript?"""
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


def detect_shell() -> str:
    """Shell sugerido: vacío en POSIX (vale el del sistema), Git Bash en Windows."""
    if os.name != "nt":
        return ""
    for name in ("bash", "bash.exe"):
        found = shutil.which(name)
        if found:
            return found.replace("\\", "/")
    return ""


def environment(host: str, workspace: str) -> dict:
    """Foto del entorno, sin suposiciones y sin lanzar excepciones."""
    root = os.path.abspath(workspace or os.getcwd())
    cerebro = default_cerebro_root(root)
    models, error = detect_ollama(host)
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}."
                  f"{sys.version_info.micro}",
        "tomllib": TOML_AVAILABLE,
        "host": host or DEFAULT_HOST,
        "models": models,
        "ollama_error": error,
        "workspace": root,
        "workspace_writable": os.path.isdir(root) and os.access(root, os.W_OK),
        "cerebro_root": cerebro,
        "render": detect_render(),
        "shell": detect_shell(),
        "rules": default_config_path(root) or "",
    }


def pick_model(models: list[str], preferred: str) -> tuple[str, str]:
    """Elige el modelo a escribir en la config. Devuelve (modelo, nota)."""
    if not models:
        return preferred, "no se pudo consultar Ollama: se deja el modelo por defecto"
    if preferred in models:
        return preferred, "instalado"
    # Ollama puede devolver `qwen2.5:1.5b` para `qwen2.5:1.5b-instruct`? No: es exacto,
    # pero sí distingue mayúsculas. Se busca por nombre base antes de cambiar de modelo.
    base = preferred.split(":")[0]
    for name in models:
        if name.split(":")[0] == base:
            return name, f"se ajustó a '{name}' (instalado)"
    return models[0], f"'{preferred}' no está instalado; se usa '{models[0]}'"


# --------------------------------------------------------------------------
# Generación de `rules.toml`
# --------------------------------------------------------------------------
def render_rules(env: dict, model: str) -> str:
    """Config lista para usar: los números salen de las mediciones de este proyecto."""
    shell = env.get("shell", "")
    cerebro = env.get("cerebro_root", "")
    lines = [
        "# rules.toml — configuración de loagen.",
        "#",
        "# Generado por `loagen --init`. Se puede borrar: los valores por defecto",
        "# están en el código. Lo que conviene revisar es `[permissions]`, que es el",
        "# contrato de seguridad del agente en esta máquina.",
        "#",
        "# LA PRIMERA REGLA QUE CASA GANA: aquí los DENY van arriba y los ALLOW debajo.",
        "# Si añades un ALLOW que deba ganar a un deny genérico, ponlo ENCIMA de ese deny.",
        "",
        "[model]",
        f'name = "{model}"',
        f'host = "{env.get("host", DEFAULT_HOST)}"',
        "temperature = 0.0",
        "num_ctx = 8192",
        "# Tope de tokens por turno. Imprescindible en CPU: sin él, un bucle de repetición",
        "# del modelo puede tardar minutos (medido: ~1.900 tokens hasta agotar el timeout).",
        "num_predict = 320",
        "timeout = 150",
        "",
        "[loop]",
        "task_timeout = 300         # segundos globales, incluidos subagentes",
        "max_tool_calls = 40        # llamadas de modelo y herramientas compartidas",
        "max_total_tokens = 24000   # tokens prompt + salida",
        "max_steps = 12              # tope de Perception->Action por tarea",
        "max_retries = 2             # reintentos guiados si el modelo se rinde tras un error",
        "tool_output_max_chars = 4000",
        "keep_last_messages = 14",
        "compress_threshold = 0.85",
        "# Task graph. DESACTIVADO POR MEDICIÓN, y esto es contraintuitivo:",
        "# misma tarea simple ('escribe en notas.txt el texto hola'), 8 ejecuciones:",
        "#   plan activado   -> 0/4 limpias",
        "#   plan desactivado -> 4/4 limpias",
        "# El planificador REESCRIBE la tarea (inventó 'crear la carpeta demo' y",
        "# 'demo/notes.txt' cuando se pidió 'notas.txt') y el modelo ejecuta el plan",
        "# inventado, no lo que pediste. Actívalo para tareas de varios pasos o con un",
        "# modelo mayor que 1.5B, y comprueba que el plan menciona tu tarea de verdad.",
        "plan = false",
        "plan_max_steps = 5",
        "plan_max_nudges = 3",
        "# Exigir el plan EMPEORA el resultado con un 1.5B (medido: 9 pasos, 88,9 s y cero",
        "# ficheros). Actívalo si usas un modelo mayor.",
        "plan_require = false",
        "subagent_model = \"\"        # \"\" = el mismo modelo que el principal",
        "subagent_max_steps = 5",
        "subagent_max_chars = 700",
        "web = true                  # navegador (web_search, wiki, fetch_url)",
        "# Cerebro local: índice de búsqueda del workspace. \"\" = autodetectado.",
        f'cerebro_root = "{_toml_path(cerebro)}"',
        "# Selección de herramientas por tarea: MENOS tokens de prompt y menos latencia",
        "# (medido: 1702 -> 1422 tokens y 27,9 s -> 20,8 s). Las tareas ambiguas reciben",
        "# el catálogo completo, así que nunca deja una tarea sin la herramienta que necesita.",
        "tool_selection = true",
        "# Intérprete de `bash`. \"\" = el del sistema (/bin/sh en Linux/macOS, cmd.exe en",
        "# Windows). En Windows, con Git Bash, aqui va \"bash\" para que funcionen los",
        "# comandos POSIX que genera el modelo y las reglas de abajo.",
        f'shell = "{_toml_path(shell)}"',
        "",
        "[permissions]",
        "# Acción para herramientas que modifican algo y no casan con ninguna regla.",
        'default = "ask"',
        "allow_outside_workspace = false",
        "",
        "# Los DENY van PRIMERO: la primera regla que casa gana. Si algún día necesitas un",
        "# ALLOW que deba ganar a un deny genérico (p. ej. `sudo systemctl restart mi-app`),",
        "# ponlo ENCIMA de ese deny, no debajo: el orden del fichero ES la precedencia.",
        "#",
        "# Nota: leer no necesita regla (la naturaleza de las herramientas de lectura ya las",
        "# permite); escribir ficheros cae en `default`.",
        "",
        "# --- DENY: gana a todo lo de abajo -------------------------------------",
        "[[permissions.rule]]",
        'tool = "bash"',
        r'match = "rm\\s+-[a-z]*[rf]\\s+(/|~|\\*)"',
        'action = "deny"',
        'reason = "borrado recursivo de raíz, home o todo"',
        "",
        "[[permissions.rule]]",
        'tool = "bash"',
        r'match = "sudo|mkfs|dd\\s+if=|shutdown|reboot|:\\(\\)\\{"',
        'action = "deny"',
        'reason = "comando destructivo o que pide privilegios"',
        "",
        "[[permissions.rule]]",
        'tool = "*"',
        r'match = "(\\.ssh|id_rsa|\\.aws/credentials|/etc/shadow|\\.npmrc|\\.pypirc)"',
        'action = "deny"',
        'reason = "credenciales o secretos"',
        "",
        "[[permissions.rule]]",
        'tool = "bash"',
        r'match = "curl[^|]*\\|\\s*(ba)?sh"',
        'action = "deny"',
        'reason = "ejecutar un script descargado de la red"',
        "",
        "# --- Shell: aprobación explícita (las tools de lectura no necesitan shell) ---",
        "# Ojo: en TOML cada barra invertida va doblada (dos en el fichero, una en la regex).",
        "[[permissions.rule]]",
        'tool = "bash"',
        r'match = "^\\s*(ls|pwd|cat|head|tail|wc|file|stat|tree|du|grep|rg|fd|which|date)\\b"',
        'action = "ask"',
        'reason = "shell libre: el prefijo no garantiza sus efectos"',
        "",
        "[[permissions.rule]]",
        'tool = "bash"',
        r'match = "^\\s*git\\s+(status|log|diff|show)\\b"',
        'action = "ask"',
        'reason = "git vía shell: requiere aprobación"',
        "",
    ]
    return "\n".join(lines) + "\n"


def _toml_path(path: str) -> str:
    """Escapa una ruta para el TOML generado (Windows usa barras invertidas)."""
    return str(path or "").replace("\\", "\\\\").replace('"', '\\"')


def write_rules(path: str, text: str, force: bool = False) -> tuple[bool, str]:
    """Escribe el `rules.toml`. No pisa uno existente salvo `force`."""
    if os.path.exists(path) and not force:
        return False, f"ya existe {path} (usa --force para sobrescribirlo)"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        return False, f"no se pudo escribir {path}: {exc}"
    return True, path


# --------------------------------------------------------------------------
# Diagnóstico
# --------------------------------------------------------------------------
def doctor(cfg, host: str | None = None) -> list[Check]:
    """Revisa lo que hace falta para ejecutar y lo que solo mejora la experiencia."""
    checks: list[Check] = []
    version = (f"{sys.version_info.major}.{sys.version_info.minor}."
               f"{sys.version_info.micro}")
    if TOML_AVAILABLE:
        checks.append(Check(OK, "Python", f"{version} (tomllib disponible)"))
    else:
        checks.append(Check(
            ERROR, "Python",
            f"{version}: falta tomllib, hace falta {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+. "
            "Se ignora rules.toml y se usan los permisos por defecto",
        ))

    if getattr(cfg, "toml_skipped", ""):
        checks.append(Check(ERROR, "Reglas",
                            f"no se pudo leer {cfg.toml_skipped}; se usan los valores "
                            "por defecto"))
    elif getattr(cfg, "rules_path", ""):
        checks.append(Check(OK, "Reglas",
                            f"{len(cfg.rules)} reglas de {cfg.rules_path}"))
    else:
        checks.append(Check(
            INFO, "Reglas",
            'sin rules.toml: todo lo que modifica algo pasa por "ask". '
            "Crea uno con `--init`",
        ))

    host = host or getattr(cfg, "host", DEFAULT_HOST)
    models, error = detect_ollama(host or DEFAULT_HOST)
    if error:
        checks.append(Check(ERROR, "Ollama", f"{error}"))
    else:
        checks.append(Check(OK, "Ollama",
                            f"{host} · {len(models)} modelo(s): "
                            f"{', '.join(models) or '(ninguno)'}"))
    if not error:
        if cfg.model in models:
            checks.append(Check(OK, "Modelo", f"{cfg.model} (instalado)"))
        else:
            base = cfg.model.split(":")[0]
            match = [m for m in models if m.split(":")[0] == base]
            if match:
                checks.append(Check(INFO, "Modelo",
                                    f"{cfg.model} no está, pero sí {match[0]} "
                                    f"(se usará `--model {match[0]}`)"))
            else:
                checks.append(Check(ERROR, "Modelo",
                                    f"'{cfg.model}' no está en Ollama. "
                                    f"Prueba: ollama pull {cfg.model}"))

    workspace = getattr(cfg, "workspace", ".")
    if os.path.isdir(workspace):
        if os.access(workspace, os.W_OK):
            checks.append(Check(OK, "Workspace", f"{workspace} (escribible)"))
        else:
            checks.append(Check(AVISO, "Workspace",
                                f"{workspace} no es escribible: solo tareas de lectura"))
    else:
        checks.append(Check(ERROR, "Workspace", f"{workspace} no existe"))

    root = getattr(cfg, "cerebro_root", "")
    if root and os.path.isfile(os.path.join(root, "cerebro.py")):
        checks.append(Check(OK, "Cerebro", root))
    else:
        checks.append(Check(
            INFO, "Cerebro",
            "no detectado: se retiran cerebro_buscar/defs/callers/impacto "
            "(el resto funciona igual)",
        ))

    if detect_render():
        checks.append(Check(OK, "Render", "Playwright disponible (páginas con JS)"))
    else:
        checks.append(Check(
            INFO, "Render",
            "sin Playwright: fetch_url devuelve el HTML estático, que casi siempre basta",
        ))

    shell = getattr(cfg, "shell", "")
    if shell:
        found = shutil.which(shell)
        if found:
            checks.append(Check(OK, "Shell", f"{shell} -> {found}"))
        else:
            checks.append(Check(ERROR, "Shell",
                                f"'{shell}' no existe: revisa `shell` en rules.toml"))
    elif os.name == "nt":
        checks.append(Check(AVISO, "Shell",
                            "cmd.exe por defecto; con Git Bash pon `shell = \"bash\"`"))
    else:
        checks.append(Check(OK, "Shell", "el del sistema (/bin/sh)"))

    return checks


def has_errors(checks: list[Check]) -> bool:
    return any(check.level == ERROR for check in checks)


def format_check(check: Check) -> str:
    return f"  [{check.level:5s}] {check.label:10s} {check.detail}"
