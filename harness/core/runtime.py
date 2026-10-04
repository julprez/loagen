"""Presupuesto de una tarea, compartido con sus subagentes."""
from dataclasses import dataclass, field
import time

from ..tools import ToolContext, resolve_path


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    seconds: float = 300
    max_calls: int = 40
    max_tokens: int = 24000
    started: float = field(default_factory=time.monotonic)
    calls: int = 0
    tokens: int = 0

    def remaining(self) -> float:
        return max(0, self.seconds - (time.monotonic() - self.started))

    def check(self) -> None:
        if self.remaining() <= 0 or self.calls >= self.max_calls or self.tokens >= self.max_tokens:
            raise BudgetExceeded('presupuesto global agotado (tiempo, llamadas o tokens)')

    def charge_call(self) -> None:
        self.check()
        self.calls += 1

    def charge_tokens(self, count: int) -> None:
        self.tokens += max(0, count)


def verification_status(cfg, trace, stopped: str, pending: bool = False) -> str:
    if stopped == 'error':
        return 'error'
    if pending or stopped != 'final' or trace.last_step_failed():
        return 'incomplete'
    if cfg.expected_files:
        for path, expected in cfg.expected_files.items():
            try:
                full = resolve_path(ToolContext(workspace=cfg.workspace), path)
                with open(full, encoding='utf-8') as fh:
                    content = fh.read(1_000_001)
                if len(content) > 1_000_000:
                    return 'incomplete'
                if expected is not None and content != expected:
                    return 'incomplete'
            except (OSError, ValueError):
                return 'incomplete'
        return 'completed'
    if not trace.used_tools():
        return 'blocked' if any(e.get('action') == 'deny' for e in trace.events) else 'unverified'
    # Successful tools are evidence of execution, not a semantic task verifier.
    return 'unverified'
