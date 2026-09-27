"""Inherent production-line orderings checked against a frozen whole-machine view.

Each rule is an invariant that must hold at *every* instant of a legitimate
machine trajectory. The write side enforces the same orderings with ``PreGate``s
when facts are created; these rules refuse to publish a frozen bundle whose
readings contradict one another — the signature of sections sampled at different
instants, or of a read model that has fallen behind the line.

Only orderings with a sticky upstream are included. A temporal precondition whose
upstream is allowed to move again afterwards (the valve may close once the
cylinders are hot; the roll may drop after the pump stopped) cannot be re-derived
from a single instant without falsely rejecting a healthy line, and the write
gate already guarantees it happened in order. A cold, idle line satisfies every
rule, so the checks report real contradictions rather than a line not yet started.
"""

from collections.abc import Callable
from typing import Any, Mapping

from .view import Violation

Sections = Mapping[str, Mapping[str, Any]]
Rule = Callable[[Sections], "Violation | None"]

COMPLETE = "complete"


def _violation(
    rule: str,
    section: str,
    expected: str,
    actual: str,
    detail: str,
) -> Violation:
    return Violation(
        rule=rule,
        section=section,
        expected=expected,
        actual=actual,
        detail=detail,
    )


def speed_needs_persisted_press(sections: Sections) -> Violation | None:
    """提速就该有压榨落盘的底子: raised speed stands on a durable press record."""
    dry = sections["dry"]
    press = sections["press"]
    speed = float(dry["speed"])
    if speed > 0.0 and not bool(press["persisted"]):
        return _violation(
            rule="dry.speed-requires-press.state",
            section="dry",
            expected="press dewatering record landed (press.persisted=true) before speed is raised",
            actual=f"dry.speed={speed:g} while press.persisted=false",
            detail="dry section already reports running speed but the press durable record is absent; "
            "the two readings cannot belong to the same instant",
        )
    return None


def nip_needs_released_speed(sections: Sections) -> Violation | None:
    """压区已合就该已提速: a loaded nip sits on a released, non-zero machine speed."""
    cal = sections["cal"]
    dry = sections["dry"]
    if bool(cal["nip_closed"]) and float(dry["speed"]) <= 0.0:
        return _violation(
            rule="cal.nip-requires-dry.speed",
            section="cal",
            expected="dry section speed released (dry.speed>0) before the nip closes",
            actual=f"cal.nip_closed=true while dry.speed={float(dry['speed']):g}",
            detail="calender nip is loaded on a machine that shows no released speed",
        )
    return None


def change_needs_closed_nip(sections: Sections) -> Violation | None:
    """换卷就该压区已合: while a turn-up is underway the calender nip stays loaded."""
    reel = sections["reel"]
    cal = sections["cal"]
    completed = tuple(reel["procedure"]["completed"])
    in_progress = bool(completed) and reel["procedure"]["next"] != COMPLETE
    stage = completed[-1] if completed else "change"
    if in_progress and not bool(cal["nip_closed"]):
        return _violation(
            rule="reel.change-requires-cal.nip",
            section="reel",
            expected="calender nip closed for the whole spool transfer",
            actual=f"reel transfer past stage '{stage}' while cal.nip_closed=false",
            detail=f"turn-up is underway (next stage is {reel['procedure']['next']!r}) "
            "with the press zone open",
        )
    return None


def valve_needs_drain(sections: Sections) -> Violation | None:
    """进汽就该先疏水: the admission valve never opens onto an undrained header."""
    steam = sections["steam"]
    if bool(steam["valve_open"]) and not bool(steam["drained"]):
        return _violation(
            rule="steam.valve-requires-steam.drain",
            section="steam",
            expected="condensate drained (steam.drained=true) before the valve opens",
            actual="steam.valve_open=true while steam.drained=false",
            detail="steam admitted onto a header that the same frozen view shows not drained",
        )
    return None


RULES: tuple[Rule, ...] = (
    speed_needs_persisted_press,
    nip_needs_released_speed,
    change_needs_closed_nip,
    valve_needs_drain,
)


def rule_names() -> tuple[str, ...]:
    return tuple(rule.__name__ for rule in RULES)


def evaluate(sections: Sections) -> tuple[Violation, ...]:
    """Run every ordering rule against one frozen set of section readings."""
    findings = [rule(sections) for rule in RULES]
    return tuple(found for found in findings if found is not None)
