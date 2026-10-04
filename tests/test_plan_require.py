"""`plan_require`: exigir el plan de verdad, con tope de rechazos.

Se prueba el bucle completo con un modelo falso que se niega a trabajar, para
comprobar los dos caminos: rechazo mientras queda presupuesto y cierre avisando
cuando se agota (sin pelearse indefinidamente con el modelo).
"""

from __future__ import annotations  # noqa: F407  (placeholder guard)

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.config import Config  # noqa: E402
from harness.llm import LLMReply  # noqa: E402
from harness.loop import run  # noqa: E402
from harness.trace import Trace  # noqa: E402

PLAN_JSON = json.dumps({"tasks": [
    {"id": "1", "task": "crear la carpeta", "depends_on": []},
    {"id": "2", "task": "escribir el fichero", "depends_on": ["1"]},
]})


class StubbornModel:
    """Devuelve el plan y después solo prosa, sin llamar a ninguna herramienta."""

    def __init__(self, answer="Ya está todo hecho."):
        self.model = "fake"
        self.answer = answer
        self.calls = 0

    def chat(self, messages, tools=None, tool_params=None):
        self.calls += 1
        if tools is None:  # la llamada de planificación no lleva herramientas
            return LLMReply(content=PLAN_JSON, output_tokens=20)
        return LLMReply(content=self.answer, output_tokens=8)


class WorkingModel:
    """Marca los dos pasos del plan y después cierra."""

    def __init__(self):
        self.model = "fake"
        self.calls = 0

    def chat(self, messages, tools=None, tool_params=None):
        self.calls += 1
        if tools is None:
            return LLMReply(content=PLAN_JSON, output_tokens=20)
        if self.calls == 2:
            return LLMReply(content="", output_tokens=12, tool_calls=[{
                "id": "c1", "type": "function",
                "function": {"name": "plan_update",
                             "arguments": {"index": "1,2", "status": "done"}},
            }])
        return LLMReply(content="Terminado: plan completo.", output_tokens=10)


class PlanRequireTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, **kw):
        cfg = Config(workspace=self.tmp.name, plan=True, plan_require=True, max_steps=8)
        cfg.plan_max_nudges = 1
        cfg.plan_hard_stops = 2
        cfg.cerebro_root = ""
        cfg.web = False
        for key, value in kw.items():
            setattr(cfg, key, value)
        return cfg

    def test_rejects_until_the_hard_stop_then_closes_with_a_warning(self):
        trace = Trace(os.path.join(self.tmp.name, "traces"), quiet=True)
        model = StubbornModel()
        result = run(self._cfg(), "haz el trabajo", trace, interactive=False, llm=model)

        rechazos = [e for e in trace.events
                    if e["kind"] == "plan_guard" and e.get("mode") == "rechazo"]
        avisos = [e for e in trace.events
                  if e["kind"] == "plan_guard" and e.get("mode") == "aviso"]
        self.assertEqual(len(avisos), 1)      # plan_max_nudges
        self.assertEqual(len(rechazos), 2)    # plan_hard_stops
        # Al agotar el tope, cierra avisando de que quedó incompleto.
        self.assertEqual(trace.incomplete_plan(), ["1", "2"])
        self.assertEqual(result.stopped, "final")
        # 1 llamada de planificación + 4 pasos de bucle (aviso, 2 rechazos y el cierre).
        # Si el presupuesto del plan se filtrara al de reintentos, serían 6 pasos.
        self.assertEqual(model.calls, 5)

    def test_without_plan_require_it_closes_after_the_nudges(self):
        trace = Trace(os.path.join(self.tmp.name, "traces"), quiet=True)
        run(self._cfg(plan_require=False), "haz el trabajo", trace,
            interactive=False, llm=StubbornModel())

        self.assertEqual(
            [e for e in trace.events
             if e["kind"] == "plan_guard" and e.get("mode") == "rechazo"], []
        )
        self.assertIsNone(trace.incomplete_plan())

    def test_completing_the_plan_closes_cleanly(self):
        trace = Trace(os.path.join(self.tmp.name, "traces"), quiet=True)
        result = run(self._cfg(), "haz el trabajo", trace, interactive=False,
                     llm=WorkingModel())

        self.assertEqual(result.stopped, "final")
        self.assertEqual(result.answer, "Terminado: plan completo.")
        self.assertIsNone(trace.incomplete_plan())
        self.assertEqual([e for e in trace.events if e["kind"] == "plan_guard"], [])


if __name__ == "__main__":
    unittest.main()