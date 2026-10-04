"""Acceso al cerebro local (índice SQLite del workspace) como herramienta.

El cerebro es ~250-650× más rápido que recorrer ficheros a mano (medido en
`cerebro/AGENTS.md`), así que un agente que trabaja en este workspace debe usarlo
**antes** de leer ficheros. Aquí se envuelve su CLI:

* `cerebro.py search <consulta> --json` -> búsqueda léxica (FTS5/BM25).
* `codeindex.py defs <símbolo> --json` -> dónde está DEFINIDO.
* `codeindex.py callers <símbolo> --json` -> qué definiciones lo USAN (directo).
* `codeindex.py impact <símbolo>` -> «qué rompe si cambio esto» (transitivo).

Dos detalles que importan:

* **`--json` se parsea, el resto se recorta.** La salida de `impact` es texto; se
  limita para no inundar el contexto de un modelo de 1.5B.
* **Nunca lanza.** Si el cerebro no está, si falla o si tarda, devuelve un texto que
  el modelo puede leer; la herramienta se retira del registro cuando no existe.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

DEFAULT_TIMEOUT = 30


class Cerebro:
    def __init__(self, root: str, enabled: bool = True, timeout: int = DEFAULT_TIMEOUT):
        self.root = os.path.abspath(root)
        self.enabled = enabled
        self.timeout = timeout

    # -- disponibilidad ----------------------------------------------------
    @property
    def search_script(self) -> str:
        return os.path.join(self.root, "cerebro.py")

    @property
    def codeindex_script(self) -> str:
        return os.path.join(self.root, "codeindex.py")

    def disponible(self) -> bool:
        return self.enabled and os.path.isfile(self.search_script)

    def _run(self, script: str, args: list[str], pkgs: bool = False) -> tuple[int, str]:
        if not os.path.isfile(script):
            return 1, f"ERROR: no encuentro {script}"
        env = dict(os.environ)
        if pkgs:
            env["PYTHONPATH"] = os.path.join(self.root, ".pkgs")
        try:
            proc = subprocess.run(
                [sys.executable, script, *args],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return 1, f"ERROR: el cerebro superó {self.timeout}s."
        except OSError as exc:
            return 1, f"ERROR al ejecutar el cerebro: {exc}"
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        return proc.returncode, out.strip()

    # -- consultas ---------------------------------------------------------
    def buscar(self, consulta: str, limite: int = 8) -> str:
        code, out = self._run(
            self.search_script, ["search", consulta, "--json", "-n", str(limite)]
        )
        if code != 0:
            return f"ERROR del cerebro: {out[:400]}"
        try:
            data = json.loads(out)
        except ValueError:
            return f"ERROR: salida no JSON del cerebro: {out[:300]}"

        results = data.get("results") or []
        mode = data.get("mode", "?")
        if not results:
            return (f"El cerebro no encuentra nada para {consulta!r}. "
                    "Prueba con palabras clave del código o reformula.")

        lines = [f"cerebro: {len(results)} resultados (modo {mode}, "
                 f"{data.get('elapsed_ms', 0):.1f} ms)"]
        if mode == "OR":
            # En modo OR el ranking es ruido (lo documenta cerebro/AGENTS.md). Se dice
            # con claridad y se pide reformular, en vez de dejar que el modelo sigo.
            # Medido: con una consulta en prosa el modelo respondió mal porque el modo
            # OR le devolvió primero un script de otro proyecto.
            lines.append(
                "AVISO IMPORTANTE: modo OR significa que alguna palabra NO existe en el "
                "índice, así que estos resultados son RUIDO y no puedes fiarte de ellos. "
                "Repite cerebro_buscar con 3 palabras clave EXACTAS del código (nombre de "
                "fichero, símbolo o comando), no una frase."
            )
        for item in results:
            lines.append(f"- {item.get('loc', '?')} (score {item.get('score', 0):.2f})")
            snippet = " ".join(str(item.get("snippet", "")).split())
            if snippet:
                lines.append(f"    {snippet[:200]}")
        return "\n".join(lines)

    def impacto(self, simbolo: str, limite: int = 40) -> str:
        code, out = self._run(self.codeindex_script, ["impact", simbolo], pkgs=True)
        if code != 0:
            return f"ERROR del cerebro (impact): {out[:400]}"
        if len(out) > limite * 60:
            out = out[: limite * 60] + "\n... [recortado]"
        return out or f"(sin información para {simbolo!r})"

    def _codeindex_json(self, modo: str, simbolo: str) -> tuple[dict | None, str]:
        """Ejecuta `codeindex.py <modo> --json` y devuelve (datos, error).

        El índice estructural solo se consulta por el identificador **desnudo**
        (`Repo.Save` -> `Save`), y la ambigüedad se declara en la salida en vez de
        esconderse: aquí no se elige una definición por el modelo.
        """
        code, out = self._run(self.codeindex_script, [modo, simbolo, "--json"], pkgs=True)
        if code != 0:
            return None, f"ERROR del cerebro ({modo}): {out[:400]}"
        try:
            return json.loads(out), ""
        except ValueError:
            return None, f"ERROR: salida no JSON del cerebro ({modo}): {out[:300]}"

    def definiciones(self, simbolo: str, limite: int = 12) -> str:
        """Dónde está definido un símbolo (no quién lo usa)."""
        data, err = self._codeindex_json("defs", simbolo)
        if err or data is None:
            return err or 'ERROR: datos del cerebro vacíos'
        rows = data.get("definitions") or []
        if not rows:
            # Comprobado en este workspace: el índice estructural puede estar construido
            # antes de que existiera un proyecto, y entonces devuelve 0 para símbolos que
            # SÍ existen. Decir solo «no existe» haría que el modelo dejara de buscar.
            return (f"El cerebro no tiene ninguna definición de {simbolo!r}. Dos causas "
                    "posibles: (a) el nombre no es exacto —el índice no busca por "
                    "frases—, o (b) ese proyecto no está en el índice (se reconstruye "
                    "con codeindex.py build). Comprueba el nombre con cerebro_buscar; "
                    "si el fichero existe, usa grep o read_file.")
        lines = [f"DEFINICIONES de {simbolo!r}: {len(rows)}"]
        if len(rows) > 1:
            lines.append("(el índice busca por el nombre desnudo, así que puede haber "
                         "varias con el mismo identificador en ficheros distintos)")
        for item in rows[:limite]:
            sig = " ".join(str(item.get("sig", "")).split())
            lines.append(f"- {item.get('loc', '?')}  [{item.get('kind', '?')}]")
            if sig:
                lines.append(f"    {sig[:160]}")
        if len(rows) > limite:
            lines.append(f"... y {len(rows) - limite} definición(es) más")
        return "\n".join(lines)

    def llamadores(self, simbolo: str, limite: int = 12) -> str:
        """Qué definiciones usan un símbolo, en un solo salto.

        Se distingue a propósito de `impacto`: aquí no hay propagación, así que la
        lista es corta y fiable; el árbol transitivo lo da `cerebro_impacto`.
        """
        data, err = self._codeindex_json("callers", simbolo)
        if err or data is None:
            return err or 'ERROR: datos del cerebro vacíos'
        rows = data.get("callers") or []
        if not rows:
            return (f"Ninguna definición del índice usa {simbolo!r}: o el nombre no es "
                    "exacto, o ese proyecto no está en el índice, o es un punto de "
                    "entrada. Para el árbol completo usa cerebro_impacto; si el fichero "
                    "existe, usa grep.")
        lines = [f"USAN {simbolo!r} (uso directo): {len(rows)} definición(es)"]
        for item in rows[:limite]:
            lines.append(f"- {item.get('loc', '?')}  [{item.get('kind', '?')}] "
                         f"{item.get('qual', '?')} ({item.get('refs', 0)} uso/s)")
        if len(rows) > limite:
            lines.append(f"... y {len(rows) - limite} más (o usa cerebro_impacto)")
        return "\n".join(lines)