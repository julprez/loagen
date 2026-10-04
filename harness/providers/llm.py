"""Cliente de Ollama por HTTP (`/api/chat`), sin dependencias externas.

Maneja dos dialectos de tool-calling que aparecen en los modelos locales:

1. Nativo: el modelo devuelve `message.tool_calls` (qwen2.5:1.5b-instruct lo hace).
2. Texto: el modelo escribe el JSON en `content` (qwen2.5-coder:3b lo hace).
   Se extrae con heurísticas y se normaliza al mismo formato.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_TAG = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)

# Claves habituales con las que los modelos pequeños nombran los argumentos.
_NAME_KEYS = ("name", "tool", "tool_name", "function", "action")
_ARGS_KEYS = ("arguments", "args", "parameters", "params", "input")

# Marcas de prosa: si el "resto" parece una frase, se descarta el dialecto
# posicional. Sacrifica contenidos con ", " o punto final a cambio de no
# confundir una respuesta en prosa con una llamada a herramienta.
_PROSE = re.compile(r",\s|\.\s|[?!]\s*$|\.\s*$")


class OllamaError(RuntimeError):
    pass


@dataclass
class LLMReply:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    load_seconds: float = 0.0
    prompt_seconds: float = 0.0
    eval_seconds: float = 0.0
    done_reason: str = ''


def _normalize_call(raw: dict) -> dict | None:
    """Acepta {name, arguments} o {function:{name, arguments}} y devuelve forma canónica."""
    nested = raw.get('function')
    fn = nested if isinstance(nested, dict) else raw
    name = None
    for key in _NAME_KEYS:
        candidate = fn.get(key)
        if isinstance(candidate, str) and candidate.strip():
            name = candidate.strip()
            break
    if not name:
        return None
    args = {}
    for key in _ARGS_KEYS:
        if isinstance(fn.get(key), dict):
            args = fn[key]
            break
        if isinstance(fn.get(key), str):
            try:
                parsed = json.loads(fn[key])
                if isinstance(parsed, dict):
                    args = parsed
                    break
            except (ValueError, TypeError):
                continue
    return {
        "id": raw.get("id") or f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


def iter_json_objects(text: str):
    """Recorre objetos JSON dentro de un texto, respetando anidamiento y prosa alrededor."""
    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        try:
            obj, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        yield obj
        index = text.find("{", end)


_ESCAPE_RX = re.compile(r"\\(n|t|r|\"|'|\\)")
_ESCAPE_MAP = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'", "\\": "\\"}


def _unescape(value: str) -> str:
    """Convierte `\\n` literales en saltos reales.

    En el dialecto posicional `shlex` no interpreta escapes, y los modelos escriben
    el código en una sola línea con `\\n`; sin esto, el fichero sale con un `\\n` de
    dos caracteres y el intérprete da un SyntaxError (fallo observado en pruebas).
    """
    if "\\" not in value:
        return value
    return _ESCAPE_RX.sub(lambda m: _ESCAPE_MAP[m.group(1)], value)


def _split_positional(rest: str, params: list[str]) -> dict:
    """Reparte `resto` entre los parámetros obligatorios (con comillas y espacios)."""
    import shlex

    if not params:
        return {}
    try:
        tokens = shlex.split(rest)
    except ValueError:
        tokens = rest.split()
    if not tokens:
        return {}
    if len(params) == 1:
        return {params[0]: _unescape(rest.strip().strip('"').strip("'"))}
    if len(tokens) < len(params):
        return {}
    head = tokens[: len(params) - 1]
    tail = " ".join(tokens[len(params) - 1:]).strip()
    return {**dict(zip(params, head)), params[-1]: _unescape(tail)}


def _unquoted(text: str) -> str:
    """Elimina los tramos entrecomillados: el contenido de un fichero no es prosa."""
    return re.sub(r'"[^"]*"|\'[^\']*\'', " ", text)


_PAREN_RX = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*$", re.S)
_KWARG_RX = re.compile(r"(\w+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^,]+)")


def _parse_call_args(inner: str, params: list[str]) -> dict:
    """Interpreta `write_file("ruta", "contenido")` sin ejecutar nada de código.

    `ast.literal_eval` solo evalúa literales (números, cadenas, listas): es seguro y no
    ejecuta llamadas ni nombres.
    """
    import ast

    inner = inner.strip()
    if not inner:
        return {}
    try:
        values = ast.literal_eval(f"({inner}{'' if inner.endswith(',') else ','})")
    except (ValueError, SyntaxError, MemoryError, TypeError):
        values = None
    if isinstance(values, tuple):
        if len(params) == 1:
            return {params[0]: values[0]}
        if len(values) == len(params):
            return dict(zip(params, values))
        return {}
    # Forma con nombres: path="x", content="y"
    out: dict = {}
    for key, raw in _KWARG_RX.findall(inner):
        if key not in params:
            continue
        try:
            out[key] = ast.literal_eval(raw.strip())
        except (ValueError, SyntaxError, MemoryError, TypeError):
            out[key] = raw.strip().strip('"').strip("'")
    return out


def _extract_paren_call(text: str, tool_params: dict[str, list[str]] | None) -> list[dict]:
    """Dialecto `herramienta(arg1, arg2)` o `herramienta(path="x")"."""
    if not tool_params or not text:
        return []
    match = _PAREN_RX.match(text.strip().lstrip("-*`# "))
    if not match:
        return []
    name, inner = match.group(1), match.group(2)
    params = tool_params.get(name)
    if not params:
        return []
    args = _parse_call_args(inner, params)
    if not args:
        return []
    return [{
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": args},
    }]


def _extract_positional(text: str, tool_params: dict[str, list[str]] | None) -> list[dict]:
    """Reconoce el dialecto posicional: `write_file ruta "contenido"`.

    Solo se aplica si TODO el texto empieza por un nombre de herramienta, para no
    confundir una respuesta en prosa ("bash es peligroso...") con una llamada.
    """
    if not tool_params or not text:
        return []
    stripped = text.strip().lstrip("-*`# ")
    for name in sorted(tool_params, key=len, reverse=True):
        if not stripped.startswith(name):
            continue
        rest = stripped[len(name):]
        if rest[:1] not in (" ", "\t"):
            continue
        rest = rest.strip()
        if not rest or rest[0] in "({[:=":
            continue
        if _PROSE.search(_unquoted(rest)):
            continue
        args = _split_positional(rest, tool_params[name])
        if args:
            return [{
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            }]
    return []


def _extract_from_text(text: str, tool_params: dict[str, list[str]] | None = None) -> list[dict]:
    """Rescata tool-calls escritos como texto (```json, <tool_call>, JSON suelto o posicional)."""
    if not text:
        return []
    blobs = [m.strip() for m in _TAG.findall(text)]
    blobs += [m.strip() for m in _FENCE.findall(text)]
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        blobs.append(stripped)
    if not any(blobs):
        blobs = [text]  # último recurso: buscar objetos JSON embebidos en la respuesta

    calls: list[dict] = []
    seen: set[str] = set()
    for blob in blobs:
        if not blob:
            continue
        items: list = []
        if blob[0] in "{[":
            try:
                parsed = json.loads(blob)
                items = parsed if isinstance(parsed, list) else [parsed]
            except ValueError:
                items = []
        if not items:
            items = list(iter_json_objects(blob))
        for item in items:
            if not isinstance(item, dict):
                continue
            call = _normalize_call(item)
            if call:
                key = json.dumps(call, sort_keys=True, ensure_ascii=False)
                if key not in seen:
                    seen.add(key)
                    calls.append(call)
    if not calls:
        calls = _extract_paren_call(text, tool_params)
    if not calls:
        calls = _extract_positional(text, tool_params)
    return calls


class Ollama:
    def __init__(self, host: str, model: str, timeout: int = 180,
                 temperature: float = 0.0, num_ctx: int = 8192,
                 num_predict: int = 512):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = float(timeout)
        self.temperature = temperature
        self.num_ctx = num_ctx
        # Tope duro de tokens generados. Sin él, un modelo pequeño se enrolla en un
        # bucle de repetición y, con inferencia en CPU (~12 tok/s), una sola llamada
        # puede durar minutos: se midió una generación de ~1.900 tokens que agotó los
        # 180 s de timeout. Un turno con herramienta gasta <100 tokens.
        self.num_predict = num_predict
        self.warm = False

    # -- API ---------------------------------------------------------------
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_params: dict[str, list[str]] | None = None) -> LLMReply:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": self._options(),
        }
        if tools:
            body["tools"] = tools
        payload = self._post("/api/chat", body)
        msg = payload.get("message") or {}
        content = msg.get("content") or ""

        raw_calls = msg.get("tool_calls") or []
        tool_calls = [c for c in (_normalize_call(r) for r in raw_calls if isinstance(r, dict)) if c]

        if not tool_calls:
            tool_calls = _extract_from_text(content, tool_params)
            if tool_calls:
                content = ""  # el "texto" era en realidad la llamada

        return LLMReply(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=int(payload.get("prompt_eval_count") or 0),
            output_tokens=int(payload.get("eval_count") or 0),
            seconds=round((payload.get("total_duration") or 0) / 1e9, 2),
            load_seconds=(payload.get('load_duration') or 0) / 1e9,
            prompt_seconds=(payload.get('prompt_eval_duration') or 0) / 1e9,
            eval_seconds=(payload.get('eval_duration') or 0) / 1e9,
            done_reason=str(payload.get('done_reason') or ''),
        )

    def _options(self) -> dict:
        options = {"temperature": self.temperature, "num_ctx": self.num_ctx}
        if self.num_predict and self.num_predict > 0:
            options["num_predict"] = self.num_predict
        return options

    def list_models(self) -> list[str]:
        payload = self._get("/api/tags")
        return [m.get("name", "") for m in payload.get("models", [])]

    def ready(self) -> tuple[bool, str]:
        try:
            models = self.list_models()
        except OllamaError as exc:
            return False, str(exc)
        names = {m.split(":")[0]: m for m in models}
        if self.model in models:
            return True, self.model
        if self.model.split(":")[0] in names:
            return True, names[self.model.split(":")[0]]
        return False, (
            f"el modelo '{self.model}' no está en Ollama. Instalados: "
            f"{', '.join(models) or '(ninguno)'}. Prueba: ollama pull {self.model}"
        )

    # -- transporte --------------------------------------------------------
    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.host + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._send(req)

    def _get(self, path: str) -> dict:
        return self._send(urllib.request.Request(self.host + path, method="GET"))

    def _send(self, req: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise OllamaError(f"Ollama devolvió HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise OllamaError(
                f"No se pudo conectar con Ollama en {self.host} ({exc.reason}). "
                "¿Está corriendo? Prueba: ollama serve"
            ) from exc
        except TimeoutError as exc:
            raise OllamaError(
                f"Ollama no respondió en {self.timeout}s y se cortó la conexión. Lo más "
                f"probable es que el modelo se esté enrollando en repeticiones: baja "
                "num_predict (tope de tokens) o sube timeout en rules.toml."
            ) from exc
        except ValueError as exc:
            raise OllamaError(f"Respuesta inválida de Ollama: {exc}") from exc
