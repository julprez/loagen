"""Selección de herramientas por tarea: recortar el prompt sin romper la tarea.

Dos contratos que estos tests fijan:

1. **Lo declarado se puede usar.** El perfil se aplica construyendo el registro con
   `only=`, así que no hay herramientas declaradas que devuelvan "no existe".
2. **Ante la duda, catálogo completo.** La selección es una optimización; una tarea
   ambigua no puede quedarse sin una herramienta que necesitaba.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import toolset  # noqa: E402
from harness.config import Config  # noqa: E402
from harness.llm import LLMReply  # noqa: E402
from harness.loop import run  # noqa: E402
from harness.tools import Registry, ToolContext, build_registry  # noqa: E402

# Catálogo completo tal como lo ve la selección: todas las herramientas conocidas, no
# las que sobreviven a un contexto concreto (sin plan, sin cerebro, sin web).
FULL = set(build_registry())


class ClassifyTests(unittest.TestCase):
    def test_examples_hit_their_own_profile(self):
        for profile, example in toolset.EXAMPLES.items():
            chosen = toolset.classify(example)
            self.assertIsNotNone(chosen, f"{profile}: {example}")
            self.assertIn(profile, chosen.split("+"), f"{profile}: {example}")

    def test_code_task_picks_code_tools(self):
        chosen = toolset.selection(
            "mira quién llama a load_config y refactoriza la función",
            FULL, FULL,
        )
        self.assertIn("cerebro_defs", chosen)
        self.assertIn("cerebro_callers", chosen)
        self.assertNotIn("web_search", chosen)
        self.assertNotIn("wiki", chosen)
        self.assertLess(len(chosen), len(FULL))

    def test_research_task_picks_browser_and_sources(self):
        chosen = toolset.selection(
            "busca en internet la última versión de Python y cita las fuentes",
            FULL, FULL,
        )
        self.assertIn("web_search", chosen)
        self.assertIn("fetch_url", chosen)
        self.assertIn("fuentes", chosen)
        self.assertNotIn("cerebro_defs", chosen)

    def test_ops_task_keeps_bash_and_cerebro(self):
        chosen = toolset.selection(
            "ejecuta los tests, compila y reinicia el servicio", FULL, FULL)
        self.assertIn("bash", chosen)
        self.assertIn("cerebro_buscar", chosen)
        self.assertNotIn("wiki", chosen)

    def test_mixed_code_and_research_keeps_both_groups(self):
        chosen = toolset.selection(
            "busca en la web cómo funciona el endpoint y refactoriza la función",
            FULL, FULL,
        )
        self.assertIn("web_search", chosen)
        self.assertIn("cerebro_callers", chosen)

    def test_ambiguous_task_gets_everything(self):
        for task in ("haz lo que puedas", "mira esto", "", "   "):
            self.assertIsNone(toolset.selection(task, FULL, FULL), task)

    def test_file_extension_alone_means_code(self):
        self.assertEqual(toolset.classify("revisa main.go"), "code")
        self.assertEqual(toolset.classify("arregla utils.py"), "code")

    def test_plain_conversation_stays_full(self):
        """Sin señal clara no se recorta: una tarea mal clasificada cuesta más."""
        self.assertIsNone(toolset.classify("¿qué hora es?"))


class SelectionSafetyTests(unittest.TestCase):
    def test_core_tools_are_always_present(self):
        for profile, example in toolset.EXAMPLES.items():
            chosen = toolset.selection(example, FULL, FULL)
            self.assertTrue(toolset.CORE <= chosen, profile)

    def test_coordination_is_always_present(self):
        chosen = toolset.selection("busca en la web y cita las fuentes", FULL, FULL)
        self.assertIn("plan_update", chosen)
        self.assertIn("subagent", chosen)

    def test_never_selects_a_tool_the_context_removed(self):
        available = FULL - {"web_search", "wiki", "fetch_url", "fuentes"}
        chosen = toolset.selection("busca en la web y cita las fuentes",
                                   available, available)
        self.assertTrue(chosen is None or chosen <= available)

    def test_small_catalogue_is_left_alone(self):
        small = {"read_file", "write_file", "edit_file", "mkdir", "list_dir", "glob"}
        self.assertIsNone(toolset.selection("refactoriza la función", small, small))

    def test_selection_is_smaller_than_the_full_catalogue(self):
        chosen = toolset.selection("ejecuta los tests", FULL, FULL)
        self.assertLess(len(chosen), len(FULL))
        self.assertGreaterEqual(len(chosen), 4)


class RegistryOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_limits_the_catalogue(self):
        ctx = ToolContext(workspace=self.tmp.name)
        reg = Registry(ctx, only={"read_file", "bash"})
        self.assertEqual(set(reg.tools), {"read_file", "bash"})
        out, err = reg.run("list_dir", {"path": "."})
        self.assertTrue(err)
        self.assertIn("no existe", out)

    def test_only_none_is_the_whole_catalogue(self):
        reg = Registry(ToolContext(workspace=self.tmp.name))
        self.assertGreater(len(reg.tools), 6)


class ToolsetLoopTests(unittest.TestCase):
    """El bucle decide ANTES de construir el registro: lo no enviado no existe."""

    class FakeModel:
        def __init__(self):
            self.model = "fake"
            self.seen_tools: list[list[str]] = []

        def chat(self, messages, tools=None, tool_params=None):
            if tools is None:
                return LLMReply(content="", output_tokens=5)
            self.seen_tools.append([t["function"]["name"] for t in tools])
            return LLMReply(content="Resumen sin herramientas.", output_tokens=6)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, **kw):
        cfg = Config(workspace=self.tmp.name, plan=False, web=True, max_steps=3)
        cfg.cerebro_root = ""
        for key, value in kw.items():
            setattr(cfg, key, value)
        return cfg

    def test_research_task_sends_fewer_tools_than_the_catalogue(self):
        from harness.trace import Trace

        trace = Trace(os.path.join(self.tmp.name, "traces"), quiet=True)
        model = self.FakeModel()
        run(self._cfg(), "busca en internet la última versión de Python y cita las fuentes",
            trace, interactive=False, llm=model)

        sent = set(model.seen_tools[0])
        self.assertIn("web_search", sent)
        self.assertIn("fuentes", sent)
        self.assertNotIn("cerebro_defs", sent)
        events = [e for e in trace.events if e["kind"] == "toolset"]
        self.assertEqual(len(events), 1)
        self.assertLess(events[0]["sent"], events[0]["full"])

    def test_ambiguous_task_sends_the_full_catalogue(self):
        from harness.trace import Trace

        trace = Trace(os.path.join(self.tmp.name, "traces"), quiet=True)
        model = self.FakeModel()
        run(self._cfg(), "haz lo que puedas", trace, interactive=False, llm=model)

        events = [e for e in trace.events if e["kind"] == "toolset"]
        self.assertEqual(events[0]["sent"], events[0]["full"])
        self.assertEqual(events[0]["groups"], ["completo"])

    def test_disabling_selection_sends_everything(self):
        from harness.trace import Trace

        trace = Trace(os.path.join(self.tmp.name, "traces"), quiet=True)
        model = self.FakeModel()
        run(self._cfg(tool_selection=False),
            "busca en internet y cita las fuentes", trace, interactive=False, llm=model)

        events = [e for e in trace.events if e["kind"] == "toolset"]
        self.assertEqual(events[0]["sent"], events[0]["full"])


if __name__ == "__main__":
    unittest.main()