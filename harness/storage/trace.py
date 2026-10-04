"""Observabilidad: event bus mínimo que escribe trazas JSONL y pinta en consola.

Equivale a la "Observability Layer" del diagrama (Event Bus + Hooks), sin daemon
threads: un fichero por sesión, una línea JSON por evento.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone


class Trace:
    def __init__(self, traces_dir: str, verbose: bool = False, quiet: bool = False):
        self.verbose = verbose
        self.quiet = quiet
        self.parent = ""
        self._dir = traces_dir
        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + '-' + uuid.uuid4().hex[:12]
        self.path = ""
        self.events: list[dict] = []
        os.makedirs(traces_dir, exist_ok=True)
        self.path = os.path.join(traces_dir, f"{self.session_id}.jsonl")
        self._t0 = time.time()

    def child(self, label: str) -> "Trace":
        """Traza hija en su propio fichero.

        Un subagente escribe aquí: su actividad no debe intercalarse con la del padre,
        porque `last_step_failed()` deduce el estado del paso leyendo hacia atrás hasta
        el último `model_call`.
        """
        child = Trace(self._dir, verbose=self.verbose, quiet=True)
        child.session_id = f"{self.session_id}.{label}"
        child.path = os.path.join(
            self._dir, f"{self.session_id}.{label}.jsonl"
        )
        child.parent = self.session_id
        return child

    # -- escritura ---------------------------------------------------------
    def emit(self, kind: str, **payload) -> dict:
        # Keep counters/artifact paths, not model-generated file contents or page text.
        if kind == 'tool_call' and 'args' in payload:
            args = dict(payload['args'])
            for key in ('content', 'old', 'new', 'note'):
                if key in args:
                    args[key] = '[redactado]'
            payload['args'] = args
        if kind in ('tool_result', 'tool_error') and 'output' in payload:
            payload['output'] = '[resultado omitido; consulta el estado y artefactos]'
        if kind in ('final', 'max_steps'):
            payload.pop('content', None)
        event = {
            "ts": round(time.time() - self._t0, 3),
            "kind": kind,
            **payload,
        }
        self.events.append(event)
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:
            self.warn(f'no se pudo guardar la traza: {exc}')
        return event

    # -- presentación ------------------------------------------------------
    def info(self, text: str) -> None:
        if not self.quiet:
            print(text, file=sys.stderr)

    def detail(self, text: str) -> None:
        if self.verbose and not self.quiet:
            print(text, file=sys.stderr)

    def step(self, index: int, text: str) -> None:
        if not self.quiet:
            print(f"\033[36m[{index}]\033[0m {text}", file=sys.stderr)

    def tool_call(self, name: str, args: dict) -> None:
        if not self.quiet:
            shown = json.dumps(args, ensure_ascii=False)
            if len(shown) > 160:
                shown = shown[:157] + "..."
            print(f"  \033[33m->\033[0m {name}({shown})", file=sys.stderr)

    def observation(self, text: str) -> None:
        if not self.verbose or self.quiet:
            return
        one_line = " ".join(str(text).split())
        if len(one_line) > 200:
            one_line = one_line[:197] + "..."
        print(f"  \033[32m<-\033[0m {one_line}", file=sys.stderr)

    def warn(self, text: str) -> None:
        if not self.quiet:
            print(f"  \033[31m!\033[0m {text}", file=sys.stderr)

    # -- consultas ---------------------------------------------------------
    def last_step_failed(self) -> bool:
        """¿El paso más reciente (desde el último `model_call`) acabó en error o denegación?"""
        for event in reversed(self.events):
            kind = event.get("kind")
            if kind in ("model_call", "session_start"):
                return False
            if kind == "tool_error":
                return True
            if kind == "permission" and event.get("action") == "deny":
                return True
        return False

    def incomplete_plan(self) -> list[str] | None:
        """Pasos que quedaron pendientes cuando el harness cerró por tope."""
        for event in reversed(self.events):
            if event.get("kind") == "plan_incomplete":
                return event.get("pending") or []
        return None

    def successful_calls(self) -> list[dict]:
        calls = {e.get('call_id'): e for e in self.events if e.get('kind') == 'tool_call' and e.get('call_id')}
        return [calls[e['call_id']] for e in self.events
                if e.get('kind') == 'tool_result' and not e.get('is_error') and e.get('call_id') in calls]

    def used_tools(self) -> set[str]:
        return {e['tool'] for e in self.successful_calls()}

    def facts(self) -> dict:
        """Hechos que el harness puede afirmar por sí mismo (no lo que dice el modelo).

        `sources` es lo que el agente (sic) consultó de verdad en la web. El harness no
        puede comprobar que cada frase salga de ahí —medido: tras buscar "última versión
        de Python" el modelo encontró 3.14.8 y se inventó la fecha de salida—, pero sí
        puede dejar la lista de fuentes a la vista para que el usuario lo compruebe.
        """
        tools: dict[str, int] = {}
        files: list[str] = []
        sources: list[str] = []
        for event in self.successful_calls():
            name = event.get("tool", "")
            args = event.get("args") or {}
            tools[name] = tools.get(name, 0) + 1
            if name in ("write_file", "edit_file", "mkdir"):
                path = args.get("path")
                if path:
                    files.append(f"{name}: {path}")
            elif name == "web_search" and args.get("query"):
                sources.append(f"búsqueda: {args['query']}")
            elif name == "fetch_url" and args.get("url"):
                sources.append(f"leída: {args['url']}")
            elif name == "wiki" and args.get("topic"):
                sources.append(f"wikipedia: {args['topic']}")
            elif name == "cerebro_buscar" and args.get("consulta"):
                sources.append(f"cerebro: {args['consulta']}")
        return {"tools": tools, "files": files, "sources": sources}

    # -- resumen -----------------------------------------------------------
    def summary(self) -> dict:
        calls = [e for e in self.events if e["kind"] == "tool_call"]
        denied = [e for e in self.events if e["kind"] == "permission" and e["action"] == "deny"]
        errors = [e for e in self.events if e["kind"] == "tool_error"]
        llm = [e for e in self.events if e['kind'] in ('model_call', 'aux_model_call')]
        return {
            "steps": sum(1 for e in self.events if e["kind"] == "model_call"),
            "facts": self.facts(),
            "tool_calls": len(calls),
            "denied": len(denied),
            "errors": len(errors),
            "seconds": round(time.time() - self._t0, 2),
            "prompt_tokens": sum(e.get("prompt_tokens", 0) for e in llm),
            "output_tokens": sum(e.get("eval_tokens", 0) for e in llm),
            "trace": self.path,
        }
