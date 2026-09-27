"""Single-instant frozen whole-machine view and its production-line self-check."""

import pytest

from paperops.errors import MachineConsistencyError

from support import advance_clock, call, ok, run_line


def test_freezes_every_section_at_one_tick_and_watermark(services):
    view = services.machine.freeze()
    body = view.to_dict()
    assert set(body["subsystems"]) == {
        "press",
        "dry",
        "steam",
        "glue",
        "cal",
        "reel",
        "broke",
        "water",
    }
    assert body["tick"] == 0
    assert body["watermark"] == 0
    assert body["consistent"] is True
    assert body["violation_count"] == 0
    assert len(body["digest"]) == 64


def test_cold_idle_line_is_consistent(services):
    # An idle line must not be reported as a contradiction.
    assert services.machine.freeze().consistent is True
    check = services.machine.check()
    assert check["consistent"] is True
    assert check["violations"] == []
    assert "subsystems" not in check  # diagnostics never carry readings


def test_healthy_line_after_full_run_is_released(console):
    run_line(console)
    response = call(console, "GET", "/api/machine/view")
    assert response.status == 200, response.body
    body = response.body
    assert body["consistent"] is True
    assert body["violation_count"] == 0
    # All readings share the frame the response names.
    assert body["subsystems"]["dry"]["speed"] == 900
    assert body["subsystems"]["press"]["persisted"] is True
    assert body["subsystems"]["cal"]["nip_closed"] is True


def test_speed_without_persisted_press_is_withheld(services):
    # Forge the contradictory frame directly: speed raised, press never landed.
    services.dry._speed = 900.0
    view = services.machine.freeze()
    assert view.consistent is False
    violation = view.violations[0]
    assert violation.section == "dry"
    assert violation.rule == "dry.speed-requires-press.state"
    with pytest.raises(MachineConsistencyError):
        services.machine.require_consistent()


def test_change_without_closed_nip_is_withheld(services):
    # New spool turned up while the calender nip is open.
    services.reel._procedure.advance("new_spool")
    services.reel._spool_loaded = True
    violations = services.machine.freeze().violations
    assert any(v.section == "reel" and v.rule == "reel.change-requires-cal.nip" for v in violations)


def test_nip_without_released_speed_is_withheld(services):
    services.cal._nip_closed = True
    services.cal._nip_pressure = 60.0
    violations = services.machine.freeze().violations
    assert any(v.section == "cal" and v.rule == "cal.nip-requires-dry.speed" for v in violations)


def test_valve_open_without_drain_is_withheld(services):
    services.steam._valve_open = True
    violations = services.machine.freeze().violations
    assert any(v.section == "steam" and v.rule == "steam.valve-requires-steam.drain" for v in violations)


def test_view_endpoint_returns_409_with_section_and_gap(console):
    services = console.services
    services.dry._speed = 900.0  # press record missing
    response = call(console, "GET", "/api/machine/view")
    assert response.status == 409
    body = response.body
    assert body["withheld"] is True
    assert body["consistent"] is False
    assert "subsystems" not in body  # readings never leave when gated
    violation = body["violations"][0]
    assert violation["section"] == "dry"
    assert "press" in violation["expected"]
    assert "900" in violation["actual"]


def test_check_endpoint_reports_violations_but_no_readings(console):
    console.services.steam._valve_open = True
    body = ok(console, "GET", "/api/machine/check")
    assert body["consistent"] is False
    assert body["violation_count"] == 1
    assert body["violations"][0]["section"] == "steam"
    assert "subsystems" not in body
    assert set(body["rules"]) >= {
        "speed_needs_persisted_press",
        "change_needs_closed_nip",
        "valve_needs_drain",
    }


def test_completed_transfer_with_nip_open_is_not_a_change_violation(services):
    # Once the turn-up has walked all the way through, opening the nip is normal
    # maintenance and must not be mistaken for a mid-transfer contradiction.
    for stage in ("new_spool", "retract", "bite"):
        services.reel._procedure.advance(stage)
    violations = services.machine.freeze().violations
    assert not any(v.rule == "reel.change-requires-cal.nip" for v in violations)


def test_advance_tick_is_frozen_once_per_frame(console):
    run_line(console)
    advance_clock(console, 3)
    body = ok(console, "GET", "/api/machine/view")
    assert body["tick"] == console.services.clock.tick
