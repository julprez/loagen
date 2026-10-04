"""Cliente MCP stdio 2025-06-18. Sin HTTP, OAuth, sampling ni scripts implícitos."""
import json
import os
import queue
import subprocess
import threading
import time

from ..tools.process import terminate
from ..tools.result import ToolResult

VERSION = '2025-06-18'


class StdioClient:
    def __init__(self, command: list[str], workspace: str, timeout: float = 10,
                 max_bytes: int = 262144):
        if not command or not all(isinstance(x, str) for x in command):
            raise ValueError('MCP command debe ser una lista de strings')
        self.command = command
        self.workspace = workspace
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.process: subprocess.Popen[bytes] | None = None
        self.responses: queue.Queue[dict | Exception] = queue.Queue(maxsize=32)
        self.reader: threading.Thread | None = None
        self.counter = 0

    def __enter__(self):
        # Do not transmit API credentials from the host environment by default.
        env = {k: v for k, v in os.environ.items() if k in ('PATH', 'SYSTEMROOT', 'WINDIR', 'HOME', 'LANG', 'TMP', 'TEMP')}
        self.process = subprocess.Popen(self.command, cwd=self.workspace, env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL,
                                        start_new_session=os.name == 'posix')
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            result = self.request('initialize', {'protocolVersion': VERSION, 'capabilities': {},
                                  'clientInfo': {'name': 'loagen', 'version': '0.1.0'}})
            if result.get('protocolVersion') != VERSION or 'tools' not in result.get('capabilities', {}):
                raise ValueError('servidor MCP incompatible: requiere tools y versión ' + VERSION)
            self.notify('notifications/initialized')
            return self
        except Exception:
            self.close()
            raise

    def _read(self):
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                line = self.process.stdout.readline(self.max_bytes + 1)
                if not line:
                    self.responses.put_nowait(EOFError('MCP cerró stdout'))
                    return
                if len(line) > self.max_bytes:
                    raise ValueError('respuesta MCP demasiado grande')
                self.responses.put_nowait(json.loads(line))
        except (ValueError, OSError, queue.Full) as exc:
            try:
                self.responses.put_nowait(exc)
            except queue.Full:
                terminate(self.process)

    def _send(self, message: dict):
        assert self.process is not None and self.process.stdin is not None
        payload = json.dumps(message).encode() + b'\n'
        if len(payload) > self.max_bytes:
            raise ValueError('petición MCP demasiado grande')
        errors: list[Exception] = []
        pipe = self.process.stdin
        def write():
            try:
                pipe.write(payload)
                pipe.flush()
            except (OSError, ValueError) as exc:
                errors.append(exc)
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        writer.join(timeout=self.timeout)
        if writer.is_alive():
            terminate(self.process)
            writer.join(timeout=1)
            raise TimeoutError('servidor MCP no consume stdin')
        if errors:
            raise errors[0]

    def notify(self, method: str, params: dict | None = None):
        self._send({'jsonrpc': '2.0', 'method': method, 'params': params or {}})

    def request(self, method: str, params: dict | None = None) -> dict:
        self.counter += 1
        identifier = self.counter
        self._send({'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                response = self.responses.get(timeout=max(.001, deadline - time.monotonic()))
            except queue.Empty as exc:
                self.close()
                raise TimeoutError('timeout MCP: ' + method) from exc
            if isinstance(response, Exception):
                raise response
            if response.get('jsonrpc') != '2.0':
                raise ValueError('JSON-RPC MCP inválido')
            if 'method' in response and 'id' in response:
                self._send({'jsonrpc': '2.0', 'id': response['id'], 'error': {'code': -32601, 'message': 'Capability not supported'}})
            if response.get('id') == identifier:
                if 'error' in response:
                    raise ValueError('error MCP: ' + str(response['error']))
                if not isinstance(response.get('result'), dict):
                    raise ValueError('resultado MCP inválido')
                return response['result']
            if time.monotonic() >= deadline:
                self.close()
                raise TimeoutError('timeout MCP: ' + method)

    def list_tools(self) -> list[dict]:
        tools = []
        cursor = None
        for _ in range(10):
            result = self.request('tools/list', {'cursor': cursor} if cursor else {})
            rows = result.get('tools', [])
            if not isinstance(rows, list):
                raise ValueError('tools/list inválido')
            tools.extend(rows)
            if len(tools) > 100:
                raise ValueError('catálogo MCP demasiado grande')
            cursor = result.get('nextCursor')
            if not cursor:
                return tools
        raise ValueError('paginación MCP excedida')

    def call_tool(self, name: str, arguments: dict) -> ToolResult:
        result = self.request('tools/call', {'name': name, 'arguments': arguments})
        text = '\n'.join(item.get('text', '') for item in result.get('content', []) if item.get('type') == 'text')
        return ToolResult(text, ok=not result.get('isError', False))

    def close(self):
        if self.process is not None:
            terminate(self.process)
            for pipe in (self.process.stdin, self.process.stdout):
                if pipe and not pipe.closed:
                    pipe.close()
            if self.reader and self.reader is not threading.current_thread():
                self.reader.join(timeout=1)

    def __exit__(self, *args):
        self.close()
