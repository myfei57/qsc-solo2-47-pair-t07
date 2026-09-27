"""Freeze a single-instant whole-machine view and gate its release.

The consolidated :meth:`StateService.view` re-reads each subsystem in turn, so the
eight readings in its response can come from different ticks. This service fixes
the clock tick, the journal watermark and the event sequence *once*, captures
every section against that same frame, and then checks the production-line
orderings in :mod:`paperops.machine.rules`. A bundle that fails any rule is never
handed out by :meth:`require_consistent` — the caller gets the sections and the
exact precondition each one missed instead.
"""

from typing import Any, Mapping, Protocol

from ..clock import LogicalClock
from ..errors import MachineConsistencyError
from ..event import EventBus
from ..store import Journal
from . import rules
from .view import MachineView


class Readable(Protocol):
    """Anything that can render a frozen-able section view."""

    def view(self) -> Any: ...


class MachineService:
    """Captures one consistent frame of the line and vets it before release."""

    def __init__(
        self,
        journal: Journal,
        bus: EventBus,
        clock: LogicalClock,
        subsystems: Mapping[str, Readable],
    ) -> None:
        self._journal = journal
        self._bus = bus
        self._clock = clock
        self._subsystems = dict(subsystems)

    def freeze(self) -> MachineView:
        """Capture every section at the same tick and watermark and vet the frame."""
        tick = self._clock.tick
        watermark = self._journal.watermark
        events = self._bus.sequence
        subsystems = {
            name: dict(subsystem.view().to_dict())
            for name, subsystem in self._subsystems.items()
        }
        violations = rules.evaluate(subsystems)
        return MachineView(
            tick=tick,
            watermark=watermark,
            events=events,
            subsystems=subsystems,
            violations=violations,
        )

    def check(self) -> dict[str, Any]:
        """Report the verdict only; never leaks section readings."""
        view = self.freeze()
        return {
            "tick": view.tick,
            "watermark": view.watermark,
            "consistent": view.consistent,
            "rules": list(rules.rule_names()),
            "violation_count": len(view.violations),
            "violations": [violation.to_dict() for violation in view.violations],
        }

    def require_consistent(self) -> MachineView:
        """Return a vetted frame or raise, withholding the readings from release."""
        view = self.freeze()
        if not view.consistent:
            raise MachineConsistencyError(view, view.violations)
        return view
