#!/usr/bin/env python3
"""Punto de entrada del harness de agente local.

    python3 agente.py "crea la carpeta demo con un saludo.txt"
    python3 agente.py --interactive
    python3 agente.py --tools
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness.config import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_MODEL,
    load_config,
    version_notice,
)
from harness.providers.llm import Ollama, OllamaError  # noqa: E402
from harness.storage.memory import SourceLedger  # noqa: E402
from harness.loop import run  # noqa: E402
from harness.tools import Registry, ToolContext  # noqa: E402
from harness.storage.trace import Trace  # noqa: E402

BANNER = "\033[1mloagen\033[0m · harness para Ollama"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agente.py",
        description="Agente local con herramientas sobre Ollama (modelo por defecto: "
                    f"{DEFAULT_MODEL}).",
    )
    p.add_argument("task", nargs="*", help="tarea a ejecutar (una sola vez)")
    p.add_argument("-i", "--interactive", action="store_true", help="modo conversación")
    p.add_argument("-m", "--model", default=None, help=f"modelo Ollama (def. {DEFAULT_MODEL})")
    p.add_argument("--subagent-max-steps", type=int, default=None,
                   help="tope de pasos de cada subagente (def. 8)")
    p.add_argument("--host", default=None, help=f"URL de Ollama (def. {DEFAULT_HOST})")
    p.add_argument("-w", "--workspace", default=None, help="raíz de trabajo (def. cwd)")
    p.add_argument("--rules", default=None, help="ruta a rules.toml (def. <workspace>/rules.toml)")
    p.add_argument("--max-steps", type=int, default=None, help="tope de pasos por tarea")
    p.add_argument('--task-timeout', type=int, default=None)
    p.add_argument('--max-tool-calls', type=int, default=None)
    p.add_argument('--max-total-tokens', type=int, default=None)
    p.add_argument('--expect-file', action='append', default=[], metavar='PATH', help='exige que exista este fichero al terminar')
    p.add_argument('--expect-content', action='append', default=[], metavar='PATH=TEXT', help='exige contenido exacto')
    p.add_argument('--skill', action='append', default=None, help='skill local a cargar explícitamente')
    p.add_argument('--list-skills', action='store_true')
    p.add_argument("--plan", dest="plan", action="store_true", default=None,
                   help="planificación previa: construye un task graph antes de actuar")
    p.add_argument("--no-plan", dest="plan", action="store_false",
                   help="desactiva la planificación aunque esté activada en rules.toml")
    p.add_argument("--plan-max-steps", type=int, default=None,
                   help="máximo de pasos que puede tener el plan (def. 5)")
    p.add_argument("--plan-require", dest="plan_require", action="store_true", default=None,
                   help="no acepta terminar con pasos del plan pendientes")
    p.add_argument("--no-plan-require", dest="plan_require", action="store_false",
                   help="permite cerrar con pasos pendientes (solo avisa)")
    p.add_argument("--no-web", dest="web", action="store_false", default=None,
                   help="desactiva el navegador (web_search, wiki, fetch_url)")
    p.add_argument("--no-tool-selection", dest="tool_selection", action="store_false",
                   default=None,
                   help="envía SIEMPRE todas las herramientas (por defecto se eligen "
                        "por tarea para recortar el prompt y la latencia)")
    p.add_argument("--cerebro-root", default=None,
                   help="ruta a cerebro/ (def. autodetectada)")
    p.add_argument("--subagent-model", default=None,
                   help="modelo para los subagentes (def. el mismo que el principal)")
    p.add_argument("--num-ctx", type=int, default=None, help="ventana de contexto de Ollama")
    p.add_argument("--max-output-tokens", type=int, default=None,
                   help="tope de tokens generados por turno (def. 512); evita que el "
                        "modelo se enrolle y bloquee la sesión")
    p.add_argument("--temperature", type=float, default=None, help="temperatura (def. 0)")
    p.add_argument("--allow-outside-workspace", action="store_true",
                   help="permite leer/escribir fuera del workspace")
    p.add_argument("--yes", action="store_true",
                   help="auto-aprueba los permisos 'ask' (sesión de confianza)")
    p.add_argument("--read-only", action="store_true",
                   help="deniega toda herramienta que modifique el sistema")
    p.add_argument("-v", "--verbose", action="store_true", help="muestra observaciones")
    p.add_argument("-q", "--quiet", action="store_true", help="silencia el progreso")
    p.add_argument("--init", action="store_true",
                   help="escribe un rules.toml a medida en el workspace y sale")
    p.add_argument("--force", action="store_true",
                   help="con --init, sobrescribe un rules.toml existente")
    p.add_argument("--doctor", action="store_true",
                   help="diagnostica el entorno (Python, Ollama, modelo, cerebro, shell)")
    p.add_argument("--tools", action="store_true", help="lista las herramientas y sale")
    p.add_argument("--list-models", action="store_true", help="lista los modelos de Ollama")
    return p


def _invocation() -> str:
    """Cómo se invoca al harness desde aquí: instalado (`loagen`) o copiado."""
    prog = os.path.basename(sys.argv[0] or "")
    if prog.startswith("loagen"):
        return prog
    return f"python3 {os.path.abspath(__file__)}"


def cmd_init(args, cfg) -> int:
    """Asistente: detecta el entorno y escribe un `rules.toml` ya usable."""
    from harness import bootstrap

    env = bootstrap.environment(cfg.host, cfg.workspace)
    model, note = bootstrap.pick_model(env["models"], cfg.model)
    print(f"{BANNER}\n\nAsistente de configuración\n")
    print(f"  modelo      {model}   ({note})")
    print(f"  workspace   {env['workspace']}"
          + ("" if env["workspace_writable"] else "   ¡no escribible!"))
    print(f"  ollama      {env['host']}"
          + ("   no responde: arranca Ollama (ollama serve)" if env["ollama_error"] else ""))
    print(f"  cerebro     {env['cerebro_root'] or 'no detectado (se retiran sus 4 herramientas)'}")
    print(f"  render JS   {'sí' if env['render'] else 'no (fetch_url usará el HTML estático)'}")
    print(f"  shell       {env['shell'] or 'el del sistema'}")
    print()

    target = os.path.abspath(args.rules) if args.rules else os.path.join(cfg.workspace, "rules.toml")
    written, detail = bootstrap.write_rules(
        target, bootstrap.render_rules(env, model), force=args.force
    )
    if not written:
        print(f"aviso: {detail}", file=sys.stderr)
        return 2
    print(f"escrito: {detail}")
    print("\nrevisa la sección [permissions]: es el contrato de seguridad del agente.")
    print(f"siguiente:  {_invocation()} --doctor")
    return 0


def cmd_doctor(cfg) -> int:
    """Diagnóstico: qué falta para ejecutar y qué solo mejora la experiencia."""
    from harness import bootstrap

    checks = bootstrap.doctor(cfg)
    print(f"{BANNER}\n\nDiagnóstico\n")
    for check in checks:
        print(bootstrap.format_check(check))

    errors = [c for c in checks if c.level == bootstrap.ERROR]
    print()
    if errors:
        print(f"{len(errors)} problema(s) que impiden ejecutar:")
        for check in errors:
            print(f"  - {check.label}: {check.detail}")
        return 2
    print("todo listo. Ejemplo:")
    print(f'  {_invocation()} "crea la carpeta demo con un saludo.txt"')
    return 0


def print_tools(cfg=None) -> None:
    """Lista las herramientas que el agente tendría DE VERDAD en este contexto.

    Se construye el mismo contexto que en una ejecución (plan, subagente, cerebro, web)
    para que el listado no prometa herramientas que luego se retiran del registro.
    """
    from harness.cerebro_tool import Cerebro
    from harness.storage.memory import SourceLedger
    from harness.plan import Plan, PlanStep
    from harness import toolset

    cfg = cfg or load_config(workspace=os.getcwd())
    demo = Plan([PlanStep("1", "paso de ejemplo"), PlanStep("2", "otro paso")])
    cerebro = Cerebro(cfg.cerebro_root) if cfg.cerebro_root else None
    ctx = ToolContext(
        workspace=cfg.workspace,
        plan=demo if cfg.plan else None,
        spawn=(lambda task: "(informe)"),
        cerebro=cerebro,
        web=cfg.web,
        sources=SourceLedger(cfg.sources_path),
        shell=cfg.shell,
    )
    from harness.extensions import mcp_tools
    reg = Registry(ctx, extra=mcp_tools(cfg.mcp_servers)).tools
    print(f"{BANNER}\n\nHerramientas ({len(reg)}):\n")
    for tool in reg.values():
        flag = "escritura" if tool.mutating else "solo lectura"
        params = ", ".join(tool.parameters.get("required", []))
        print(f"  {tool.name}({params})  [{flag}]")
        print(f"      {tool.description}")

    # La selección por tarea es lo que decide cuántas se envían de verdad: enseñarlo
    # evita el malentendido de "declara 18 herramientas y manda 18".
    if cfg.tool_selection:
        names = set(reg)
        print("\nSelección por tarea (las tareas ambiguas reciben todas):\n")
        for profile, example in toolset.EXAMPLES.items():
            chosen = toolset.selection(example, names, names)
            count = len(chosen) if chosen else len(names)
            print(f"  {profile:9s} -> {count} herramientas   (ej.: {example})")
    else:
        print("\nSelección por tarea desactivada (--no-tool-selection): se envían todas.")


def make_config(args):
    overrides = {
        "model": args.model,
        "host": args.host,
        "max_steps": args.max_steps,
        'task_timeout': args.task_timeout,
        'max_tool_calls': args.max_tool_calls,
        'max_total_tokens': args.max_total_tokens,
        'skills': args.skill,
        "plan": args.plan,
        "plan_max_steps": args.plan_max_steps,
        "plan_require": args.plan_require,
        "web": args.web,
        "tool_selection": args.tool_selection,
        "cerebro_root": args.cerebro_root,
        "subagent_model": args.subagent_model,
        "subagent_max_steps": args.subagent_max_steps,
        "num_ctx": args.num_ctx,
        "num_predict": args.max_output_tokens,
        "temperature": args.temperature,
        "auto_approve": args.yes or None,
        "read_only": args.read_only or None,
        "allow_outside_workspace": args.allow_outside_workspace or None,
    }
    cfg = load_config(
        workspace=args.workspace or os.getcwd(),
        config_path=args.rules,
        overrides=overrides,
    )
    cfg.expected_files = {path: None for path in args.expect_file}
    for item in args.expect_content:
        if '=' not in item:
            raise ValueError('--expect-content necesita PATH=TEXT')
        path, text = item.split('=', 1)
        cfg.expected_files[path] = text
    return cfg


def run_task(cfg, task: str, trace: Trace, interactive: bool) -> int:
    print(f"\033[1m›\033[0m {task}", file=sys.stderr)
    ledger = SourceLedger(cfg.sources_path)
    antes = len(ledger.entries())
    result = run(cfg, task, trace, interactive=interactive)
    summary = trace.summary()
    print("\n" + result.answer if result.answer else "\n(sin respuesta)")

    # El resumen del modelo NO es evidencia: el harness imprime aparte los hechos
    # que puede afirmar por sí mismo (un 1.5B tiende a narrar lo que no ha hecho).
    pendientes = trace.incomplete_plan()
    if pendientes:
        print(f"\n\033[31mAVISO: el plan quedó incompleto. Pasos pendientes: "
              f"{', '.join(pendientes)}\033[0m", file=sys.stderr)

    facts = summary["facts"]
    if facts["tools"]:
        used = ", ".join(f"{k}×{v}" for k, v in sorted(facts["tools"].items()))
        print(f"\n\033[2m--- operaciones exitosas (no verifican toda la tarea) ---\n  herramientas: {used}",
              file=sys.stderr)
        for item in facts["files"]:
            print(f"  {item}", file=sys.stderr)
        print("\033[0m", file=sys.stderr)

    # Las fuentes van con su número para que el usuario pueda comprobar cada cita, y se
    # imprimen aunque el modelo no haya usado ninguna herramienta de fichero. La lista
    # marca lo que solo salió en un resultado de búsqueda: eso NO está leído.
    rendered = ledger.render(limit=20, entries=ledger.entries()[antes:])
    if rendered:
        print("\n\033[2m--- fuentes consultadas (citables) ---", file=sys.stderr)
        for line in rendered.splitlines()[1:]:
            print(f"\033[2m  {line}", file=sys.stderr)
        print("\033[0m", file=sys.stderr)

    trace.detail(
        f"\n[{result.stopped}] {result.steps} pasos · "
        f"{summary['tool_calls']} herramientas · {summary['seconds']}s · "
        f"traza: {summary['trace']}"
    )
    print(f'\nEstado: {result.status}', file=sys.stderr)
    if result.status == 'error':
        return 2
    if result.status in ('incomplete', 'blocked'):
        return 3
    # Unverified answers are useful for open questions, but not proof of completion.
    return 0 if result.status == 'completed' else 4


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Python sin `tomllib` (< 3.11): se avisa y se sigue, no se revienta con un traceback.
    notice = version_notice()
    if notice:
        print(notice, file=sys.stderr)

    try:
        if args.init:
            from harness.config import Config
            cfg = Config(workspace=os.path.abspath(args.workspace or os.getcwd()),
                         model=args.model or DEFAULT_MODEL, host=args.host or DEFAULT_HOST)
        else:
            cfg = make_config(args)
    except (ValueError, OSError) as exc:
        print(f'ERROR de configuración: {exc}', file=sys.stderr)
        return 2
    if cfg.toml_skipped:
        print(f"aviso: no se pudo leer {cfg.toml_skipped}; "
              "se usan los permisos por defecto.", file=sys.stderr)

    if args.init:
        return cmd_init(args, cfg)

    if args.doctor:
        return cmd_doctor(cfg)

    if args.list_skills:
        from harness.skills import SkillStore
        print('\n'.join(SkillStore(cfg.skills_root).names()))
        return 0

    if args.tools:
        print_tools(cfg)
        return 0

    if args.list_models:
        client = Ollama(host=cfg.host, model=cfg.model)
        try:
            for name in client.list_models():
                marker = " *" if name == cfg.model else ""
                print(f"{name}{marker}")
        except OllamaError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        return 0

    interactive = args.interactive or not args.task
    if not args.task and not sys.stdin.isatty() and not args.interactive:
        task = sys.stdin.read().strip()
        if task:
            args.task = [task]

    if not args.task and not args.interactive:
        print("ERROR: no hay tarea. Uso: agente.py \"tu tarea\"", file=sys.stderr)
        return 2

    client = Ollama(host=cfg.host, model=cfg.model)
    ok, detail = client.ready()
    if not ok:
        print(f"ERROR: {detail}", file=sys.stderr)
        return 2
    if detail != cfg.model:
        cfg.model = detail

    trace = Trace(cfg.traces_path, verbose=args.verbose, quiet=args.quiet)
    trace.info(f"{BANNER} · modelo={cfg.model} · workspace={cfg.workspace}")
    trace.info(f"permisos: default={cfg.default_action}"
               + (" · read-only" if cfg.read_only else "")
               + (" · auto-aprobado" if cfg.auto_approve else "")
               + (f" · shell={cfg.shell}" if cfg.shell else ""))
    trace.info(f"planificación: {'activada' if cfg.plan else 'desactivada'}"
               + (" · exigida" if cfg.plan and cfg.plan_require else "")
               + (f" · subagentes con {cfg.subagent_model}" if cfg.subagent_model else ""))
    trace.info("web: " + ("activada" if cfg.web else "desactivada")
               + " · cerebro: " + (cfg.cerebro_root or "no disponible")
               + " · herramientas: " + ("por tarea" if cfg.tool_selection else "todas"))

    if args.task:
        return run_task(cfg, " ".join(args.task), trace, interactive)

    # Modo conversación: cada línea es una tarea; la memoria persiste.
    trace.info("modo conversación — escribe una tarea, o 'salir' para terminar.\n")
    status = 0
    while True:
        try:
            line = input("\033[1m» \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.lower() in ("salir", "exit", "quit", "q"):
            break
        if not line:
            continue
        task_trace = Trace(cfg.traces_path, verbose=args.verbose, quiet=args.quiet)
        status = run_task(cfg, line, task_trace, interactive=True)
        print()
    trace.info(f"traza de la sesión: {trace.path}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
