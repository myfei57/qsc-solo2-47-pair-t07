"""Whole-machine views frozen at one moment and vetted against the line order.

A consolidated view is only worth sending out if every section was read at the
same moment and the readings respect the order the line physically imposes: a
speed release stands on a durable press state, a spool transfer stands on a
loaded calender nip, and so on. A view that breaks one of those relations is
withheld, and the mismatch is reported section by section.
"""

from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class FreezePoint:
    """The moment a whole-machine view is pinned to."""

    tick: int
    watermark: int
    events: int

    def to_dict(self) -> dict[str, int]:
        return {
            "tick": self.tick,
            "watermark": self.watermark,
            "events": self.events,
        }


@dataclass(frozen=True)
class Violation:
    """One line-order relation a frozen view does not respect."""

    rule: str
    section: str
    expects: str
    actual: str

    def __str__(self) -> str:
        return (
            f"{self.section}: {self.rule} expects {self.expects}; "
            f"found {self.actual}"
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "section": self.section,
            "expects": self.expects,
            "actual": self.actual,
        }


@dataclass(frozen=True)
class Rule:
    """An inherent order relation between sections of the line."""

    name: str
    section: str
    expects: str
    check: Callable[[Mapping[str, Any]], str | None]  # None while the relation holds


def _press_persisted_before_speed(sections: Mapping[str, Any]) -> str | None:
    speed = float(sections["dry"]["speed"])
    if speed <= 0.0 or sections["press"]["persisted"]:
        return None
    return f"dry.speed={speed:g} released while press.persisted=False"


def _cal_nip_closed_before_reel_change(sections: Mapping[str, Any]) -> str | None:
    reel = sections["reel"]
    completed = list(reel["procedure"]["completed"])
    underway = bool(completed) or reel["spool_loaded"] or reel["retracted"]
    if not underway or sections["cal"]["nip_closed"]:
        return None
    return f"reel transfer underway (completed={completed}) while cal.nip_closed=False"


def _dry_speed_before_cal_nip(sections: Mapping[str, Any]) -> str | None:
    if not sections["cal"]["nip_closed"]:
        return None
    speed = float(sections["dry"]["speed"])
    if speed > 0.0:
        return None
    pressure = sections["cal"]["nip_pressure"]
    return f"cal nip loaded (pressure={pressure:g}) while dry.speed={speed:g}"


def _steam_valve_before_dry_heat(sections: Mapping[str, Any]) -> str | None:
    temperature = float(sections["dry"]["temperature"])
    if temperature <= 0.0 or sections["steam"]["valve_open"]:
        return None
    return f"dry.temperature={temperature:g} with steam.valve_open=False"


def _steam_drained_before_admit(sections: Mapping[str, Any]) -> str | None:
    completed = sections["dry"]["procedure"]["completed"]
    if "steam" not in completed or sections["steam"]["drained"]:
        return None
    return "dry procedure admitted steam while steam.drained=False"


def _reel_spool_before_retract(sections: Mapping[str, Any]) -> str | None:
    reel = sections["reel"]
    if not reel["retracted"] or reel["spool_loaded"]:
        return None
    return "finished spool retracted while reel.spool_loaded=False"


LINE_RULES: tuple[Rule, ...] = (
    Rule(
        "press.persisted_before_speed",
        "dry",
        "the press state to be durable before any speed release",
        _press_persisted_before_speed,
    ),
    Rule(
        "cal.nip_closed_before_reel_change",
        "reel",
        "the calender nip to be loaded before a spool transfer",
        _cal_nip_closed_before_reel_change,
    ),
    Rule(
        "dry.speed_before_cal_nip",
        "cal",
        "a durable speed release before the nip is loaded",
        _dry_speed_before_cal_nip,
    ),
    Rule(
        "steam.valve_before_dry_heat",
        "dry",
        "the admission valve to be open before heat is applied",
        _steam_valve_before_dry_heat,
    ),
    Rule(
        "steam.drained_before_admit",
        "steam",
        "the condensate to be drained before steam is admitted",
        _steam_drained_before_admit,
    ),
    Rule(
        "reel.spool_before_retract",
        "reel",
        "the new spool to be loaded before the old one retracts",
        _reel_spool_before_retract,
    ),
)


def check_line(sections: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[Violation]]:
    """Evaluate every line-order rule against a frozen whole-machine view."""
    checks: list[dict[str, Any]] = []
    violations: list[Violation] = []
    for rule in LINE_RULES:
        actual = rule.check(sections)
        checks.append(
            {
                "rule": rule.name,
                "section": rule.section,
                "expects": rule.expects,
                "ok": actual is None,
            }
        )
        if actual is not None:
            violations.append(Violation(rule.name, rule.section, rule.expects, actual))
    return checks, violations
