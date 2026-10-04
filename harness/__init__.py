"""Harness de agente local para Ollama.

Arquitectura inspirada en la capa útil del diagrama "Claude Code Architecture"
(Input / Knowledge / Execution / Observability), recortada a lo que un modelo
de 1.5B sostiene de verdad:

  INPUT            -> CLI + Permission Gate + reglas TOML (3 niveles)
  KNOWLEDGE        -> memoria persistente + compresión de contexto por capas
  EXECUTION        -> Master Agent Loop + Tool Dispatch tipado
  OBSERVABILITY    -> trazas JSONL (event bus) + resumen final

Dependencias: solo biblioteca estándar de Python.
"""

# Preserve historical imports and monkeypatch identity during the module migration.
import importlib
import sys

for _alias, _module in {
    'llm': 'providers.llm', 'permissions': 'policy.permissions',
    'network': 'policy.network', 'memory': 'storage.memory',
    'trace': 'storage.trace', 'process': 'tools.process',
    'result': 'tools.result', 'runtime': 'core.runtime',
}.items():
    _loaded = importlib.import_module('.' + _module, __name__)
    sys.modules[__name__ + '.' + _alias] = _loaded
    globals()[_alias] = _loaded

__all__ = ["config", "llm", "tools", "permissions", "memory", "trace", "loop"]
