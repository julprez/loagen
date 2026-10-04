"""Permission Gate de 3 niveles: deny / ask / approve (allow).

Primera regla que casa gana. Las reglas viven en `rules.toml`:

    [[permissions.rule]]
    tool = "bash"
    match = "rm\\\\s+-rf"
    action = "deny"

Modos globales:
  * `--read-only`  -> cualquier herramienta que muta queda en `deny`.
  * `--yes`        -> los `ask` se auto-aprueban (solo para sesiones de confianza).
"""

from __future__ import annotations

import json
import sys

from ..config import Config
from ..storage.trace import Trace

class PermissionGate:
    def __init__(self, cfg: Config, trace: Trace, interactive: bool = True):
        self.cfg = cfg
        self.trace = trace
        self.interactive = interactive
        self._always: set[tuple[str, str]] = set()

    def check(self, tool: str, args: dict) -> tuple[bool, str]:
        """Devuelve (permitido, motivo). El motivo se reinyecta como observación."""
        target = self._target(tool, args)
        if tool.startswith('mcp_'):
            target = json.dumps(args, ensure_ascii=False)
        rule = next(
            (r for r in self.cfg.rules if r.tool in (tool, "*") and self._matches(r, target)),
            None,
        )
        action = self.cfg.action_for(tool, target)
        reason = (rule.reason or f"regla '{rule.match}'") if rule else "política por defecto"

        if action == "allow":
            self.trace.emit("permission", tool=tool, action="allow", reason=reason)
            return True, reason

        if action == "deny":
            self.trace.emit("permission", tool=tool, action="deny", reason=reason)
            self.trace.warn(f"DENEGADO {tool}: {reason}")
            return False, f"PERMISO DENEGADO por el usuario ({reason}). Busca otra vía o termina."

        # action == "ask"
        if (tool, target) in self._always:
            self.trace.emit("permission", tool=tool, action="allow", reason="siempre")
            return True, "aprobado previamente en esta sesión"
        if self.cfg.auto_approve:
            self.trace.emit("permission", tool=tool, action="allow", reason="--yes")
            return True, "auto-aprobado (--yes)"
        if not self.interactive or not sys.stdin.isatty():
            self.trace.emit("permission", tool=tool, action="deny", reason="no interactivo")
            return False, ("PERMISO DENEGADO: no hay terminal interactiva para aprobar. "
                           "Reformula sin esta herramienta o pide --yes al usuario.")

        answer = self._prompt(tool, target)
        if answer == "always":
            self._always.add((tool, target))
            action, answer = "allow", "allow"
        action = answer
        self.trace.emit("permission", tool=tool, action=action, reason="usuario")
        if action == "allow":
            return True, "aprobado por el usuario"
        return False, "PERMISO DENEGADO por el usuario. No insistas: cambia de plan."

    # -- internos ----------------------------------------------------------
    def _target(self, tool: str, args: dict) -> str:
        for key in ("command", "path", "url", "pattern"):
            value = args.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(args, ensure_ascii=False)

    @staticmethod
    def _matches(rule, target: str) -> bool:
        import re

        try:
            return re.search(rule.match, target) is not None
        except re.error:
            return rule.match in target

    def _prompt(self, tool: str, target: str) -> str:
        shown = target if len(target) <= 300 else target[:297] + "..."
        while True:
            print(f"\n\033[35m[permiso]\033[0m {tool}: {shown}", file=sys.stderr)
            try:
                reply = input("  ¿permitir? [s]í / [n]o / [t]odo esta sesión: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return "deny"
            if reply in ("s", "si", "sí", "y", "yes"):
                return "allow"
            if reply in ("n", "no"):
                return "deny"
            if reply in ("t", "todo", "always", "a"):
                return "always"
