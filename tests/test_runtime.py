import json
from pathlib import Path
import tempfile
import unittest

from harness.config import Config
from harness.llm import LLMReply
from harness.loop import run
from harness.memory import Compressor, Memory
from harness.trace import Trace


class Model:
    model = 'fixture'

    def chat(self, *args, **kwargs):
        return LLMReply(content='Hecho.', output_tokens=3)


class RuntimeTests(unittest.TestCase):
    def test_no_evidence_is_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run(Config(workspace=tmp, web=False), 'crea x.txt', Trace(tmp, quiet=True), False, llm=Model())
            self.assertEqual(result.status, 'unverified')

    def test_explicit_output_contract_checks_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(workspace=tmp, web=False, expected_files={'x.txt': 'hola'})
            (Path(tmp) / 'x.txt').write_text('incorrecto')
            result = run(cfg, 'crea x.txt', Trace(tmp, quiet=True), False, llm=Model())
            self.assertEqual(result.status, 'incomplete')
            (Path(tmp) / 'x.txt').write_text('hola')
            result = run(cfg, 'crea x.txt', Trace(tmp, quiet=True), False, llm=Model())
            self.assertEqual(result.status, 'completed')

    def test_recent_memory_survives_growth(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(str(Path(tmp) / 'memory.md'), max_chars=300)
            memory.append('a' * 1000)
            memory.append('RECENT_NOTE')
            self.assertIn('RECENT_NOTE', memory.read())
            self.assertLessEqual(len(memory.read()), 300)

    def test_compressor_counts_arguments_and_preserves_task(self):
        comp = Compressor(2048, .85, 14, 4000)
        history = [{'role': 'user', 'content': 'ORIGINAL_TASK'},
                   {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': 'write_file', 'arguments': {'content': 'x' * 50000}}}]},
                   {'role': 'tool', 'content': 'written', 'tool_name': 'write_file'}]
        result = comp.build('prefix', history, schemas=[{'name': 'fixture'}])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), comp.budget_chars)
        self.assertIn('ORIGINAL_TASK', json.dumps(result))
        # Do not leave an observation after discarding its call.
        roles = [m['role'] for m in result]
        if 'tool' in roles:
            self.assertIn('assistant', roles)
