"""Memoria persistente + compresión de contexto por capas.

Mapea dos piezas del diagrama:

* **Memory Store** (Cross-session persistence) -> `agent_memory.md`, que el
  agente lee al arrancar y puede ampliar con la herramienta `remember`.
* **Context Compressor** (3-layer, umbral) -> tres capas con distinta política:

    capa 1  system + reglas      congelada, nunca se comprime
    capa 2  agent_memory.md      estable: igual en cada turno
    capa 3  historial            elástico: se resume al pasar el umbral

  Mantener 1 y 2 idénticos al principio del prompt es lo que permite que Ollama
  reutilice el prefijo en su caché (`Prompt Cache` del diagrama) y que un 1.5B
  no pierda sus instrucciones por el camino.
"""

from __future__ import annotations

import json
import os
import re
import time

HEADER = "# Memoria del agente\n\nNotas duraderas escritas por el agente (`remember`).\n"


class Memory:
    def __init__(self, path: str, max_chars: int = 4000):
        self.path = path
        self.max_chars = max_chars
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(HEADER)

    def read(self) -> str:
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(0, os.SEEK_END)
                end = fh.tell()
                fh.seek(max(0, end - self.max_chars * 4))
                text = fh.read()
        except OSError:
            return ""
        if len(text) > self.max_chars:
            text = text[-self.max_chars:]
        return text.strip()

    def append(self, note: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M")
        text = self.read() + f"\n- ({stamp}) {note.strip()}\n"
        text = text[-self.max_chars:]
        temp = self.path + '.tmp'
        with open(temp, 'w', encoding='utf-8') as fh:
            fh.write(text)
        os.replace(temp, self.path)


class SourceLedger:
    """Fuentes que el agente ha consultado, numeradas y persistentes.

    El problema que resuelve: un modelo pequeño cita URLs de memoria (medido: tras
    buscar «última versión de Python» encontró 3.14.8 y se inventó la fecha de salida).
    Aquí cada consulta real —`fetch_url`, `wiki`, un resultado de `web_search`— queda
    registrada con un número, y la herramienta `fuentes` devuelve esa lista numerada
    para que el modelo cite **de ahí** en vez de inventar.

    Detalles de diseño:

    * **JSONL, no markdown**: una fuente por línea, se puede releer sin parsear prosa.
    * **Fuera del prefijo congelado**: la lista crece en cada paso, así que meterla en el
      prompt invalidaría la caché de prefijo de Ollama. El modelo la pide con `fuentes`.
    * **Distingue leída de vista**: una URL que solo salió en resultados de búsqueda se
      marca como *no leída*; el harness no la trata como evidencia.
    * **Compartido con los subagentes** (mismo workspace, mismo fichero): lo que el hijo
      leyó lo puede citar el padre, que es lo correcto.
    """

    def __init__(self, path: str, max_entries: int = 500):
        self.path = path
        self.max_entries = max_entries
        self._cached: list[dict] = []
        self._signature: tuple[int, int] | None = None
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def entries(self) -> list[dict]:
        out: list[dict] = []
        try:
            stat = os.stat(self.path)
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature == self._signature:
                return [dict(item) for item in self._cached]
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict) and item.get("ref"):
                        out.append(item)
        except OSError:
            return []
        self._cached = [dict(item) for item in out]
        self._signature = signature
        return out

    # Una URL pasa de «solo vista en un resultado» a «leída» sin cambiar de número.
    READ_KINDS = ("url", "wiki")

    def add(self, kind: str, ref: str, title: str = "") -> dict | None:
        """Registra una fuente y devuelve su entrada (con número). No duplica.

        Si la misma URL ya estaba registrada como resultado de búsqueda y ahora se lee
        con `fetch_url`, la entrada **se asciende** a leída conservando su número. Sin
        esto pasó lo medido: una página leída seguía marcada `[NO leída]` y el guard de
        citación no la veía.
        """
        ref = " ".join(str(ref or "").split())
        if not ref:
            return None
        entries = self.entries()
        for item in entries:
            if item.get("ref") != ref:
                continue
            if kind in self.READ_KINDS and item.get("kind") not in self.READ_KINDS:
                item["kind"] = kind
                if title:
                    item["title"] = " ".join(str(title).split())[:160]
                self._write_all(entries)
            return item
        if len(entries) >= self.max_entries:
            return None
        entry = {
            "n": len(entries) + 1,
            "kind": kind,
            "ref": ref,
            "title": " ".join(str(title or "").split())[:160],
        }
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            return None
        return entry

    def _write_all(self, entries: list[dict]) -> None:
        temp = self.path + '.tmp'
        with open(temp, 'w', encoding='utf-8') as fh:
            for item in entries:
                fh.write(json.dumps(item, ensure_ascii=False) + '\n')
        os.replace(temp, self.path)
        self._signature = None

    def read_urls(self) -> set[str]:
        """URLs **leídas** de verdad (no las que solo aparecieron en una búsqueda)."""
        return {e["ref"] for e in self.entries() if e.get("kind") in self.READ_KINDS}

    def known_urls(self) -> set[str]:
        return {e["ref"] for e in self.entries() if e.get("ref", "").startswith("http")}

    def uncited(self, answer: str) -> set[str]:
        """URLs leídas que la respuesta no cita, ni por URL ni por su número `[n]`.

        Contar el número es imprescindible: medido, el modelo cita `[1]` (que es justo lo
        que se le pide) y un guard que solo buscara la URL no se apagaría nunca, gastando
        los reintentos en un aviso ya cumplido.
        """
        text = answer or ""
        numbers = {int(n) for n in re.findall(r"\[(\d+)\]", text)}
        out: set[str] = set()
        for item in self.entries():
            if item.get("kind") not in self.READ_KINDS:
                continue
            ref = item.get("ref", "")
            if not ref or ref in text:
                continue
            if item.get("n") in numbers:
                continue
            out.add(ref)
        return out

    def find(self, key) -> dict | None:
        """Busca por número o por fragmento de la referencia."""
        entries = self.entries()
        text = str(key).strip()
        if text.isdigit():
            number = int(text)
            for item in entries:
                if item.get("n") == number:
                    return item
        for item in entries:
            if text and text.lower() in str(item.get("ref", "")).lower():
                return item
        return None

    def render(self, limit: int = 20, entries: list[dict] | None = None) -> str:
        """Lista numerada lista para pegar en una respuesta.

        `entries` permite al CLI limitarse a las fuentes de ESTA sesión (el registro es
        del workspace y persiste): sin eso, al cerrar una tarea se listarían como citas
        de hoy URLs de sesiones anteriores.
        """
        entries = self.entries() if entries is None else list(entries)
        if not entries:
            return ""
        shown = entries[-limit:]
        lines = ["FUENTES CONSULTADAS (cita el número, p. ej. [2])"]
        if len(entries) > len(shown):
            lines.append(f"(se muestran las {len(shown)} últimas de {len(entries)})")
        for item in shown:
            mark = ("" if item.get("kind") in self.READ_KINDS
                    else "  [NO leída: solo resultado de búsqueda]")
            title = f" — {item['title']}" if item.get("title") else ""
            lines.append(f"[{item.get('n', '?')}] {item.get('kind', '?')}: "
                         f"{item.get('ref', '?')}{title}{mark}")
        return "\n".join(lines)

    def cite(self, key) -> str:
        """Cita exacta de una fuente, para pegar tal cual."""
        item = self.find(key)
        if item is None:
            return (f"No hay ninguna fuente {key!r} en el registro. Llama a fuentes sin "
                    "argumentos para ver la lista.")
        return f"[{item.get('n')}] {item.get('ref')}"


def _digest_line(message: dict) -> str:
    role = message.get("role", "?")
    calls = message.get("tool_calls")
    if calls:
        names = ", ".join(c.get("function", {}).get("name", "?") for c in calls)
        return f"- {role}: llamó a {names}"
    if role == "tool":
        body = " ".join(str(message.get("content", "")).split())[:140]
        return f"- observación[{message.get('tool_name', '?')}]: {body}"
    body = " ".join(str(message.get("content", "")).split())[:160]
    return f"- {role}: {body}"


class Compressor:
    """Recorta el historial cuando se acerca al umbral de contexto."""

    def __init__(self, num_ctx: int, threshold: float, keep_last: int,
                 tool_output_max_chars: int):
        # ~4 caracteres por token es una aproximación suficiente para presupuestar.
        self.budget_chars = int(num_ctx * 4 * threshold)
        self.keep_last = keep_last
        self.tool_output_max_chars = tool_output_max_chars
        self.compressed_rounds = 0

    def build(self, frozen_prefix: str, history: list[dict], schemas: list[dict] | None = None) -> list[dict]:
        """Devuelve [system] + historial listo para enviar al modelo."""
        history = [self._trim(m) for m in history]
        system = {'role': 'system', 'content': frozen_prefix}
        schema_size = len(json.dumps(schemas or [], ensure_ascii=False))
        budget = self.budget_chars - schema_size
        def size(messages):
            return len(json.dumps(messages, ensure_ascii=False))
        if size([system] + history) <= budget:
            return [system] + history
        # Keep the user's original contract; discard entire action/observation groups.
        original = history[:1]
        groups: list[list[dict]] = []
        for message in history[1:]:
            if message.get('role') == 'tool' and groups:
                groups[-1].append(message)
            else:
                groups.append([message])
        note = {'role': 'system', 'content': 'Resumen comprimido: pasos anteriores descartados; vuelve a consultar los detalles necesarios.'}
        base = [system] + original + [note]
        if size(base) > budget:
            raise ValueError('instrucciones, tarea y herramientas exceden el presupuesto de contexto')
        keep: list[dict] = []
        for group in reversed(groups):
            if size(base + group + keep) > budget or len(group) + len(keep) > self.keep_last:
                break
            keep = group + keep
        self.compressed_rounds += 1
        return base + keep

    def _trim(self, message: dict) -> dict:
        if message.get("role") != "tool":
            return message
        content = str(message.get("content", ""))
        if len(content) <= self.tool_output_max_chars:
            return message
        trimmed = dict(message)
        trimmed["content"] = (
            content[: self.tool_output_max_chars]
            + f"\n... [observación recortada: {len(content) - self.tool_output_max_chars} caracteres]"
        )
        return trimmed
