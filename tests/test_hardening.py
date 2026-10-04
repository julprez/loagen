import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.config import Config, Rule
from harness.permissions import PermissionGate
from harness.tools import Registry, ToolContext
from harness.trace import Trace


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.trace = Trace(str(self.root / 'traces'), quiet=True)

    def test_read_only_beats_allow_at_gate(self):
        cfg = Config(read_only=True, rules=[Rule('write_file', '.*', 'allow')])
        self.assertFalse(PermissionGate(cfg, self.trace, False).check('write_file', {'path': 'x'})[0])

    def test_read_only_preserves_secret_denies(self):
        cfg = Config(read_only=True, rules=[Rule('read_file', 'secret', 'deny')])
        self.assertFalse(PermissionGate(cfg, self.trace, False).check('read_file', {'path': 'secret'})[0])

    def test_single_approval_is_honored(self):
        gate = PermissionGate(Config(), self.trace)
        with patch('sys.stdin.isatty', return_value=True), patch.object(gate, '_prompt', return_value='allow'):
            self.assertTrue(gate.check('write_file', {'path': 'x'})[0])

    def test_shell_composition_does_not_inherit_prefix_allow(self):
        cfg = Config(rules=[Rule('bash', r'^ls\b', 'allow')])
        gate = PermissionGate(cfg, self.trace, False)
        for command in ('ls; echo x > x', 'ls > x', 'ls $(touch x)', 'ls && touch x'):
            self.assertFalse(gate.check('bash', {'command': command})[0], command)

    def test_glob_parent_escape_is_denied(self):
        registry = Registry(ToolContext(workspace=str(self.workspace)))
        out, error = registry.run('glob', {'pattern': '../*'})
        self.assertTrue(error, out)

    def test_symlink_escape_is_denied_for_read_write_and_search(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'secret.txt').write_text('fixture')
        link = self.workspace / 'link'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            # Windows without symlink privileges: test the canonical escape directly.
            self.assertFalse(os.path.commonpath([str(outside), str(self.workspace)]) == str(self.workspace))
            return
        registry = Registry(ToolContext(workspace=str(self.workspace)))
        for name, args in [('read_file', {'path': 'link/secret.txt'}),
                           ('write_file', {'path': 'link/new.txt', 'content': 'x'})]:
            out, error = registry.run(name, args)
            self.assertTrue(error, out)
        self.assertFalse((outside / 'new.txt').exists())
        out, _ = registry.run('glob', {'pattern': '**/*.txt'})
        self.assertNotIn('secret.txt', out)

    def test_failed_write_is_not_a_verified_fact(self):
        self.trace.emit('tool_call', call_id='1', tool='write_file', args={'path': 'missing.txt'})
        self.trace.emit('tool_error', call_id='1', tool='write_file', is_error=True)
        self.assertEqual(self.trace.facts()['files'], [])

    def test_argument_types_and_ranges_are_validated(self):
        registry = Registry(ToolContext(workspace=str(self.workspace)))
        for args in ({'path': None}, {'path': []}, {'path': 'x', 'limit': -1}):
            out, error = registry.run('read_file', args)
            self.assertTrue(error, out)
            self.assertNotIn('TypeError', out)
