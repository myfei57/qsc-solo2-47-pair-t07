"""The whole-machine view frozen at a single logical instant.

Unlike :meth:`StateService.view`, which re-reads every subsystem one after another
and so can stitch together readings taken at different ticks, a
:class:`MachineView` captures the clock, the journal watermark and every subsystem
reading in one freeze. The digest then pins that exact bundle, so a consumer can
prove that the section readings it correlates really came from the same instant.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..store.snapshot import canonical


@dataclass(frozen=True)
class Violation:
    """One ordering rule the frozen view does not satisfy.

    ``rule`` names the production-line ordering that was checked, ``section`` is
    the section that is out of step with its upstream, and ``expected`` /
    ``actual`` spell out the precondition and what the frozen view showed.
    """

    rule: str
    section: str
    expected: str
    actual: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "section": self.section,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class MachineView:
    """Every section's reading captured at the same ``tick`` and ``watermark``."""

    tick: int
    watermark: int
    events: int
    subsystems: Mapping[str, Mapping[str, Any]]
    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def consistent(self) -> bool:
        """True when every inherent production-line ordering held at the freeze."""
        return not self.violations

    def reading(self, section: str) -> Mapping[str, Any]:
        return self.subsystems[section]

    def digest(self) -> str:
        """Content digest over the frozen readings, excluding the audit verdict."""
        import hashlib

        payload = canonical(
            {
                "tick": self.tick,
                "watermark": self.watermark,
                "events": self.events,
                "subsystems": {name: dict(row) for name, row in self.subsystems.items()},
            }
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self, *, include_readings: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {
            "tick": self.tick,
            "watermark": self.watermark,
            "events": self.events,
            "consistent": self.consistent,
            "violation_count": len(self.violations),
            "violations": [violation.to_dict() for violation in self.violations],
            "digest": self.digest(),
        }
        if include_readings:
            body["subsystems"] = {name: dict(row) for name, row in self.subsystems.items()}
        return body
