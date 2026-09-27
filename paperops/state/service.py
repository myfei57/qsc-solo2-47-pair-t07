"""Consolidated read model and the expiring control snapshot."""

from typing import Any, Mapping, Protocol

from ..clock import LogicalClock
from ..errors import InconsistentStateError
from ..event import Event, EventBus, topics
from ..store import Journal
from ..store.snapshot import digest_rows
from ..verdict import VerdictHistory
from ..version import ArtifactRef, Artifacts, Generations
from .counters import counters
from .frozen import FreezePoint, Violation, check_line
from .view import ControlState

SNAPSHOT_NAME = "control.snapshot"


class Readable(Protocol):
    """Anything that can render a read model."""

    def view(self) -> Any: ...


class StateService:
    """Aggregates subsystem views and freezes them into expiring snapshots."""

    def __init__(
        self,
        journal: Journal,
        bus: EventBus,
        clock: LogicalClock,
        generations: Generations,
        artifacts: Artifacts,
        subsystems: Mapping[str, Readable],
        histories: Mapping[str, VerdictHistory],
    ) -> None:
        self._journal = journal
        self._bus = bus
        self._clock = clock
        self._generations = generations
        self._artifacts = artifacts
        self._subsystems = dict(subsystems)
        self._histories = dict(histories)

    def view(self) -> ControlState:
        return ControlState(
            tick=self._clock.tick,
            watermark=self._journal.watermark,
            committed_keys=len(self._journal.keys()),
            events=self._bus.sequence,
            subsystems={
                name: subsystem.view().to_dict()
                for name, subsystem in self._subsystems.items()
            },
        )

    def counters(self) -> dict[str, int]:
        return counters(self._bus)

    def frozen_view(self, attempts: int = 3) -> dict[str, Any]:
        """Render every section at one moment; release it only if the line order holds."""
        sections, point = self._render_at_one_moment(attempts)
        checks, violations = check_line(sections)
        if violations:
            raise InconsistentStateError(point, violations)
        return {
            "tick": point.tick,
            "watermark": point.watermark,
            "events": point.events,
            "digest": digest_rows(sections),
            "checks": checks,
            "subsystems": sections,
        }

    def _freeze_point(self) -> FreezePoint:
        return FreezePoint(
            tick=self._clock.tick,
            watermark=self._journal.watermark,
            events=self._bus.sequence,
        )

    def _render_at_one_moment(self, attempts: int) -> tuple[dict[str, Any], FreezePoint]:
        moved: tuple[FreezePoint, FreezePoint] | None = None
        for _ in range(max(1, attempts)):
            before = self._freeze_point()
            sections = {
                name: subsystem.view().to_dict()
                for name, subsystem in self._subsystems.items()
            }
            after = self._freeze_point()
            if before == after:
                return sections, before
            moved = (before, after)
        assert moved is not None  # max(1, attempts) guarantees at least one pass
        before, after = moved
        raise InconsistentStateError(
            after,
            [
                Violation(
                    rule="state.same_moment",
                    section="state",
                    expects="every section read at one tick, watermark and event sequence",
                    actual=(
                        f"the freeze point moved from {before.to_dict()} "
                        f"to {after.to_dict()} while the sections were read"
                    ),
                )
            ],
        )

    def snapshot(self) -> dict[str, Any]:
        """Freeze the committed stream and record it as a new snapshot generation."""
        frozen = self._journal.snapshot()
        generation = self._generations.advance(SNAPSHOT_NAME, "state-snapshot")
        ref = self._artifacts.record("snapshot", SNAPSHOT_NAME, generation, frozen.digest)
        self._bus.publish(topics.TOPIC_SNAPSHOT, frozen.watermark)
        return {"snapshot": frozen.to_dict(), "artifact": ref.to_row()}

    def require_snapshot(self, max_age_ticks: int = 100) -> ArtifactRef:
        """Refuse a snapshot that a parameter change has superseded."""
        return self._artifacts.require_current(
            "snapshot",
            SNAPSHOT_NAME,
            max_age_ticks,
            self._generations,
        )

    def verdict_history(self, subject: str) -> dict[str, Any]:
        history = self._histories.get(subject)
        if history is None:
            return {"subject": subject, "current": "unknown", "points": []}
        return history.to_dict()

    def history(self, subject: str) -> VerdictHistory | None:
        """The history object for a subject, for callers that need its predicates."""
        return self._histories.get(subject)

    def histories(self) -> tuple[str, ...]:
        return tuple(sorted(self._histories))

    def wire(self) -> None:
        """Expire published snapshots whenever a parameter generation moves."""
        for topic in (
            topics.TOPIC_DRY_CURVE_SET,
            topics.TOPIC_STEAM_BASELINE_SET,
            topics.TOPIC_STEAM_BASELINE_INVALIDATED,
            topics.TOPIC_GLUE_CONC_SET,
            topics.TOPIC_WATER_MAP,
        ):
            self._bus.subscribe(topic, self._expire_snapshot)

    def _expire_snapshot(self, event: Event) -> None:
        self._generations.advance(SNAPSHOT_NAME, "state-snapshot-expire")
