"""Pruebas del harness (solo biblioteca estándar).

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

from harness import config as config_mod  # noqa: E402
from harness import llm as llm_mod  # noqa: E402
from harness.config import Config, Rule, load_config, version_notice  # noqa: E402
from harness.memory import Compressor, Memory  # noqa: E402
from harness.tools import Registry, ToolContext, resolve_path  # noqa: E402


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx = ToolContext(workspace=self.tmp.name, max_output=2000)
        self.reg = Registry(self.ctx)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mkdir_then_write_then_read(self):
        out, err = self.reg.run("mkdir", {"path": "demo"})
        self.assertFalse(err, out)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp.name, "demo")))

        out, err = self.reg.run("write_file", {"path": "demo/saludo.txt", "content": "hola mundo"})
        self.assertFalse(err, out)

        out, err = self.reg.run("read_file", {"path": "demo/saludo.txt"})
        self.assertFalse(err)
        self.assertIn("hola mundo", out)

    def test_write_file_on_directory_is_clear_error(self):
        self.reg.run("mkdir", {"path": "demo"})
        out, err = self.reg.run("write_file", {"path": "demo", "content": "x"})
        self.assertTrue(err)
        self.assertIn("CARPETA", out)

    def test_read_file_on_directory_suggests_list_dir(self):
        self.reg.run("mkdir", {"path": "demo"})
        out, err = self.reg.run("read_file", {"path": "demo"})
        self.assertTrue(err)
        self.assertIn("list_dir", out)

    def test_path_escape_is_blocked(self):
        with self.assertRaises(PermissionError):
            resolve_path(self.ctx, "../../etc/passwd")
        out, err = self.reg.run("write_file", {"path": "/tmp/escape.txt", "content": "x"})
        self.assertTrue(err)
        self.assertIn("fuera del workspace", out)

    def test_missing_required_argument(self):
        out, err = self.reg.run("read_file", {})
        self.assertTrue(err)
        self.assertIn("faltan parámetros", out)

    def test_unknown_tool_lists_available(self):
        out, err = self.reg.run("inventada", {})
        self.assertTrue(err)
        self.assertIn("mkdir", out)

    def test_edit_file(self):
        self.reg.run("write_file", {"path": "a.py", "content": "x = 1\n"})
        out, err = self.reg.run("edit_file", {"path": "a.py", "old": "x = 1", "new": "x = 2"})
        self.assertFalse(err, out)
        out, _ = self.reg.run("read_file", {"path": "a.py"})
        self.assertIn("x = 2", out)

    def test_edit_file_reports_when_text_absent(self):
        self.reg.run("write_file", {"path": "a.py", "content": "x = 1\n"})
        out, err = self.reg.run("edit_file", {"path": "a.py", "old": "nope", "new": "y"})
        self.assertTrue(err)
        self.assertIn("no aparece", out)

    def test_glob_and_grep(self):
        self.reg.run("write_file", {"path": "src/main.go", "content": "package main\n"})
        self.reg.run("write_file", {"path": "src/util.py", "content": "def hola():\n    pass\n"})
        out, err = self.reg.run("glob", {"pattern": "**/*.py"})
        self.assertFalse(err)
        self.assertIn("src/util.py", out)
        self.assertNotIn("main.go", out)

        out, err = self.reg.run("grep", {"pattern": "def hola"})
        self.assertFalse(err)
        self.assertIn("src/util.py", out)

    def test_path_with_newlines_is_rejected(self):
        """Medido: el modelo metió un script entero en `path` y se creó una carpeta con
        saltos de línea en el nombre, imposible de borrar de forma cómoda."""
        script = 'demo\nmkdir demo/hola.txt\nwrite_file demo/hola.txt "hola'
        out, err = self.reg.run("mkdir", {"path": script})
        self.assertTrue(err)
        self.assertIn("saltos de línea", out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "demo")))

        out, err = self.reg.run("write_file", {"path": script, "content": "x"})
        self.assertTrue(err)
        self.assertIn("saltos de línea", out)

    def test_path_with_a_tab_is_rejected(self):
        out, err = self.reg.run("list_dir", {"path": "src\tname"})
        self.assertTrue(err)
        self.assertIn("saltos de línea", out)

    def test_bash_reports_exit_code(self):
        out, err = self.reg.run("bash", {"command": "exit 3"})
        self.assertTrue(err)
        self.assertIn("exit 3", out)

    def test_bash_runs_in_workspace(self):
        command = f'"{sys.executable}" -c "import os; print(os.getcwd())"'
        out, err = self.reg.run("bash", {"command": command})
        self.assertFalse(err)
        self.assertEqual(out.strip(), os.path.realpath(self.tmp.name))

    def test_bash_passes_the_configured_shell_as_executable(self):
        """`shell` de rules.toml llega a subprocess como `executable` (vía para Windows)."""
        ctx = ToolContext(workspace=self.tmp.name, shell="bash")
        with mock.patch('harness.tools.run_bounded', return_value=(0, 'ok\n', False)) as run:
            out, err = Registry(ctx).run("bash", {"command": "echo ok"})
        self.assertFalse(err)
        self.assertEqual(out.strip(), "ok")
        self.assertEqual(run.call_args.args[0], ['bash', '-c', 'echo ok'])

    def test_bash_without_shell_uses_the_system_default(self):
        ctx = ToolContext(workspace=self.tmp.name)
        with mock.patch('harness.tools.run_bounded', return_value=(0, '', False)) as run:
            Registry(ctx).run("bash", {"command": "echo"})
        self.assertEqual(run.call_args.args[0][-1], 'echo')
        self.assertIn(run.call_args.args[0][-2], ('-c', '/c'))

    def test_bash_reports_a_bad_shell_instead_of_crashing(self):
        ctx = ToolContext(workspace=self.tmp.name, shell="no-existe-shell-xyz")
        out, err = Registry(ctx).run("bash", {"command": "echo hola"})
        self.assertTrue(err)
        self.assertIn("no se pudo usar el shell", out)
        self.assertIn("rules.toml", out)


class PermissionTests(unittest.TestCase):
    def _cfg(self, rules, **kw):
        cfg = Config(workspace="/tmp")
        cfg.rules = rules
        for key, value in kw.items():
            setattr(cfg, key, value)
        return cfg

    def test_default_tiers(self):
        cfg = self._cfg([])
        self.assertEqual(cfg.action_for("read_file", "/tmp/x"), "allow")
        self.assertEqual(cfg.action_for("bash", "rm -rf /"), "ask")

    def test_explicit_deny_wins(self):
        cfg = self._cfg([Rule("bash", r"rm\s+-rf", "deny")])
        self.assertEqual(cfg.action_for("bash", "rm -rf /"), "deny")

    def test_read_only_denies_mutating(self):
        cfg = self._cfg([], read_only=True)
        self.assertEqual(cfg.action_for("bash", "ls"), "deny")
        self.assertEqual(cfg.action_for("read_file", "/tmp/x"), "allow")

    def test_rules_file_is_parsed(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rules.example.toml")
        cfg = load_config(workspace="/tmp", config_path=path)
        self.assertEqual(cfg.model, "qwen2.5:1.5b-instruct")
        self.assertEqual(cfg.action_for("bash", "rm -rf /"), "deny")
        self.assertEqual(cfg.action_for("bash", "ls -la"), "ask")


class LLMParsingTests(unittest.TestCase):
    def test_parses_bare_json_object(self):
        calls = llm_mod._extract_from_text('{"name": "list_dir", "arguments": {"path": "."}}')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "list_dir")
        self.assertEqual(calls[0]["function"]["arguments"], {"path": "."})

    def test_parses_fenced_and_tagged(self):
        for text in (
            '```json\n{"name":"glob","arguments":{"pattern":"**/*.py"}}\n```',
            '<tool_call>{"tool":"glob","parameters":{"pattern":"**/*.py"}}</tool_call>',
            'Claro:\n{"function":"glob","args":{"pattern":"**/*.py"}}',
        ):
            calls = llm_mod._extract_from_text(text)
            self.assertEqual(len(calls), 1, text)
            self.assertEqual(calls[0]["function"]["name"], "glob")

    def test_normalizes_openai_shape(self):
        call = llm_mod._normalize_call(
            {"function": {"name": "bash", "arguments": '{"command": "ls"}'}}
        )
        self.assertEqual(call["function"]["arguments"], {"command": "ls"})

    def test_plain_prose_is_not_a_tool_call(self):
        self.assertEqual(llm_mod._extract_from_text("Ya está todo hecho."), [])
        # "bash es peligroso" empieza por el nombre de una herramienta, pero es prosa:
        # el parser posicional exige un separador tras el nombre.
        order = {"bash": ["command"], "write_file": ["path", "content"]}
        self.assertEqual(llm_mod._extract_from_text("bash es peligroso, evítalo.", order), [])

    def test_parses_positional_dialect(self):
        order = {"bash": ["command"], "write_file": ["path", "content"]}
        calls = llm_mod._extract_from_text(
            'write_file primos.py "print(1)"', order
        )
        self.assertEqual(len(calls), 1)
        fn = calls[0]["function"]
        self.assertEqual(fn["name"], "write_file")
        self.assertEqual(fn["arguments"], {"path": "primos.py", "content": "print(1)"})

        calls = llm_mod._extract_from_text("bash ls -la", order)
        self.assertEqual(calls[0]["function"]["arguments"], {"command": "ls -la"})

        # El contenido entrecomillado puede llevar comas y puntos sin ser prosa:
        calls = llm_mod._extract_from_text(
            'write_file primos.py "print(2, 3, 5, 7)"', order
        )
        self.assertEqual(calls[0]["function"]["arguments"]["content"], "print(2, 3, 5, 7)")

    def test_parses_call_syntax_dialect(self):
        order = {"write_file": ["path", "content"], "mkdir": ["path"]}
        calls = llm_mod._extract_from_text('write_file("a.txt", "hola")', order)
        self.assertEqual(calls[0]["function"]["arguments"], {"path": "a.txt", "content": "hola"})

        calls = llm_mod._extract_from_text('mkdir("demo")', order)
        self.assertEqual(calls[0]["function"]["arguments"], {"path": "demo"})

        calls = llm_mod._extract_from_text('write_file(path="a.txt", content="hola")', order)
        self.assertEqual(calls[0]["function"]["arguments"], {"path": "a.txt", "content": "hola"})

        # No se ejecuta código: una expresión arbitraria no produce llamada.
        self.assertEqual(
            llm_mod._extract_from_text('write_file(open("x").read(), "y")', order), []
        )

    def test_positional_decodes_literal_newlines(self):
        order = {"write_file": ["path", "content"]}
        calls = llm_mod._extract_from_text(
            'write_file a.py "import os\\nprint(1)\\n"', order
        )
        content = calls[0]["function"]["arguments"]["content"]
        self.assertIn("\n", content)
        self.assertNotIn("\\n", content)
        self.assertEqual(content, "import os\nprint(1)\n")


class PlanTests(unittest.TestCase):
    """Task Graph: parseo tolerante, orden topológico y saneo del grafo."""

    def test_parses_json_with_dependencies(self):
        from harness.plan import parse_plan

        plan = parse_plan(
            '{"tasks": [{"id": "1", "task": "crear carpeta", "depends_on": []},'
            ' {"id": "2", "task": "escribir fichero", "depends_on": ["1"]}]}'
        )
        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.steps[1].depends_on, ["1"])

    def test_parses_other_key_names_and_prose_wrapper(self):
        from harness.plan import parse_plan

        plan = parse_plan(
            'Claro, aquí está:\n```json\n{"steps": [{"index": 1, "description": "a"},'
            ' {"index": 2, "description": "b"}]}\n```'
        )
        self.assertEqual([s.text for s in plan.steps], ["a", "b"])
        self.assertEqual([s.id for s in plan.steps], ["1", "2"])

    def test_falls_back_to_numbered_list(self):
        from harness.plan import parse_plan

        plan = parse_plan("1. crear la carpeta\n2. escribir el fichero\n3. verificar")
        self.assertEqual(len(plan.steps), 3)
        self.assertEqual(plan.steps[0].text, "crear la carpeta")
        self.assertEqual(plan.steps[0].depends_on, [])

    def test_single_item_is_not_a_plan(self):
        from harness.plan import parse_plan

        # Un solo paso no es un task graph: mejor seguir sin plan que fingir uno.
        self.assertIsNone(parse_plan("1. hacer la tarea"))
        self.assertIsNone(parse_plan('{"tasks": [{"id": "1", "task": "hacer la tarea"}]}'))
        self.assertIsNone(parse_plan("no hay nada que parsear"))
        self.assertIsNone(parse_plan(""))

    def test_two_steps_are_enough(self):
        from harness.plan import parse_plan

        plan = parse_plan('{"tasks": [{"id": "1", "task": "a"}, {"id": "2", "task": "b"}]}')
        self.assertEqual(len(plan.steps), 2)

    def test_unknown_dependencies_are_dropped(self):
        from harness.plan import parse_plan

        plan = parse_plan(
            '{"tasks": [{"id": "1", "task": "a", "depends_on": ["99"]},'
            ' {"id": "2", "task": "b", "depends_on": ["1", "7"]}]}'
        )
        self.assertEqual(plan.steps[0].depends_on, [])
        self.assertEqual(plan.steps[1].depends_on, ["1"])

    def test_cycles_are_broken(self):
        from harness.plan import parse_plan

        plan = parse_plan(
            '{"tasks": [{"id": "a", "task": "x", "depends_on": ["b"]},'
            ' {"id": "b", "task": "y", "depends_on": ["a"]}]}'
        )
        # Sin saneo, order() se quedaria atascada; debe haber un orden valido.
        self.assertEqual(len(plan.order()), 2)
        self.assertTrue(all(not s.depends_on for s in plan.steps))

    def test_order_respects_declaration_out_of_order(self):
        from harness.plan import parse_plan

        plan = parse_plan(
            '{"tasks": [{"id": "2", "task": "segundo", "depends_on": ["1"]},'
            ' {"id": "1", "task": "primero", "depends_on": []}]}'
        )
        self.assertEqual([s.id for s in plan.order()], ["1", "2"])

    def test_next_step_waits_for_dependencies(self):
        from harness.plan import parse_plan

        plan = parse_plan(
            '{"tasks": [{"id": "1", "task": "a", "depends_on": []},'
            ' {"id": "2", "task": "b", "depends_on": ["1"]}]}'
        )
        self.assertEqual(plan.next_step().id, "1")
        plan.mark("1", "done")
        self.assertEqual(plan.next_step().id, "2")
        plan.mark("2", "done")
        self.assertIsNone(plan.next_step())
        self.assertEqual(plan.pending(), [])

    def test_mark_many_accepts_strings_and_lists(self):
        from harness.plan import parse_plan

        raw = ("{\"tasks\": [" + ",".join(
            f'{{"id": "{i}", "task": "paso {i}"}}' for i in range(1, 5)
        ) + "]}")
        plan = parse_plan(raw)
        self.assertEqual([s.id for s in plan.mark_many("1,2", "done")], ["1", "2"])
        self.assertEqual([s.id for s in plan.mark_many([3, 4], "done")], ["3", "4"])
        self.assertEqual(plan.pending(), [])
        # Un índice inexistente no rompe: se marca el resto.
        self.assertEqual([s.id for s in plan.mark_many("1, 99", "done")], ["1"])

    def test_mark_by_id_position_and_status_aliases(self):
        from harness.plan import parse_plan

        plan = parse_plan(
            '{"tasks": [{"id": "10", "task": "a"}, {"id": "20", "task": "b"}]}'
        )
        self.assertEqual(plan.mark("10", "hecho").status, "done")
        self.assertEqual(plan.mark(2, "en curso").id, "20")
        self.assertEqual(plan.steps[1].status, "doing")
        self.assertIsNone(plan.mark("999", "done"))

    def test_render_is_static_and_status_line_is_dynamic(self):
        from harness.plan import parse_plan

        plan = parse_plan(
            '{"tasks": [{"id": "1", "task": "a", "depends_on": []},'
            ' {"id": "2", "task": "b", "depends_on": ["1"]}]}'
        )
        before = plan.render()
        plan.mark("1", "done")
        # La estructura no cambia: es lo que permite mantener la cache de prefijo.
        self.assertEqual(before, plan.render())
        self.assertIn("1/2 hechos", plan.status_line())
        self.assertIn("siguiente: [2]", plan.status_line())

    def test_respects_max_steps(self):
        from harness.plan import parse_plan

        raw = "{\"tasks\": [" + ",".join(
            f'{{"id": "{i}", "task": "paso {i}"}}' for i in range(1, 12)
        ) + "]}"
        self.assertEqual(len(parse_plan(raw, max_steps=4).steps), 4)


class PlanPromptTests(unittest.TestCase):
    """El prompt lleva llaves JSON y la tarea puede llevar cualquier cosa."""

    def test_prompt_renders_and_keeps_json_braces(self):
        from harness.plan import plan_prompt

        prompt = plan_prompt("crea la carpeta", 5)
        self.assertIn('"tasks"', prompt)
        self.assertIn("Entre 2 y 5 pasos", prompt)
        self.assertIn("crea la carpeta", prompt)
        self.assertNotIn("__MAX_STEPS__", prompt)

    def test_task_is_appended_verbatim(self):
        from harness.plan import plan_prompt

        # Una tarea con llaves y $ no debe romper ni interpolarse.
        task = "ejecuta echo ${HOME} y usa {'a': 1}"
        prompt = plan_prompt(task, 6)
        self.assertTrue(prompt.endswith(task))


class PlanContradictionTests(unittest.TestCase):
    """Guard determinista: un plan que se refiere a OTROS ficheros que la tarea se tira."""

    def _plan(self, *texts):
        from harness.plan import Plan, PlanStep

        return Plan([PlanStep(str(i + 1), t) for i, t in enumerate(texts)])

    def _task(self, task, *steps):
        from harness.plan import contradicts_task

        return contradicts_task(task, self._plan(*steps))

    def test_the_measured_case_is_caught(self):
        problem = self._task("escribe en notas.txt el texto hola", "crear la carpeta demo",
                             "escribir demo/notes.txt")
        self.assertTrue(problem)
        self.assertIn("notas", problem)

    def test_the_same_file_is_fine(self):
        self.assertEqual(
            self._task("escribe en notas.txt el texto hola", "crear notas.txt",
                       "escribir notas.txt con hola"),
            "",
        )

    def test_a_derived_name_is_fine(self):
        """`calculadora` -> `tests/test_calculadora.py` es una derivación legítima."""
        self.assertEqual(
            self._task("crea calculadora.py con una función sumar",
                       "crear calculadora.py", "escribir tests/test_calculadora.py"),
            "",
        )

    def test_another_extension_is_fine(self):
        """Añadir un README a una tarea sobre `main.py` no contradice nada."""
        self.assertEqual(
            self._task("arregla main.py", "leer main.py", "escribir un README.md"),
            "",
        )

    def test_without_filenames_in_the_task_there_is_nothing_to_check(self):
        self.assertEqual(self._task("ordena el proyecto", "listar el directorio"), "")

    def test_version_numbers_are_not_files(self):
        """`python3.11` no es un fichero: la extensión tiene que empezar por letra."""
        self.assertEqual(
            self._task("actualiza el proyecto a python3.11", "instalar python3.12"), ""
        )


class MakePlanTests(unittest.TestCase):
    """La planificación es auxiliar: nunca debe tumbar la ejecución."""

    class _Reply:
        def __init__(self, content):
            self.content = content
            self.seconds = 0.1

    class _Engine:
        def __init__(self, behaviour):
            self.behaviour = behaviour
            self.model = "fake"

        def chat(self, messages, tools=None, tool_params=None):
            if isinstance(self.behaviour, Exception):
                raise self.behaviour
            return MakePlanTests._Reply(self.behaviour)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from harness.config import Config
        from harness.trace import Trace

        self.cfg = Config(workspace=self.tmp.name, plan=True)
        self.trace = Trace(os.path.join(self.tmp.name, "traces"), quiet=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_plan_is_built(self):
        from harness.loop import make_plan

        engine = self._Engine('{"tasks": [{"id": "1", "task": "a"}, {"id": "2", "task": "b"}]}')
        plan = make_plan(engine, self.cfg, self.trace, "tarea")
        self.assertEqual(len(plan.steps), 2)

    def test_garbage_degrades_to_no_plan(self):
        from harness.loop import make_plan

        for content in ("no sé", "", "1. solo un paso"):
            self.assertIsNone(make_plan(self._Engine(content), self.cfg, self.trace, "t"))

    def test_model_failure_degrades_to_no_plan(self):
        from harness.loop import make_plan

        self.assertIsNone(
            make_plan(self._Engine(RuntimeError("boom")), self.cfg, self.trace, "t")
        )
        self.assertTrue(any(e["kind"] == "plan_none" for e in self.trace.events))

    def test_plan_that_invents_other_files_is_discarded(self):
        """Medido: para «escribe en notas.txt el texto hola» el planificador devolvió
        «crear la carpeta demo» + «escribir demo/notes.txt» y el modelo hizo eso en vez
        de la tarea. Ese plan no se acepta."""
        from harness.loop import make_plan

        engine = self._Engine(
            '{"tasks": [{"id": "1", "task": "crear la carpeta demo"}, '
            '{"id": "2", "task": "escribir demo/notes.txt", "depends_on": ["1"]}]}'
        )
        plan = make_plan(engine, self.cfg, self.trace, "escribe en notas.txt el texto hola")
        self.assertIsNone(plan)
        reasons = [e.get("reason") for e in self.trace.events if e["kind"] == "plan_none"]
        self.assertIn("contradice la tarea", reasons)

    def test_plan_that_respects_the_task_is_kept(self):
        from harness.loop import make_plan

        engine = self._Engine(
            '{"tasks": [{"id": "1", "task": "crear notas.txt"}, '
            '{"id": "2", "task": "escribir hola en notas.txt", "depends_on": ["1"]}]}'
        )
        plan = make_plan(engine, self.cfg, self.trace, "escribe en notas.txt el texto hola")
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.steps), 2)


class PlanToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self):
        from harness.plan import parse_plan

        return parse_plan(
            '{"tasks": [{"id": "1", "task": "a", "depends_on": []},'
            ' {"id": "2", "task": "b", "depends_on": ["1"]}]}'
        )

    def test_plan_update_marks_progress(self):
        ctx = ToolContext(workspace=self.tmp.name, plan=self._plan())
        reg = Registry(ctx)
        out, err = reg.run("plan_update", {"index": "1", "status": "done"})
        self.assertFalse(err, out)
        self.assertIn("1/2 hechos", out)
        self.assertIn("plan_update", reg.tools)

    def test_plan_update_without_plan_is_rejected(self):
        ctx = ToolContext(workspace=self.tmp.name)
        reg = Registry(ctx)
        self.assertNotIn("plan_update", reg.tools)
        out, err = reg.run("plan_update", {"index": "1"})
        self.assertTrue(err)
        self.assertIn("no existe", out)

    def test_plan_update_with_bad_index_lists_steps(self):
        ctx = ToolContext(workspace=self.tmp.name, plan=self._plan())
        reg = Registry(ctx)
        out, err = reg.run("plan_update", {"index": "42"})
        self.assertTrue(err)
        self.assertIn("no existe ningún paso", out)
        self.assertIn("1, 2", out)

    def test_plan_update_marks_several_at_once(self):
        ctx = ToolContext(workspace=self.tmp.name, plan=self._plan())
        reg = Registry(ctx)
        out, err = reg.run("plan_update", {"index": "1,2", "status": "done"})
        self.assertFalse(err, out)
        self.assertIn("2/2 hechos", out)
        self.assertIn("completo", out)

    def test_subagent_requires_a_spawner(self):
        ctx = ToolContext(workspace=self.tmp.name)
        reg = Registry(ctx)
        self.assertNotIn("subagent", reg.tools)
        out, err = reg.run("subagent", {"task": "investiga"})
        self.assertTrue(err)

    def test_subagent_calls_the_spawner(self):
        seen = {}

        def spawn(task):
            seen["task"] = task
            return f"informe sobre {task}"

        ctx = ToolContext(workspace=self.tmp.name, spawn=spawn)
        reg = Registry(ctx)
        out, err = reg.run("subagent", {"task": "leer los ficheros"})
        self.assertFalse(err, out)
        self.assertEqual(seen["task"], "leer los ficheros")
        self.assertIn("informe sobre leer los ficheros", out)

    def test_subagent_failure_returns_as_observation(self):
        def spawn(task):
            raise RuntimeError("boom")

        ctx = ToolContext(workspace=self.tmp.name, spawn=spawn)
        reg = Registry(ctx)
        out, err = reg.run("subagent", {"task": "x"})
        self.assertTrue(err)
        self.assertIn("RuntimeError", out)


class SelfCorrectionTests(unittest.TestCase):
    """Guards de la capa OUTPUT: no aceptar lo que el modelo afirma sin evidencia."""

    def test_retry_after_error(self):
        from harness.loop import self_correction

        out = self_correction(True, "tarea", set(), "texto")
        self.assertEqual(out["cause"], "se rindió tras un error")

    def test_url_task_without_fetch_is_rejected(self):
        from harness.loop import self_correction

        task = "resume https://ollama.com"
        self.assertIsNotNone(self_correction(False, task, {"read_file"}, "Ollama es..."))
        # si ya descargó, se acepta
        self.assertIsNone(self_correction(False, task, {"fetch_url"}, "Ollama es..."))

    def test_claimed_execution_in_code_block_is_rejected(self):
        from harness.loop import self_correction

        answer = "Ya está:\n```bash\npython3 x.py\n```"
        out = self_correction(False, "sin url", set(), answer)
        self.assertEqual(out["cause"], "afirma haber ejecutado sin ejecutar")
        # si bash se usó de verdad, no se rechaza
        self.assertIsNone(self_correction(False, "sin url", {"bash"}, answer))

    def test_plan_with_pending_steps_rejects_the_answer(self):
        from harness.loop import self_correction
        from harness.plan import parse_plan

        plan = parse_plan(
            '{"tasks": [{"id": "1", "task": "a", "depends_on": []},'
            ' {"id": "2", "task": "b", "depends_on": ["1"]}]}'
        )
        out = self_correction(False, "tarea", {"bash"}, "ya está", plan=plan)
        self.assertTrue(out["plan"])
        self.assertIn("PLAN no está terminado", out["message"])
        self.assertIn("[1]", out["message"])
        # El guard SIEMPRE reporta el plan incompleto: quien decide entre aviso,
        # rechazo o cierre es el bucle (que conoce los presupuestos).
        self.assertTrue(
            self_correction(False, "tarea", {"bash"}, "ya está", plan=plan)["plan"]
        )
        # Con todos los pasos hechos, la respuesta se acepta.
        plan.mark("1", "done")
        plan.mark("2", "done")
        self.assertIsNone(self_correction(False, "tarea", {"bash"}, "ya está", plan=plan))

    def test_truncation_has_priority_over_the_plan(self):
        """Medido: una respuesta truncada Y con plan pendiente quedaba absorbida por el
        guard del plan y se aceptaba como final. El truncado manda."""
        from harness.loop import self_correction
        from harness.plan import parse_plan

        plan = parse_plan('{"tasks": [{"id": "1", "task": "a"}, {"id": "2", "task": "b"}]}')
        out = self_correction(False, "t", {"bash"}, "texto cortado", plan=plan,
                              truncated=True)
        self.assertIn("tope de tokens", out["cause"])
        self.assertNotIn("plan", out)

    def test_error_has_priority_over_the_plan(self):
        from harness.loop import self_correction
        from harness.plan import parse_plan

        plan = parse_plan('{"tasks": [{"id": "1", "task": "a"}, {"id": "2", "task": "b"}]}')
        out = self_correction(True, "tarea", set(), "texto", plan=plan)
        self.assertEqual(out["cause"], "se rindió tras un error")
        self.assertNotIn("plan", out)

    def test_degenerate_answer_is_rejected(self):
        """Caso real: el modelo repetía 'Creando el fichero ls13.py...' hasta el tope.

        Todas las líneas eran distintas, así que la señal NO es "líneas iguales": es
        que la generación se cortó contra num_predict.
        """
        from harness.loop import looks_degenerate, self_correction

        bucle = "\n".join(
            f"Creando la carpeta /tmp/x/utilidades/ls{n}/...\n\n"
            f"Creando el fichero /tmp/x/utilidades/ls{n}.py..."
            for n in range(3, 20)
        )
        # Las lineas del caso real eran TODAS distintas: no es un bucle literal.
        self.assertFalse(looks_degenerate(bucle))
        # Lo que sí es cierto y determinista es que la generacion se corto por el tope.
        out = self_correction(False, "tarea", {"bash"}, bucle, truncated=True)
        self.assertIn("tope de tokens", out["cause"])
        # Bucle literal (mismas lineas repetidas): se detecta sin necesidad del tope.
        literal = "\n".join(["Reintentando la conexión"] * 10)
        self.assertTrue(looks_degenerate(literal))
        out = self_correction(False, "tarea", {"bash"}, literal)
        self.assertIn("bucle", out["cause"])
        # Un resumen normal no dispara ningun guard.
        self.assertFalse(looks_degenerate("Hecho: los 10 primeros primos son 2, 3, 5."))
        self.assertFalse(looks_degenerate(""))
        self.assertIsNone(self_correction(False, "tarea", {"bash"}, "Hecho: 2, 3, 5"))
        # Un listado legitimo de 20 ficheros NO es un bucle.
        listado = "\n".join(f"fichero{i}.go: {i} lineas" for i in range(20))
        self.assertFalse(looks_degenerate(listado))

    def test_ignorance_triggers_the_browser(self):
        """El navegador existe para no aceptar "no sé" como respuesta final."""
        from harness.loop import self_correction

        for frase in (
            "No lo sé.",
            "No tengo esa información.",
            "No puedo acceder a internet.",
            "I don't know the answer.",
            "No estoy seguro de eso.",
        ):
            out = self_correction(False, "¿qué es X?", {"read_file"}, frase, web=True)
            self.assertIsNotNone(out, frase)
            self.assertIn("web_search", out["message"])
        # Si ya buscó, se acepta; y con la web desactivada no se fuerza nada.
        self.assertIsNone(
            self_correction(False, "t", {"web_search"}, "No lo sé", web=True)
        )
        self.assertIsNone(self_correction(False, "t", set(), "No lo sé", web=False))

    def test_ignorance_detection_does_not_fire_on_normal_answers(self):
        from harness.loop import self_correction

        for frase in (
            "El fichero no existe, lo creo ahora.",
            "He encontrado 3 resultados.",
            "No hay tests en este proyecto.",
        ):
            self.assertIsNone(self_correction(False, "t", {"bash"}, frase, web=True), frase)

    def test_normal_answer_is_accepted(self):
        from harness.loop import self_correction

        self.assertIsNone(self_correction(False, "tarea", {"bash"}, "Hecho: 2, 3, 5"))
        # un bloque de código documental sin comandos tampoco dispara el guard
        self.assertIsNone(
            self_correction(False, "tarea", set(), "El script es:\n```python\nprint(1)\n```")
        )


class SubagentReportTests(unittest.TestCase):
    """Un informe sin evidencia no vale como informe."""

    def test_error_report_starts_with_error(self):
        from harness.loop import format_subagent_report

        report = format_subagent_report("error", "Error hablando con Ollama: timed out", [])
        self.assertTrue(report.startswith("ERROR"))
        self.assertIn("timed out", report)

    def test_report_without_tools_is_a_failure(self):
        """Medido: un 1.5B devolvió un bloque de comandos sin ejecutar nada."""
        from harness.loop import format_subagent_report

        report = format_subagent_report("final", "```bash ls -lR ```", tools=[])
        self.assertTrue(report.startswith("ERROR"))
        self.assertIn("no usó ninguna herramienta", report)

    def test_ok_report_separates_verified_facts_from_the_summary(self):
        from harness.loop import format_subagent_report

        report = format_subagent_report("final", "hay 3 ficheros", ["grep", "read_file"])
        self.assertIn("INFORME DEL SUBAGENTE", report)
        self.assertIn("grep, read_file", report)
        self.assertIn("NO está verificado", report)
        self.assertIn("hay 3 ficheros", report)


class ModelOptionTests(unittest.TestCase):
    """El tope de tokens generados es la defensa contra bucles que bloquean la sesión."""

    def test_num_predict_is_sent_to_ollama(self):
        from harness.llm import Ollama

        engine = Ollama(host="http://127.0.0.1:11434", model="x", num_predict=256)
        self.assertEqual(engine._options()["num_predict"], 256)
        # 0 o negativo = sin tope explícito (no se envía la opción).
        self.assertNotIn("num_predict", Ollama(host="h", model="x", num_predict=0)._options())

    def test_num_predict_comes_from_rules(self):
        cfg = load_config(
            workspace="/tmp",
            config_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "rules.toml"),
        )
        self.assertGreater(cfg.num_predict, 0)
        self.assertGreater(cfg.plan_max_nudges, 1)


class IsolationTests(unittest.TestCase):
    """El subagente no debe contaminar el estado del padre."""

    def test_child_trace_is_separate(self):
        from harness.trace import Trace

        with tempfile.TemporaryDirectory() as tmp:
            parent = Trace(tmp)
            parent.emit("model_call", step=1)
            parent.emit("tool_error", tool="bash")
            self.assertTrue(parent.last_step_failed())

            child = parent.child("sub1")
            child.emit("model_call", step=1)
            self.assertFalse(child.last_step_failed())
            # La actividad del hijo no toca la del padre.
            self.assertTrue(parent.last_step_failed())
            self.assertNotEqual(parent.path, child.path)
            self.assertIn("sub1", child.path)


class MemoryTests(unittest.TestCase):
    def test_memory_persists_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Memory(os.path.join(tmp, "agent_memory.md"))
            mem.append("el proyecto usa Go")
            self.assertIn("el proyecto usa Go", mem.read())

    def test_compressor_trims_tool_output(self):
        comp = Compressor(num_ctx=100, threshold=0.9, keep_last=2, tool_output_max_chars=50)
        messages = [{"role": "tool", "content": "x" * 500, "tool_name": "bash"}]
        built = comp.build("prefix", messages)
        self.assertLess(len(built[-1]["content"]), 200)

    def test_compressor_summarizes_old_history(self):
        comp = Compressor(num_ctx=200, threshold=0.5, keep_last=2, tool_output_max_chars=100)
        history = [{"role": "user", "content": f"paso {i} " + "y" * 50} for i in range(10)]
        built = comp.build("prefix", history)
        self.assertLess(len(built), len(history) + 1)
        self.assertTrue(any("Resumen comprimido" in m.get("content", "") for m in built))
        self.assertEqual(comp.compressed_rounds, 1)


class ConfigShellTests(unittest.TestCase):
    """El intérprete de `bash` es configurable (Windows necesita Git Bash para POSIX)."""

    def _write(self, tmp: str, text: str) -> str:
        path = os.path.join(tmp, "rules.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_shell_defaults_to_the_system_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(workspace=tmp, config_path=os.path.join(tmp, "no-existe.toml"))
        self.assertEqual(cfg.shell, "")

    def test_shell_is_read_from_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, '[loop]\nshell = "bash"\n')
            cfg = load_config(workspace=tmp, config_path=path)
        self.assertEqual(cfg.shell, "bash")

    def test_example_rules_declare_the_shell_key(self):
        """La plantilla documenta la clave, para que se descubra sin leer el código."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "rules.example.toml")
        with open(path, "r", encoding="utf-8") as fh:
            self.assertIn("shell =", fh.read())


class PythonVersionDegradationTests(unittest.TestCase):
    """En Python < 3.11 (sin `tomllib`) el harness avisa y sigue; no revienta."""

    def test_no_notice_when_tomllib_is_available(self):
        self.assertEqual(version_notice(), "")

    def test_notice_explains_the_missing_tomllib(self):
        with mock.patch.object(config_mod, "TOML_AVAILABLE", False):
            notice = version_notice()
        self.assertIn("3.11", notice)
        self.assertIn("tomllib", notice)
        self.assertIn("rules.toml", notice)

    def test_load_config_degrades_without_tomllib(self):
        """Sin `tomllib` no hay traceback: se ignoran las reglas y se dice cuál se ignoró."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "rules.example.toml")
        with mock.patch.object(config_mod, "tomllib", None):
            with self.assertRaisesRegex(ValueError, 'no se ignoran reglas'):
                load_config(workspace="/tmp", config_path=path)

    def test_missing_rules_is_not_reported_as_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(workspace=tmp, config_path=os.path.join(tmp, "no-existe.toml"))
        self.assertEqual(cfg.toml_skipped, "")

    def test_cli_exits_cleanly_without_tomllib(self):
        """El flag se lee con un `rules.toml` real: el aviso sale a stderr y el exit es 0."""
        import contextlib
        import io

        import agente

        with mock.patch.object(config_mod, "tomllib", None), \
                mock.patch.object(config_mod, "TOML_AVAILABLE", False):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = agente.main(["--tools"])
        self.assertEqual(code, 2)
        self.assertIn("tomllib", err.getvalue())
        self.assertIn("rules.toml", err.getvalue())


if __name__ == "__main__":
    unittest.main()
