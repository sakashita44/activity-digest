from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceDocument:
    name: str
    context: str
    records: list[str] = field(default_factory=list[str])

    def to_markdown(self) -> str:
        return "\n\n".join(
            [f"# {self.name}", self.context.strip(), "## Records", *self.records]
        )
