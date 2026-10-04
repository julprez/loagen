import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import agente
from harness.config import Config, Rule
from harness.extensions import mcp_tools
from harness.llm import LLMReply
from harness.loop import run
from harness.network import validate_url
from harness.process import run_bounded, shell_command
from harness.runtime import Budget
from harness.tools import Registry, ToolContext
from harness.trace import Trace


class RuntimeIntegrationTests(unittest.TestCase):
    def test_read_only_cannot_write_end_to_end(self):
        class Model:
            model = 'fixture'
            def chat(self, *args, **kwargs):
                return LLMReply(tool_calls=[{'function': {'name': 'write_file', 'arguments': {'path': 'x', 'content': 'bad'}}}])
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(workspace=tmp, read_only=True, rules=[Rule('write_file', '.*', 'allow')], max_steps=1, web=False)
            result = run(cfg, 'write x', Trace(tmp, quiet=True), False, llm=Model())
            self.assertFalse((Path(tmp) / 'x').exists())
            self.assertEqual(result.status, 'incomplete')

    def test_global_call_budget_limits_calls_within_one_turn(self):
        class Model:
            model = 'fixture'
            def chat(self, *args, **kwargs):
                return LLMReply(tool_calls=[{'function': {'name': 'write_file', 'arguments': {'path': str(i), 'content': 'x'}}} for i in range(10)])
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(workspace=tmp, auto_approve=True, max_tool_calls=3, web=False)
            result = run(cfg, 'write files', Trace(tmp, quiet=True), False, llm=Model())
            self.assertEqual(len([p for p in Path(tmp).iterdir() if p.name.isdigit()]), 2)
            self.assertEqual(result.status, 'incomplete')

    def test_cli_exit_reports_unverified_and_explicit_contract(self):
        class Model:
            model = 'fixture'
            def chat(self, *args, **kwargs):
                return LLMReply(content='Hecho.')
        with tempfile.TemporaryDirectory() as tmp, patch('harness.loop.Ollama', return_value=Model()), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cfg = Config(workspace=tmp, web=False)
            self.assertEqual(agente.run_task(cfg, 'crea x', Trace(tmp, quiet=True), False), 4)
            cfg.expected_files = {'x': 'hola'}
            self.assertEqual(agente.run_task(cfg, 'crea x', Trace(tmp, quiet=True), False), 3)
            (Path(tmp) / 'x').write_text('hola')
            self.assertEqual(agente.run_task(cfg, 'crea x', Trace(tmp, quiet=True), False), 0)

    def test_large_process_output_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, output, truncated = run_bounded([sys.executable, '-c', "print('x'*1000000)"], tmp, 3, 4096)
            self.assertTrue(truncated)
            self.assertLessEqual(len(output.encode()), 4096)

    def test_shell_uses_correct_flags(self):
        self.assertEqual(shell_command('echo ok', 'bash'), ['bash', '-c', 'echo ok'])
        self.assertEqual(shell_command('echo ok', 'cmd.exe')[-2:], ['/c', 'echo ok'])

    def test_private_network_is_blocked_and_explicit_opt_in_works(self):
        with self.assertRaisesRegex(ValueError, 'bloqueado'):
            validate_url('http://127.0.0.1:8080')
        validate_url('http://127.0.0.1:8080', True)
        with self.assertRaises(ValueError):
            validate_url('https://user:secret@example.com')

    def test_mcp_adapter_through_registry(self):
        from test_extensions import SERVER
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'fixture.py'
            script.write_text(SERVER)
            schema = {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}
            extra = mcp_tools([{'name': 'fixture', 'command': [sys.executable, '-u', str(script)], 'tools': [{'name': 'echo', 'inputSchema': schema}]}])
            registry = Registry(ToolContext(workspace=tmp, budget=Budget()), extra=extra)
            result = registry.run('mcp_fixture_echo', {'text': 'fixture checked'})
            self.assertTrue(result.ok, result.content)
            self.assertEqual(result.content, 'fixture checked')
            cfg = Config(read_only=True)
            self.assertEqual(cfg.action_for('mcp_fixture_echo', json.dumps({'text': 'x'})), 'deny')
