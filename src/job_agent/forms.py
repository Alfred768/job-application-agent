from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FieldPlan:
    label: str
    value: str
    sensitive: bool = False
    confidence: float = 1.0


@dataclass(frozen=True)
class FormFillPlan:
    fields: list[FieldPlan] = field(default_factory=list)
    can_auto_submit: bool = False
    submit_gate_reason: str = (
        "Final Submit remains manual for browser-based applications unless a "
        "source-specific adapter explicitly permits auto-submit."
    )

    @property
    def review_required_fields(self) -> list[str]:
        return [field.label for field in self.fields if field.sensitive or field.confidence < 0.9]
