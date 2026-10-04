"""Configuración del harness: valores por defecto, `rules.toml` y overrides de CLI."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

DEFAULT_MODEL = "qwen2.5:1.5b-instruct"
DEFAULT_HOST = "http://127.0.0.1:11434"

# `tomllib` está en la stdlib desde Python 3.11. En 3.10 el harness NO debe reventar con
# un ImportError: arranca sin leer `rules.toml` (permisos por defecto, que ya son "ask"
# para todo lo que muta) y lo avisa una vez. Ver `version_notice()`.
MIN_PYTHON = (3, 11)
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depende del intérprete
    tomllib = None  # type: ignore[assignment]

TOML_AVAILABLE = tomllib is not None


def version_notice() -> str:
    """Aviso claro si falta `tomllib` (Python < 3.11). Vacío si todo va bien."""
    if TOML_AVAILABLE:
        return ""
    got = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return (
        f"aviso: este Python es {got} y el harness necesita "
        f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ para leer `rules.toml` (le falta `tomllib`).\n"
        '       No se ejecutarán tareas ni se ignorará rules.toml.\n'
        '       Instala Python 3.11+ para recuperar tus reglas.'
    )


# Herramientas que no tocan el sistema: se permiten incluso en modo --read-only.
# `plan_update` y `subagent` son coordinación interna; su propio contenido pasa por la
# puerta de permisos igualmente.
SAFE_TOOLS = {
    "read_file", "list_dir", "glob", "grep", "fetch_url", "plan_update", "subagent",
}


@dataclass
class Rule:
    """Una regla del Permission Gate: primera coincidencia gana."""

    tool: str
    match: str
    action: str  # allow | ask | deny
    reason: str = ""


@dataclass
class Config:
    model: str = DEFAULT_MODEL
    host: str = DEFAULT_HOST
    temperature: float = 0.0
    num_ctx: int = 8192
    # Sin tope, un modelo pequeño puede enrollarse en un bucle y bloquear la sesión
    # durante minutos (medido: ~1.900 tokens generados hasta agotar el timeout).
    num_predict: int = 512
    timeout: int = 180

    workspace: str = "."
    state_dir: str = ".loagen"
    allow_outside_workspace: bool = False

    task_timeout: int = 300
    max_tool_calls: int = 40
    max_total_tokens: int = 24000
    expected_files: dict[str, str | None] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)
    skills_root: str = ''
    mcp_servers: list[dict] = field(default_factory=list)
    allow_private_network: bool = False

    max_steps: int = 12
    max_retries: int = 2  # reintentos guiados cuando el modelo se rinde tras un error
    tool_output_max_chars: int = 4000
    keep_last_messages: int = 14
    compress_threshold: float = 0.85  # fracción del presupuesto de contexto

    # Task Graph (planificación previa)
    plan: bool = False
    plan_max_steps: int = 5
    # Avisos de "te quedan pasos": con 1 solo, un 1.5B se descuelga del plan y termina
    # pronto (medido). Tiene su propio presupuesto para no gastar los reintentos de error.
    plan_max_nudges: int = 3
    # `plan_require`: no se acepta terminar con pasos pendientes (hasta el tope duro).
    plan_require: bool = False
    # Tope duro de rechazos por plan incompleto. Es un tope a propósito: con un 1.5B,
    # insistir más allá de esto solo quema tiempo sin que el modelo cambie de conducta.
    plan_hard_stops: int = 2

    # Web (navegador)
    web: bool = True

    # Selección de herramientas por tarea: se envían al modelo solo los grupos que la
    # tarea puede necesitar (menos tokens de prompt = menos latencia en CPU). Las tareas
    # ambiguas reciben el catálogo completo. Ver `harness/toolset.py`.
    tool_selection: bool = True

    # Intérprete de la herramienta `bash`. Vacío = el del sistema (`/bin/sh` en
    # Linux/macOS, `cmd.exe` en Windows). Para usar POSIX en Windows: `shell = "bash"`
    # (Git Bash) o una ruta completa. Ver la sección «Sistemas soportados» del README.
    shell: str = ""

    # Ruta de un `rules.toml` que no se pudo leer (p. ej. sin `tomllib`): se informa al
    # usuario en vez de fallar en silencio.
    toml_skipped: str = ""

    # Ruta del `rules.toml` que se cargó de verdad ("" si no había ninguno): lo reporta
    # `--doctor` para que no haya duda de qué configuración está en juego.
    rules_path: str = ""

    # Cerebro local (índice del workspace). Vacío -> se autodetecta.
    cerebro_root: str = ""

    # Subagente (contexto aislado)
    subagent_model: str = ""  # vacío -> el mismo modelo
    subagent_max_steps: int = 8
    subagent_max_chars: int = 1500  # tamaño del informe que vuelve al agente padre

    # Permission Gate
    default_action: str = "ask"  # acción para herramientas que mutan
    auto_approve: bool = False  # --yes
    read_only: bool = False
    rules: list[Rule] = field(default_factory=list)

    @property
    def state_path(self) -> str:
        return os.path.join(os.path.abspath(self.workspace), self.state_dir)

    @property
    def memory_path(self) -> str:
        return os.path.join(self.state_path, "agent_memory.md")

    @property
    def traces_path(self) -> str:
        return os.path.join(self.state_path, "traces")

    @property
    def sources_path(self) -> str:
        return os.path.join(self.state_path, "fuentes.jsonl")

    def action_for(self, tool: str, target: str) -> str:
        """Decide allow/ask/deny.

        `--read-only` es un **suelo absoluto**: se evalúa antes que las reglas, porque
        si no una regla `allow` del fichero (p. ej. `git commit` o `go test`) anularía
        la promesa del modo. Sin read-only: regla explícita > naturaleza de la
        herramienta.
        """
        import re

        if self.read_only and tool not in SAFE_TOOLS:
            return 'deny'
        if tool.startswith('mcp_'):
            # An external description cannot authorize itself; deny rules still apply.
            for rule in self.rules:
                if rule.tool in (tool, '*') and rule.action == 'deny' and re.search(rule.match, target):
                    return 'deny'
            return 'ask'
        for rule in self.rules:
            if rule.tool not in (tool, "*"):
                continue
            try:
                if re.search(rule.match, target):
                    # Shell text cannot be proven read-only from a regex prefix.
                    if tool == 'bash' and rule.action == 'allow':
                        return 'ask'
                    return rule.action
            except re.error:
                if rule.match in target:
                    return rule.action
        if tool in SAFE_TOOLS:
            return "allow"
        return self.default_action


def _as_rule(raw: dict) -> Rule:
    return Rule(
        tool=str(raw.get("tool", "*")),
        match=str(raw.get("match", "")),
        action=str(raw.get("action", "ask")),
        reason=str(raw.get("reason", "")),
    )


def default_cerebro_root(workspace: str | None = None) -> str:
    """Autodetecta `cerebro/`.

    Se mira, por orden: junto al harness (layout del repo, que es el caso de uso
    normal), dentro del workspace y en su directorio padre (layout instalado, donde el
    harness vive en `site-packages` y no hay nada a su lado). Si no aparece, devuelve
    cadena vacía y las herramientas del cerebro se retiran del registro.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.normpath(os.path.join(here, "..", "..", "..", "cerebro"))]
    if workspace:
        root = os.path.abspath(workspace)
        candidates += [
            os.path.join(root, "cerebro"),
            os.path.normpath(os.path.join(root, "..", "cerebro")),
        ]
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "cerebro.py")):
            return candidate
    return ""


def default_config_path(workspace: str | None) -> str | None:
    """Busca `rules.toml` en el workspace y, si no, junto al harness.

    Así el harness funciona en cualquier proyecto sin copiar el fichero: basta con
    tener un `rules.toml` por defecto al lado de `agente.py`.
    """
    candidates = [
        os.path.join(os.path.abspath(workspace or "."), "rules.toml"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rules.toml"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.normpath(candidate)
    return None


def load_config(
    workspace: str | None = None,
    config_path: str | None = None,
    overrides: dict | None = None,
) -> Config:
    """Carga `rules.toml` si existe y aplica overrides de CLI (los overrides ganan)."""
    data: dict = {}
    path = config_path or default_config_path(workspace)
    if path and os.path.isfile(path):
        if tomllib is None:
            raise ValueError('hace falta Python 3.11+ con tomllib para leer rules.toml; no se ignoran reglas de seguridad')
        else:
            toml_skipped = ""
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
    else:
        toml_skipped = ""

    cfg = Config()
    cfg.toml_skipped = toml_skipped
    cfg.rules_path = path if (path and os.path.isfile(path) and not toml_skipped) else ""
    model = data.get("model", {})
    cfg.model = str(model.get("name", cfg.model))
    cfg.host = str(model.get("host", cfg.host))
    cfg.temperature = float(model.get("temperature", cfg.temperature))
    cfg.num_ctx = int(model.get("num_ctx", cfg.num_ctx))
    cfg.num_predict = int(model.get("num_predict", cfg.num_predict))
    cfg.timeout = int(model.get("timeout", cfg.timeout))

    loop = data.get("loop", {})
    cfg.task_timeout = int(loop.get('task_timeout', cfg.task_timeout))
    cfg.max_tool_calls = int(loop.get('max_tool_calls', cfg.max_tool_calls))
    cfg.max_total_tokens = int(loop.get('max_total_tokens', cfg.max_total_tokens))
    cfg.skills = list(data.get('skills', {}).get('enabled', []))
    cfg.skills_root = str(data.get('skills', {}).get('root', ''))
    cfg.mcp_servers = list(data.get('mcp', {}).get('servers', []))
    cfg.allow_private_network = bool(data.get('network', {}).get('allow_private', False))
    cfg.max_steps = int(loop.get("max_steps", cfg.max_steps))
    cfg.max_retries = int(loop.get("max_retries", cfg.max_retries))
    cfg.plan = bool(loop.get("plan", cfg.plan))
    cfg.plan_max_steps = int(loop.get("plan_max_steps", cfg.plan_max_steps))
    cfg.plan_max_nudges = int(loop.get("plan_max_nudges", cfg.plan_max_nudges))
    cfg.plan_require = bool(loop.get("plan_require", cfg.plan_require))
    cfg.plan_hard_stops = int(loop.get("plan_hard_stops", cfg.plan_hard_stops))
    cfg.web = bool(loop.get("web", cfg.web))
    cfg.tool_selection = bool(loop.get("tool_selection", cfg.tool_selection))
    cfg.shell = str(loop.get("shell", cfg.shell))
    cfg.cerebro_root = str(loop.get("cerebro_root", cfg.cerebro_root))
    cfg.subagent_model = str(loop.get("subagent_model", cfg.subagent_model))
    cfg.subagent_max_steps = int(loop.get("subagent_max_steps", cfg.subagent_max_steps))
    cfg.subagent_max_chars = int(loop.get("subagent_max_chars", cfg.subagent_max_chars))
    cfg.tool_output_max_chars = int(
        loop.get("tool_output_max_chars", cfg.tool_output_max_chars)
    )
    cfg.keep_last_messages = int(
        loop.get("keep_last_messages", cfg.keep_last_messages)
    )
    cfg.compress_threshold = float(
        loop.get("compress_threshold", cfg.compress_threshold)
    )

    perms = data.get("permissions", {})
    cfg.default_action = str(perms.get("default", cfg.default_action))
    cfg.allow_outside_workspace = bool(
        perms.get("allow_outside_workspace", cfg.allow_outside_workspace)
    )
    cfg.rules = [_as_rule(r) for r in perms.get("rule", [])]
    if cfg.default_action not in ('allow', 'ask', 'deny') or any(r.action not in ('allow', 'ask', 'deny') for r in cfg.rules):
        raise ValueError('acción de permisos inválida: usa allow, ask o deny')
    for rule in cfg.rules:
        import re
        re.compile(rule.match)

    cfg.workspace = os.path.abspath(workspace or cfg.workspace)

    for key, value in (overrides or {}).items():
        if value is not None and hasattr(cfg, key):
            setattr(cfg, key, value)

    if not cfg.cerebro_root:
        cfg.cerebro_root = default_cerebro_root(cfg.workspace)

    for name in ('task_timeout', 'max_tool_calls', 'max_total_tokens', 'max_steps', 'num_ctx', 'timeout', 'tool_output_max_chars'):
        if getattr(cfg, name) < 1:
            raise ValueError(f'{name} debe ser positivo')
    if not 0 < cfg.compress_threshold <= 1 or cfg.keep_last_messages < 1:
        raise ValueError('presupuesto de contexto inválido')
    if cfg.read_only:
        cfg.default_action = "deny"
    return cfg
