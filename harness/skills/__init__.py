"""Skills locales: instrucciones revisables, nunca autorizaciones."""
from pathlib import Path
import re


class SkillStore:
    def __init__(self, root: str = ''):
        self.root = Path(root) if root else Path(__file__).parent / 'data'

    def names(self) -> list[str]:
        return sorted(p.parent.name for p in self.root.glob('*/SKILL.md') if p.is_file())

    def load(self, names: list[str], max_chars: int = 6000) -> str:
        bodies = []
        for name in dict.fromkeys(names):
            if not re.fullmatch(r'[a-z0-9-]+', name):
                raise ValueError('nombre de skill inválido')
            path = (self.root / name / 'SKILL.md').resolve()
            if not path.is_relative_to(self.root.resolve()):
                raise ValueError('skill fuera del directorio autorizado')
            body = path.read_text(encoding='utf-8')
            bodies.append(f'\nSKILL {name} (no concede permisos):\n{body}')
        text = ''.join(bodies)
        if len(text) > max_chars:
            raise ValueError('las skills exceden su presupuesto; selecciona menos')
        return text
