from dataclasses import dataclass, field


@dataclass
class ToolResult:
    content: str
    ok: bool = True
    truncated: bool = False
    artifacts: list[str] = field(default_factory=list)

    def __getitem__(self, index):
        return (self.content, not self.ok)[index]

    def __iter__(self):
        # Compatibility with observation, is_error = registry.run(...).
        yield self.content
        yield not self.ok
