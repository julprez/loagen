"""Navegador web y cerebro local: parseo y degradación, sin tocar la red.

La red se sustituye por fixtures: los tests tienen que ser rápidos, offline y no
depender de que DuckDuckGo o Wikipedia estén de humor.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import navegador  # noqa: E402
from harness.cerebro_tool import Cerebro  # noqa: E402
from harness.llm import LLMReply  # noqa: E402
from harness.memory import SourceLedger  # noqa: E402
from harness.tools import Registry, ToolContext  # noqa: E402

# Fixture con la forma real de html.duckduckgo.com (enlaces vía /l/?uddg=).
DDG_HTML = """
<div class="result results_links">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.ollama.com%2Fcapabilities%2Ftool-calling&amp;rut=abc">Tool calling - <b>Ollama</b></a>
  </h2>
  <a class="result__snippet" href="x">Ollama supports <b>tool calling</b> which allows a model to invoke tools.</a>
</div>
<div class="result results_links">
  <h2 class="result__title">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Follama.com%2Fblog%2Ftool-support&amp;rut=def">Tool support - Ollama Blog</a>
  </h2>
  <a class="result__snippet" href="y">Introducing tool support.</a>
</div>
<div class="result result--ad">
  <a class="result__a" href="https://duckduckgo.com/y.js?ad_provider=x">Anuncio</a>
</div>
"""

WIKI_JSON = '{"title": "Ollama", "extract": "Ollama es una aplicación...", "content_urls": {"desktop": {"page": "https://es.wikipedia.org/wiki/Ollama"}}}'


class SearchParsingTests(unittest.TestCase):
    def setUp(self):
        self.original = navegador._get
        navegador._get = lambda url, timeout=None: DDG_HTML

    def tearDown(self):
        navegador._get = self.original

    def test_decodes_ddg_redirect_to_real_url(self):
        results = navegador.search("ollama tools")
        self.assertEqual(len(results), 2)  # el anuncio se descarta
        self.assertEqual(results[0]["url"], "https://docs.ollama.com/capabilities/tool-calling")
        self.assertEqual(results[0]["title"], "Tool calling - Ollama")
        self.assertIn("tool calling", results[0]["snippet"].lower())

    def test_respects_limit(self):
        self.assertEqual(len(navegador.search("x", limit=1)), 1)

    def test_real_url_rejects_ads_and_non_http(self):
        self.assertEqual(navegador._real_url("https://duckduckgo.com/y.js?ad=1"), "")
        self.assertEqual(navegador._real_url("/l/?uddg=%2Frelativo"), "")
        self.assertEqual(navegador._real_url("https://ejemplo.com/x"), "https://ejemplo.com/x")

    def test_wikipedia_summary_parses_json(self):
        navegador._get = lambda url, timeout=None: WIKI_JSON
        data = navegador.wikipedia_summary("Ollama")
        self.assertEqual(data["title"], "Ollama")
        self.assertIn("aplicación", data["extract"])

    def test_wikipedia_missing_returns_empty(self):
        navegador._get = lambda url, timeout=None: '{"title": "x"}'
        self.assertEqual(navegador.wikipedia_summary("nada"), {})


class HtmlToTextTests(unittest.TestCase):
    def test_strips_scripts_styles_and_keeps_text(self):
        raw = (
            "<html><head><title>T</title><style>a{color:red}</style></head>"
            "<body><script>var x=1;</script><h1>Hola</h1><p>Mundo &amp; más</p>"
            "<nav>menú</nav></body></html>"
        )
        text = navegador.html_to_text(raw)
        self.assertIn("Hola", text)
        self.assertIn("Mundo & más", text)
        self.assertNotIn("var x=1", text)
        self.assertNotIn("color:red", text)

    def test_empty_input(self):
        self.assertEqual(navegador.html_to_text(""), "")


class WebToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = navegador._get
        navegador._get = lambda url, timeout=None: DDG_HTML

    def tearDown(self):
        navegador._get = self.original
        self.tmp.cleanup()

    def test_web_search_tool_formats_results(self):
        reg = Registry(ToolContext(workspace=self.tmp.name, web=True))
        out, err = reg.run("web_search", {"query": "ollama"})
        self.assertFalse(err, out)
        self.assertIn("RESULTADOS DE BÚSQUEDA", out)
        self.assertIn("docs.ollama.com", out)

    def test_web_search_reports_empty_results(self):
        navegador._get = lambda url, timeout=None: "<html>sin resultados</html>"
        reg = Registry(ToolContext(workspace=self.tmp.name, web=True))
        out, err = reg.run("web_search", {"query": "zzz"})
        self.assertFalse(err)
        self.assertIn("Sin resultados", out)

    def test_web_tools_are_removed_when_web_is_off(self):
        reg = Registry(ToolContext(workspace=self.tmp.name, web=False))
        for name in ("web_search", "wiki", "fetch_url"):
            self.assertNotIn(name, reg.tools)
        out, err = reg.run("web_search", {"query": "x"})
        self.assertTrue(err)

    def test_fetch_url_rejects_non_http(self):
        reg = Registry(ToolContext(workspace=self.tmp.name, web=True))
        out, err = reg.run("fetch_url", {"url": "file:///etc/passwd"})
        self.assertTrue(err)
        self.assertIn("http", out)

    def test_fetch_url_returns_text(self):
        navegador._get = lambda url, timeout=None: "<html><body><p>Contenido</p></body></html>"
        reg = Registry(ToolContext(workspace=self.tmp.name, web=True))
        out, err = reg.run("fetch_url", {"url": "https://ejemplo.com", "render": False})
        self.assertFalse(err, out)
        self.assertIn("Contenido", out)

    def test_fetch_url_reports_network_error_as_text(self):
        def boom(url, timeout=None):
            raise navegador.WebError("no se pudo conectar (timed out)")

        navegador._get = boom
        reg = Registry(ToolContext(workspace=self.tmp.name, web=True))
        out, err = reg.run("fetch_url", {"url": "https://ejemplo.com", "render": False})
        self.assertTrue(err)
        self.assertIn("no se pudo conectar", out)


class CerebroToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "cerebro")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "cerebro.py"), "w") as fh:
            fh.write("# fake\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_available_without_script(self):
        self.assertFalse(Cerebro(self.tmp.name).disponible())

    def test_available_with_script(self):
        self.assertTrue(Cerebro(self.root).disponible())

    def test_search_parses_json(self):
        cerebro = Cerebro(self.root)
        cerebro._run = lambda *a, **k: (0, '{"mode": "AND", "elapsed_ms": 1.1, "results": '
                                           '[{"loc": "a/b.go:12", "score": -9.5, '
                                           '"snippet": "func main()"}]}')
        out = cerebro.buscar("main")
        self.assertIn("a/b.go:12", out)
        self.assertIn("modo AND", out)

    def test_search_warns_on_or_mode(self):
        cerebro = Cerebro(self.root)
        cerebro._run = lambda *a, **k: (0, '{"mode": "OR", "elapsed_ms": 2.0, "results": '
                                           '[{"loc": "x.py:1", "score": -1.0, "snippet": ""}]}')
        self.assertIn("AVISO", cerebro.buscar("algo raro"))

    def test_search_without_results(self):
        cerebro = Cerebro(self.root)
        cerebro._run = lambda *a, **k: (0, '{"mode": "AND", "elapsed_ms": 1.0, "results": []}')
        self.assertIn("no encuentra nada", cerebro.buscar("zzz"))

    def test_search_reports_non_json_and_failures(self):
        cerebro = Cerebro(self.root)
        cerebro._run = lambda *a, **k: (0, "no soy json")
        self.assertIn("no JSON", cerebro.buscar("x"))
        cerebro._run = lambda *a, **k: (1, "boom")
        self.assertIn("ERROR", cerebro.buscar("x"))

    def test_tools_are_removed_without_cerebro(self):
        reg = Registry(ToolContext(workspace=self.tmp.name, cerebro=None))
        self.assertNotIn("cerebro_buscar", reg.tools)
        out, err = reg.run("cerebro_buscar", {"consulta": "x"})
        self.assertTrue(err)

    def test_tools_present_with_cerebro(self):
        reg = Registry(ToolContext(workspace=self.tmp.name, cerebro=Cerebro(self.root)))
        for name in ("cerebro_buscar", "cerebro_impacto", "cerebro_defs",
                     "cerebro_callers"):
            self.assertIn(name, reg.tools)

    def test_defs_and_callers_are_removed_without_cerebro(self):
        reg = Registry(ToolContext(workspace=self.tmp.name, cerebro=None))
        for name in ("cerebro_defs", "cerebro_callers"):
            self.assertNotIn(name, reg.tools)
            out, err = reg.run(name, {"simbolo": "x"})
            self.assertTrue(err)

    def test_impact_is_trimmed(self):
        cerebro = Cerebro(self.root)
        cerebro._run = lambda *a, **k: (0, "linea\n" * 500)
        out = cerebro.impacto("simbolo")
        self.assertIn("recortado", out)
        self.assertLess(len(out), 20_000)

    def test_defs_parses_json_and_lists_every_definition(self):
        """El índice busca por el nombre desnudo: con varias definiciones hay que
        mostrarlas TODAS, sin elegir una por el modelo."""
        cerebro = Cerebro(self.root)
        seen = {}

        def fake(script, args, pkgs=False):
            seen["args"] = args
            seen["pkgs"] = pkgs
            return 0, ('{"symbol": "load_config", "definitions": ['
                       '{"name": "load_config", "qual": "load_config", "kind": "function",'
                       ' "loc": "a/b.py:10", "sig": "def load_config(x)"},'
                       '{"name": "load_config", "qual": "Cfg.load_config", '
                       '"kind": "method", "loc": "c/d.py:3", "sig": "def load_config(self)"}]}')

        cerebro._run = fake
        out = cerebro.definiciones("load_config")
        self.assertIn("--json", seen["args"])
        self.assertTrue(seen["pkgs"], "el índice estructural necesita PYTHONPATH=./.pkgs")
        self.assertIn("a/b.py:10", out)
        self.assertIn("c/d.py:3", out)
        self.assertIn("2", out)
        self.assertIn("varias con el mismo identificador", out)

    def test_callers_reports_usage_count(self):
        cerebro = Cerebro(self.root)
        cerebro._run = lambda *a, **k: (0, ('{"symbol": "ToolContext", "callers": ['
                                           '{"loc": "tools.py:30", "qual": "build_registry",'
                                           ' "kind": "function", "refs": 3, "sig": "def x()"}]}'))
        out = cerebro.llamadores("ToolContext")
        self.assertIn("tools.py:30", out)
        self.assertIn("build_registry", out)
        self.assertIn("3 uso/s", out)

    def test_defs_and_callers_explain_an_empty_result(self):
        """Un 0 del índice NO significa «no existe»: medido en este workspace, el índice
        estructural se construyó antes que el proyecto loagen y devuelve 0 para
        símbolos que sí están en disco. El mensaje tiene que dejar las dos puertas
        abiertas (nombre inexacto / proyecto sin indexar) y ofrecer grep."""
        cerebro = Cerebro(self.root)
        cerebro._run = lambda *a, **k: (0, '{"symbol": "x", "definitions": []}')
        out = cerebro.definiciones("x")
        self.assertIn("ninguna definición", out)
        self.assertIn("no está en el índice", out)
        self.assertIn("grep", out)
        cerebro._run = lambda *a, **k: (0, '{"symbol": "x", "callers": []}')
        out = cerebro.llamadores("x")
        self.assertIn("Ninguna definición", out)
        self.assertIn("no está en el índice", out)

    def test_defs_and_callers_degrade_on_failure_and_garbage(self):
        """Nunca lanzan: un fallo vuelve como texto que el modelo puede leer."""
        cerebro = Cerebro(self.root)
        cerebro._run = lambda *a, **k: (1, "boom")
        self.assertIn("ERROR", cerebro.definiciones("x"))
        self.assertIn("ERROR", cerebro.llamadores("x"))
        cerebro._run = lambda *a, **k: (0, "no soy json")
        self.assertIn("no JSON", cerebro.definiciones("x"))
        self.assertIn("no JSON", cerebro.llamadores("x"))

    def test_callers_is_trimmed_when_huge(self):
        cerebro = Cerebro(self.root)
        rows = ",".join('{"loc": "f.py:%d", "qual": "q", "kind": "function", '
                       '"refs": 1, "sig": "s"}' % i for i in range(60))
        cerebro._run = lambda *a, **k: (0, '{"symbol": "x", "callers": [' + rows + ']}')
        out = cerebro.llamadores("x", limite=12)
        self.assertIn("y 48 más", out)
        self.assertLess(len(out.splitlines()), 20)

    def test_english_param_aliases_are_normalized(self):
        """Un 1.5B mezcla idiomas: `symbol` debe valer, y el esquema sigue exigiendo
        `simbolo` para que el modelo sepa qué mandar."""
        cerebro = Cerebro(self.root)
        cerebro.definiciones = lambda simbolo, *a, **k: f"defs de {simbolo}"
        reg = Registry(ToolContext(workspace=self.tmp.name, cerebro=cerebro))
        self.assertEqual(reg.tools["cerebro_defs"].parameters["required"], ["simbolo"])
        self.assertEqual(reg.run("cerebro_defs", {"symbol": "S"})[0], "defs de S")
        out, err = reg.run("cerebro_defs", {})
        self.assertTrue(err)
        self.assertIn("faltan parámetros", out)

    def test_tools_route_to_the_right_method(self):
        cerebro = Cerebro(self.root)
        called = []
        cerebro.definiciones = lambda simbolo, *a, **k: called.append(("defs", simbolo)) or "d"
        cerebro.llamadores = lambda simbolo, *a, **k: called.append(("callers", simbolo)) or "c"
        reg = Registry(ToolContext(workspace=self.tmp.name, cerebro=cerebro))
        self.assertEqual(reg.run("cerebro_defs", {"simbolo": "S"})[0], "d")
        self.assertEqual(reg.run("cerebro_callers", {"symbol": "S"})[0], "c")
        self.assertEqual(called, [("defs", "S"), ("callers", "S")])


class SourceLedgerTests(unittest.TestCase):
    """El navegador guarda lo consultado para poder citarlo después."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = SourceLedger(os.path.join(self.tmp.name, "fuentes.jsonl"))
        self.original = navegador._get

    def tearDown(self):
        navegador._get = self.original
        self.tmp.cleanup()

    def _ctx(self, web=True):
        return ToolContext(workspace=self.tmp.name, web=web, sources=self.ledger)

    def test_fetch_url_registers_the_source_and_numbers_it(self):
        navegador._get = lambda url, timeout=None: "<html><body><p>Contenido</p></body></html>"
        reg = Registry(self._ctx())
        out, err = reg.run("fetch_url", {"url": "https://ejemplo.com/a", "render": False})
        self.assertFalse(err, out)
        self.assertIn("FUENTE REGISTRADA #1", out)
        self.assertEqual([e["ref"] for e in self.ledger.entries()], ["https://ejemplo.com/a"])

    def test_wiki_registers_the_article_url(self):
        navegador._get = lambda url, timeout=None: WIKI_JSON
        reg = Registry(self._ctx())
        reg.run("wiki", {"topic": "Ollama"})
        entries = self.ledger.entries()
        self.assertEqual(entries[0]["kind"], "wiki")
        self.assertEqual(entries[0]["ref"], "https://es.wikipedia.org/wiki/Ollama")

    def test_web_search_registers_results_as_unread(self):
        navegador._get = lambda url, timeout=None: DDG_HTML
        reg = Registry(self._ctx())
        out, err = reg.run("web_search", {"query": "ollama"})
        self.assertFalse(err, out)
        # La búsqueda ocupa el [1]; los resultados van numerados a continuación.
        self.assertIn("1. Tool calling - Ollama [#2]", out)
        kinds = {e["kind"] for e in self.ledger.entries()}
        self.assertIn("búsqueda", kinds)
        self.assertIn("resultado", kinds)
        # Un resultado de búsqueda NO cuenta como leído: solo se cita lo leído.
        self.assertEqual(self.ledger.read_urls(), set())
        # Y el registro lo dice explícitamente al listarlo.
        self.assertIn("[NO leída", self.ledger.render())

    def test_uncited_accepts_url_or_number(self):
        """Un guard que no reconociera el número `[n]` no se apagaría nunca (medido):
        el modelo cita `[1]`, que es exactamente lo que se le pide."""
        self.ledger.add("resultado", "https://vista.example")
        self.ledger.add("url", "https://leida.example")
        self.assertEqual(self.ledger.uncited("La página dice 79 [2]."), set())
        self.assertEqual(self.ledger.uncited("Dice 79 caracteres."),
                         {"https://leida.example"})
        self.assertEqual(self.ledger.uncited("Según https://leida.example, 79."), set())
        # Una fuente solo vista en búsqueda no hay que citarla: no está leída.
        self.assertEqual(self.ledger.uncited("Nada"), {"https://leida.example"})

    def test_sources_tool_lists_and_cites(self):
        navegador._get = lambda url, timeout=None: "<html><body><p>x</p></body></html>"
        reg = Registry(self._ctx())
        reg.run("fetch_url", {"url": "https://ejemplo.com/a", "render": False})
        out, err = reg.run("fuentes", {})
        self.assertFalse(err, out)
        self.assertIn("[1] url: https://ejemplo.com/a", out)
        out, err = reg.run("fuentes", {"fuente": "1"})
        self.assertFalse(err, out)
        self.assertEqual(out, "[1] https://ejemplo.com/a")
        out, err = reg.run("fuentes", {"fuente": "ejemplo.com"})
        self.assertEqual(out, "[1] https://ejemplo.com/a")

    def test_sources_tool_says_there_is_nothing_yet(self):
        reg = Registry(self._ctx())
        out, err = reg.run("fuentes", {})
        self.assertFalse(err)
        self.assertIn("ninguna fuente", out)
        out, err = reg.run("fuentes", {"fuente": "7"})
        self.assertFalse(err)
        self.assertIn("No hay ninguna fuente", out)

    def test_sources_tool_is_removed_without_a_ledger(self):
        reg = Registry(ToolContext(workspace=self.tmp.name))
        self.assertNotIn("fuentes", reg.tools)
        out, err = reg.run("fuentes", {})
        self.assertTrue(err)

    def test_search_result_upgrades_to_read_when_the_page_is_fetched(self):
        """Caso real medido: la página se registró como resultado de búsqueda y luego se
        leyó con fetch_url; sin ascenderla, seguía marcada [NO leída] y el guard de
        citación no la contaba."""
        navegador._get = lambda url, timeout=None: DDG_HTML
        reg = Registry(self._ctx())
        reg.run("web_search", {"query": "pep8"})
        self.assertEqual(self.ledger.read_urls(), set())

        navegador._get = lambda url, timeout=None: "<html><body><p>PEP 8</p></body></html>"
        url = "https://docs.ollama.com/capabilities/tool-calling"
        reg.run("fetch_url", {"url": url, "render": False})
        entries = self.ledger.entries()
        entry = next(e for e in entries if e["ref"] == url)
        self.assertEqual(entry["kind"], "url")   # ascendida, con el mismo número
        self.assertEqual(entry["n"], 2)
        self.assertEqual(self.ledger.read_urls(), {url})
        # Su línea ya no lleva la marca; los otros resultados siguen marcados.
        linea = next(l for l in self.ledger.render().splitlines() if url in l)
        self.assertNotIn("NO leída", linea)
        self.assertIn("[NO leída", self.ledger.render())
        # Y el ascenso persiste en disco (no se pierde al reabrir).
        reopened = SourceLedger(os.path.join(self.tmp.name, "fuentes.jsonl"))
        self.assertEqual(reopened.read_urls(), {url})

    def test_ledger_does_not_duplicate_and_survives_a_restart(self):
        self.ledger.add("url", "https://a.example")
        self.ledger.add("url", "https://a.example")
        self.ledger.add("url", "https://b.example")
        reopened = SourceLedger(os.path.join(self.tmp.name, "fuentes.jsonl"))
        self.assertEqual([e["n"] for e in reopened.entries()], [1, 2])
        self.assertEqual(reopened.read_urls(), {"https://a.example", "https://b.example"})

    def test_render_marks_unread_results_and_limits_output(self):
        self.ledger.add("resultado", "https://a.example", "A")
        self.ledger.add("url", "https://b.example")
        text = self.ledger.render()
        self.assertIn("[1] resultado: https://a.example — A  [NO leída", text)
        self.assertIn("[2] url: https://b.example", text)
        for i in range(30):
            self.ledger.add("url", f"https://x.example/{i}")
        self.assertIn("se muestran las 20 últimas de 32", self.ledger.render(limit=20))


class CitationGuardTests(unittest.TestCase):
    """De punta a punta: el modelo lee una página y no cita -> se le reconduce.

    Se usa un modelo falso porque el objeto de la prueba es el bucle (registro de
    fuentes + guard), no la calidad del 1.5B. El navegador sí está de verdad: solo se
    sustituye `_get` por una fixture.
    """

    class Model:
        """Paso 1: lee la URL. Paso 2: resume SIN citar. Paso 3: cita."""

        def __init__(self):
            self.model = "fake"
            self.calls = 0

        def chat(self, messages, tools=None, tool_params=None):
            self.calls += 1
            if self.calls == 1:
                return LLMReply(output_tokens=10, tool_calls=[{
                    "id": "c1", "type": "function",
                    "function": {"name": "fetch_url",
                                 "arguments": {"url": "https://ejemplo.com/a",
                                               "render": False}},
                }])
            if self.calls == 2:
                return LLMReply(content="La página dice que 79 caracteres.",
                                output_tokens=9)
            return LLMReply(content="Dice 79 caracteres [1].", output_tokens=9)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = navegador._get
        navegador._get = lambda url, timeout=None: (
            "<html><body><p>PEP 8 recomienda 79 caracteres.</p></body></html>")

    def tearDown(self):
        navegador._get = self.original
        self.tmp.cleanup()

    def test_uncited_sources_are_redirected_to_fuentes(self):
        from harness.config import Config
        from harness.loop import run
        from harness.trace import Trace

        cfg = Config(workspace=self.tmp.name, plan=False, max_steps=6)
        cfg.cerebro_root = ""
        trace = Trace(os.path.join(self.tmp.name, "traces"), quiet=True)
        model = self.Model()
        result = run(cfg, "lee https://ejemplo.com/a y di qué recomienda, cita las fuentes",
                     trace, interactive=False, llm=model)

        self.assertEqual(model.calls, 3, "se esperaba el reintento por falta de cita")
        citas = [e for e in trace.events if e["kind"] == "retry"
                 and "sin citar" in str(e.get("cause"))]
        self.assertEqual(len(citas), 1)
        self.assertIn("[1]", result.answer)
        ledger = SourceLedger(cfg.sources_path)
        self.assertEqual(ledger.read_urls(), {"https://ejemplo.com/a"})


class RenderFallbackTests(unittest.TestCase):
    def test_launch_args_try_several_combinations(self):
        """Medido: con los flags normales Chromium crashea ('Page crashed'); el
        arranque tiene que probar más de una combinación en vez de rendirse."""
        self.assertGreaterEqual(len(navegador._LAUNCH_ARGS), 2)
        self.assertTrue(any("--single-process" in args or "--no-zygote" in args
                            for args in navegador._LAUNCH_ARGS))

    def test_fetch_error_includes_the_render_failure(self):
        original_get = navegador._get
        original_render = navegador._render
        navegador._get = lambda url, timeout=None: (_ for _ in ()).throw(
            navegador.WebError("no se pudo conectar (timed out)")
        )
        navegador._render = lambda url, timeout, allow_private=False: None
        try:
            with self.assertRaises(navegador.WebError) as ctx:
                navegador.page_text("https://x.example", render=True)
            self.assertIn("render falló", str(ctx.exception))
        finally:
            navegador._get = original_get
            navegador._render = original_render


    def test_page_text_uses_render_when_html_is_poor(self):
        original_get = navegador._get
        original_render = navegador._render
        navegador._get = lambda url, timeout=None: "<html><body></body></html>"
        navegador._render = lambda url, timeout, allow_private=False: "Texto cargado por JavaScript " * 30
        try:
            text, rendered = navegador.page_text("https://spa.example", render=True)
            self.assertTrue(rendered)
            self.assertIn("JavaScript", text)
        finally:
            navegador._get = original_get
            navegador._render = original_render

    def test_page_text_without_render_keeps_static_text(self):
        original_get = navegador._get
        navegador._get = lambda url, timeout=None: "<html><body><p>estático</p></body></html>"
        try:
            text, rendered = navegador.page_text("https://x.example", render=False)
            self.assertFalse(rendered)
            self.assertIn("estático", text)
        finally:
            navegador._get = original_get


if __name__ == "__main__":
    unittest.main()