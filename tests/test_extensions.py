from pathlib import Path
import sys
import tempfile
import unittest

from harness.skills import SkillStore


SERVER = r'''
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method = request['method']
    if method == 'initialize':
        result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fixture', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'echo', 'description': 'fixture', 'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}}]}
    elif method == 'tools/call':
        result = {'content': [{'type': 'text', 'text': request['params']['arguments']['text']}]}
    else:
        result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
'''


class ExtensionTests(unittest.TestCase):
    def test_bundled_skills_and_path_validation(self):
        store = SkillStore()
        self.assertEqual(len(store.names()), 3)
        self.assertIn('no concede permisos', store.load(['code-review']))
        with self.assertRaises(ValueError):
            store.load(['../escape'])
        with self.assertRaises(ValueError):
            store.load(['research'], max_chars=5)

    def test_mcp_real_stdio_session_and_cleanup(self):
        from harness.mcp import StdioClient
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'server.py'
            script.write_text(SERVER)
            with StdioClient([sys.executable, '-u', str(script)], tmp, timeout=2) as client:
                tools = client.list_tools()
                self.assertEqual(tools[0]['name'], 'echo')
                result = client.call_tool('echo', {'text': 'verified fixture'})
                self.assertEqual(result.content, 'verified fixture')
                self.assertTrue(result.ok)
                process = client.process
            self.assertIsNotNone(process.poll())

    def test_mcp_timeout_stops_process(self):
        from harness.mcp import StdioClient
        with tempfile.TemporaryDirectory() as tmp:
            client = StdioClient([sys.executable, '-c', 'import time; time.sleep(10)'], tmp, timeout=.1)
            with self.assertRaises(TimeoutError):
                with client:
                    pass
            self.assertIsNotNone(client.process.poll())
