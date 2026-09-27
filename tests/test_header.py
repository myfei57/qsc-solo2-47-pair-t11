"""Per-header baselines, drain batches and the share-out."""

import pytest

from paperops.errors import StaleArtifactError

from support import build, issue_confirmation, ok, refused


def declare(console, name="dryer-1", demand=6.0):
    return ok(console, "POST", "/api/header/declare", {"name": name, "demand": demand})


def calibrate(console, name, pressure):
    confirmation = issue_confirmation(console, f"header.baseline.{name}", name)
    return ok(
        console,
        "POST",
        "/api/header/calibrate",
        {"name": name, "pressure": pressure, "confirmation_id": confirmation},
    )


def test_each_header_holds_its_own_baseline(console, services):
    declare(console, "dryer-1", 6.0)
    declare(console, "dryer-2", 4.0)
    assert calibrate(console, "dryer-1", 5.5)["baseline"] == 5.5
    assert calibrate(console, "dryer-2", 4.5)["baseline"] == 4.5
    assert services.header.generation("dryer-1") == 1
    assert services.header.generation("dryer-2") == 1
    state = ok(console, "GET", "/api/header/state")
    baselines = {row["name"]: row["baseline"] for row in state["headers"]}
    assert baselines == {"dryer-1": 5.5, "dryer-2": 4.5}


def test_drain_batch_is_recorded_only_once(console):
    declare(console, "dryer-1", 6.0)
    declare(console, "dryer-2", 4.0)
    body = ok(console, "POST", "/api/header/drain", {"name": "dryer-1", "batch_id": "D-1"})
    assert body["drains"] == 1
    ok(console, "POST", "/api/header/drain", {"name": "dryer-1", "batch_id": "D-2"})

    response = refused(console, "POST", "/api/header/drain", {"name": "dryer-1", "batch_id": "D-1"})
    assert response.status == 409
    response = refused(console, "POST", "/api/header/drain", {"name": "dryer-2", "batch_id": "D-1"})
    assert response.status == 409

    drains = ok(console, "GET", "/api/header/drains?name=dryer-1")
    assert drains["count"] == 2
    assert [row["batch_id"] for row in drains["drains"]] == ["D-1", "D-2"]
    assert ok(console, "GET", "/api/header/drains?name=dryer-2")["count"] == 0


def test_replacing_the_gauge_invalidates_only_that_header(console, services):
    declare(console, "dryer-1", 6.0)
    declare(console, "dryer-2", 4.0)
    calibrate(console, "dryer-1", 5.5)
    calibrate(console, "dryer-2", 4.5)

    ok(console, "POST", "/api/header/gauge", {"name": "dryer-1"})
    assert services.generations.current("header.baseline.dryer-1") == 2
    with pytest.raises(StaleArtifactError):
        services.header.require_fresh_baseline("dryer-1")
    assert services.header.require_fresh_baseline("dryer-2")["baseline"] == 4.5

    state = ok(console, "GET", "/api/header/state")
    calibrated = {row["name"]: row["calibrated"] for row in state["headers"]}
    assert calibrated == {"dryer-1": False, "dryer-2": True}


def test_contention_shows_who_grabs_and_how_to_split(console):
    declare(console, "dryer-1", 6.0)
    declare(console, "dryer-2", 4.0)
    ok(console, "POST", "/api/header/supply", {"supply": 8.0})
    ok(console, "POST", "/api/header/draw", {"name": "dryer-1", "draw": 6.5})
    ok(console, "POST", "/api/header/draw", {"name": "dryer-2", "draw": 1.0})

    state = ok(console, "GET", "/api/header/state")
    assert state["supply"] == 8.0
    assert state["total_demand"] == 10.0
    assert state["total_draw"] == 7.5
    rows = {row["name"]: row for row in state["headers"]}
    assert round(rows["dryer-1"]["share"], 2) == 4.8
    assert rows["dryer-1"]["verdict"] == "grabbing"
    assert round(rows["dryer-2"]["share"], 2) == 3.2
    assert rows["dryer-2"]["verdict"] == "starved"

    what_if = ok(console, "GET", "/api/header/state?supply=10")
    shares = {row["name"]: row["share"] for row in what_if["headers"]}
    assert round(shares["dryer-1"], 2) == 6.0
    assert round(shares["dryer-2"], 2) == 4.0


def test_unknown_header_is_refused(console):
    assert refused(console, "POST", "/api/header/draw", {"name": "ghost", "draw": 1.0}).status == 404
    assert refused(console, "POST", "/api/header/drain", {"name": "ghost", "batch_id": "D-9"}).status == 404
    assert refused(console, "POST", "/api/header/gauge", {"name": "ghost"}).status == 404


def test_declare_validates_the_name_and_demand(console):
    assert refused(console, "POST", "/api/header/declare", {"name": "supply", "demand": 1.0}).status == 400
    assert refused(console, "POST", "/api/header/declare", {"name": "dryer-1", "demand": 0}).status == 422


def test_calibration_needs_a_confirmation(console):
    declare(console, "dryer-1", 6.0)
    response = refused(
        console,
        "POST",
        "/api/header/calibrate",
        {"name": "dryer-1", "pressure": 5.5, "confirmation_id": "cfm-000999"},
    )
    assert response.status == 404


def test_header_model_arithmetic(console):
    body = ok(console, "GET", "/api/header/model?supply=8&demand=6&total_demand=10&draw=7&main=8&header=6")
    assert body["fair_share"] == 4.8
    assert body["verdict"] == "grabbing"
    assert body["deficit"] == 2.0


def test_headers_and_drains_survive_a_restart(config):
    first = build(config)
    declare(first.console, "dryer-1", 6.0)
    calibrate(first.console, "dryer-1", 5.5)
    ok(first.console, "POST", "/api/header/supply", {"supply": 8.0})
    ok(first.console, "POST", "/api/header/drain", {"name": "dryer-1", "batch_id": "D-1"})

    second = build(config)
    assert second.header.declared() == ("dryer-1",)
    assert second.header.supply() == 8.0
    assert second.header.generation("dryer-1") == 1
    assert len(second.header.drains("dryer-1")) == 1
    response = refused(
        second.console, "POST", "/api/header/drain", {"name": "dryer-1", "batch_id": "D-1"}
    )
    assert response.status == 409
