"""Whole-machine views frozen at one moment and vetted against the line order."""

import pytest

from paperops.errors import InconsistentStateError
from paperops.state import LINE_RULES, StateService, check_line
from paperops.store.snapshot import digest_rows

from support import build, call, ok, run_line

SECTIONS = {"press", "dry", "steam", "glue", "cal", "reel", "broke", "water"}


def _steady_sections():
    """One consistent cut of a running line, for exercising the rules directly."""
    return {
        "press": {"persisted": True},
        "dry": {
            "procedure": {"completed": ["drain", "steam", "warm", "dry"]},
            "temperature": 120.0,
            "speed": 900.0,
        },
        "steam": {"drained": True, "valve_open": True},
        "cal": {"nip_closed": True, "nip_pressure": 60.0},
        "reel": {
            "procedure": {"completed": ["new_spool", "retract", "bite"]},
            "spool_loaded": True,
            "retracted": True,
        },
    }


def _batch_of(journal, prefix):
    for entry in journal.entries():
        if entry.batch_id.startswith(prefix):
            return entry.batch_id
    raise AssertionError(f"no committed batch with prefix {prefix}")


def test_every_line_rule_flags_its_own_section():
    checks, violations = check_line(_steady_sections())
    assert not violations
    assert all(check["ok"] for check in checks)

    mutations = {
        "press.persisted_before_speed": lambda s: s["press"].update(persisted=False),
        "cal.nip_closed_before_reel_change": lambda s: s["cal"].update(nip_closed=False),
        "dry.speed_before_cal_nip": lambda s: s["dry"].update(speed=0.0),
        "steam.valve_before_dry_heat": lambda s: s["steam"].update(valve_open=False),
        "steam.drained_before_admit": lambda s: s["steam"].update(drained=False),
        "reel.spool_before_retract": lambda s: s["reel"].update(spool_loaded=False),
    }
    assert {rule.name for rule in LINE_RULES} == set(mutations)
    for rule_name, mutate in mutations.items():
        sections = _steady_sections()
        mutate(sections)
        _, violations = check_line(sections)
        assert [violation.rule for violation in violations] == [rule_name]


def test_frozen_view_on_a_cold_line(services):
    body = services.state.frozen_view()
    assert body["tick"] == 0
    assert body["watermark"] == 0
    assert body["events"] == 0
    assert set(body["subsystems"]) == SECTIONS
    assert all(check["ok"] for check in body["checks"])
    assert {check["rule"] for check in body["checks"]} == {rule.name for rule in LINE_RULES}


def test_frozen_endpoint_releases_a_vetted_view(console, services):
    run_line(console)
    body = ok(console, "GET", "/api/state/frozen")
    assert body["tick"] == services.clock.tick
    assert body["watermark"] == services.journal.watermark
    assert body["events"] == services.bus.sequence
    assert set(body["subsystems"]) == SECTIONS
    assert all(check["ok"] for check in body["checks"])
    assert body["digest"] == digest_rows(body["subsystems"])
    again = ok(console, "GET", "/api/state/frozen")
    assert again["digest"] == body["digest"]


def test_speed_without_a_durable_press_state_is_withheld(console, services, config):
    run_line(console)
    services.journal.rollback(_batch_of(services.journal, "press-persist"), "test")
    restarted = build(config)

    with pytest.raises(InconsistentStateError) as excinfo:
        restarted.state.frozen_view()
    assert [v.rule for v in excinfo.value.violations] == ["press.persisted_before_speed"]

    response = call(restarted.console, "GET", "/api/state/frozen")
    assert response.status == 409
    assert "subsystems" not in response.body
    violation = response.body["violations"][0]
    assert violation["rule"] == "press.persisted_before_speed"
    assert violation["section"] == "dry"
    assert "press.persisted=False" in violation["actual"]


def test_reel_change_without_a_loaded_nip_is_withheld(console, services, config):
    run_line(console)
    services.journal.rollback(_batch_of(services.journal, "cal-nip"), "test")
    restarted = build(config)

    response = call(restarted.console, "GET", "/api/state/frozen")
    assert response.status == 409
    assert "subsystems" not in response.body
    violations = response.body["violations"]
    assert [v["rule"] for v in violations] == ["cal.nip_closed_before_reel_change"]
    assert violations[0]["section"] == "reel"
    assert "cal.nip_closed=False" in violations[0]["actual"]


def test_view_is_withheld_when_the_moment_moves_while_reading(services):
    class NudgingView:
        """A section whose render commits a write, so the freeze point keeps moving."""

        def view(self):
            with services.journal.transaction("nudge") as batch:
                services.journal.append("nudge/key", 1, batch)
            return services.press.view()

    state = StateService(
        services.journal,
        services.bus,
        services.clock,
        services.generations,
        services.artifacts,
        subsystems={"press": NudgingView()},
        histories={},
    )
    with pytest.raises(InconsistentStateError) as excinfo:
        state.frozen_view(attempts=2)
    violation = excinfo.value.violations[0]
    assert violation.rule == "state.same_moment"
    assert violation.section == "state"
