import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agente
from harness import bootstrap
from harness.config import load_config
from harness.storage.trace import Trace


class DistributionTests(unittest.TestCase):
    def test_init_force_repairs_malformed_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rules.toml'
            path.write_text('[broken')
            with patch.object(bootstrap, 'detect_ollama', return_value=([], 'offline')), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(agente.main(['--init', '--force', '-w', tmp]), 0)
            self.assertEqual(load_config(tmp).default_action, 'ask')

    def test_init_honors_explicit_rules_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config' / 'custom.toml'
            with patch.object(bootstrap, 'detect_ollama', return_value=([], 'offline')), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(agente.main(['--init', '--rules', str(path), '-w', tmp]), 0)
            self.assertTrue(path.is_file())
            self.assertFalse((Path(tmp) / 'rules.toml').exists())

    def test_trace_does_not_store_written_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = Trace(tmp, quiet=True)
            trace.emit('tool_call', tool='write_file', args={'path': 'x', 'content': 'SECRET_FIXTURE'})
            trace.emit('tool_result', output='SECRET_FIXTURE')
            self.assertNotIn('SECRET_FIXTURE', Path(trace.path).read_text())
            self.assertNotEqual(trace.path, Trace(tmp, quiet=True).path)

    def test_capture_cannot_accept_plan_marking_without_reading(self):
        path = Path(__file__).resolve().parents[1] / 'docs' / 'capturas' / 'generar.py'
        spec = importlib.util.spec_from_file_location('capture_fixture', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertFalse(module.plan_task_is_good('plan (2 pasos)\nplan_update\nverificado por el harness', '.'))
