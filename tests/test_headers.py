"""Per-header baselines, drain batches and the supply split."""

import pytest

from paperops.errors import DuplicateBatchError, StaleArtifactError

from support import build, issue_confirmation, ok, refused


def register(console, header_id, demand):
    return ok(console, "POST", "/api/headers/register", {"header_id": header_id, "demand": demand})


def calibrate(console, header_id, pressure):
    return ok(
        console,
        "POST",
        "/api/headers/calibrate",
        {
            "header_id": header_id,
            "pressure": pressure,
            "confirmation_id": issue_confirmation(
                console, f"header.baseline.{header_id}", "headers"
            ),
        },
    )


def test_each_header_keeps_its_own_baseline(console, services):
    register(console, "H1", 4.0)
    register(console, "H2", 6.0)
    calibrate(console, "H1", 5.0)
    calibrate(console, "H2", 7.0)
    assert services.headers.generation("H1") == 1
    assert services.headers.generation("H2") == 1
    assert ok(console, "GET", "/api/headers/pressure?header=H1&reading=5.1")["verdict"] == "in_band"
    assert ok(console, "GET", "/api/headers/pressure?header=H2&reading=7.1")["verdict"] == "in_band"
    body = ok(console, "GET", "/api/headers/pressure?header=H1&reading=7.0")
    assert round(body["offset"], 2) == 2.0
    assert body["baseline"] == 5.0


def test_drain_batches_are_recorded_and_never_twice(console, services):
    register(console, "H1", 4.0)
    register(console, "H2", 6.0)
    ok(console, "POST", "/api/headers/drain", {"header_id": "H1", "batch_id": "D-1", "volume": 2.0})
    ok(console, "POST", "/api/headers/drain", {"header_id": "H1", "batch_id": "D-2", "volume": 1.0})
    ok(console, "POST", "/api/headers/drain", {"header_id": "H2", "batch_id": "D-3", "volume": 3.0})
    assert services.headers.drain_count("H1") == 2
    assert services.headers.drain_count("H2") == 1
    body = ok(console, "GET", "/api/headers/drains?header=H1")
    assert [row["batch_id"] for row in body["drains"]] == ["D-1", "D-2"]

    same_header = refused(console, "POST", "/api/headers/drain", {"header_id": "H1", "batch_id": "D-1", "volume": 2.0})
    assert same_header.status == 409
    other_header = refused(console, "POST", "/api/headers/drain", {"header_id": "H2", "batch_id": "D-1", "volume": 2.0})
    assert other_header.status == 409
    with pytest.raises(DuplicateBatchError):
        services.headers.drain("H1", "D-1", 2.0)
    assert services.headers.drain_count("H1") == 2


def test_allocation_shows_who_is_grabbing_and_how_it_splits(console, services):
    register(console, "H1", 8.0)
    register(console, "H2", 2.0)
    ok(console, "POST", "/api/headers/supply", {"capacity": 8.0})
    body = ok(console, "GET", "/api/headers/allocation")
    assert body["oversubscribed"] is True
    assert body["fair_share"] == 4.0
    rows = {row["header_id"]: row for row in body["headers"]}
    assert rows["H1"]["status"] == "grabbing"
    assert rows["H2"]["status"] == "starved"
    assert round(rows["H1"]["granted"], 2) == 6.4
    assert round(rows["H2"]["granted"], 2) == 1.6
    assert round(rows["H2"]["shortfall"], 2) == 0.4

    ok(console, "POST", "/api/headers/supply", {"capacity": 10.0})
    body = ok(console, "GET", "/api/headers/allocation")
    assert body["oversubscribed"] is False
    assert {row["status"] for row in body["headers"]} == {"satisfied"}


def test_demand_moves_the_split(console, services):
    register(console, "H1", 4.0)
    register(console, "H2", 4.0)
    ok(console, "POST", "/api/headers/supply", {"capacity": 8.0})
    ok(console, "POST", "/api/headers/demand", {"header_id": "H1", "demand": 14.0})
    assert services.headers.demand("H1") == 14.0
    body = ok(console, "GET", "/api/headers/allocation")
    rows = {row["header_id"]: row for row in body["headers"]}
    assert rows["H1"]["status"] == "grabbing"
    assert rows["H2"]["status"] == "starved"


def test_replacing_a_gauge_invalidates_only_that_headers_baseline(console, services):
    register(console, "H1", 4.0)
    register(console, "H2", 6.0)
    calibrate(console, "H1", 5.0)
    calibrate(console, "H2", 7.0)
    ok(console, "POST", "/api/headers/gauge", {"header_id": "H1"})
    assert services.generations.current("header.baseline.H1") == 2
    with pytest.raises(StaleArtifactError):
        services.headers.require_fresh_baseline("H1")
    assert refused(console, "GET", "/api/headers/pressure?header=H1&reading=5.1").status == 409
    assert ok(console, "GET", "/api/headers/pressure?header=H2&reading=7.1")["verdict"] == "in_band"

    calibrate(console, "H1", 5.5)
    assert ok(console, "GET", "/api/headers/pressure?header=H1&reading=5.6")["verdict"] == "in_band"


def test_pressure_verdict_needs_a_calibration(console):
    register(console, "H1", 4.0)
    assert refused(console, "GET", "/api/headers/pressure?header=H1&reading=5.0").status == 409


def test_unknown_header_is_not_found(console):
    assert refused(console, "POST", "/api/headers/demand", {"header_id": "H9", "demand": 1.0}).status == 404
    assert refused(console, "POST", "/api/headers/drain", {"header_id": "H9", "batch_id": "D-1", "volume": 1.0}).status == 404
    assert refused(console, "POST", "/api/headers/gauge", {"header_id": "H9"}).status == 404
    assert refused(console, "GET", "/api/headers/pressure?header=H9&reading=1.0").status == 404
    assert refused(console, "GET", "/api/headers/drains?header=H9").status == 404


def test_negative_values_are_refused(console):
    register(console, "H1", 4.0)
    assert refused(console, "POST", "/api/headers/demand", {"header_id": "H1", "demand": -1.0}).status == 422
    assert refused(console, "POST", "/api/headers/supply", {"capacity": -5.0}).status == 422
    assert refused(console, "POST", "/api/headers/drain", {"header_id": "H1", "batch_id": "D-1", "volume": -1.0}).status == 422


def test_headers_model_arithmetic(console):
    register(console, "H1", 4.0)
    register(console, "H2", 6.0)
    body = ok(console, "GET", "/api/headers/model?capacity=5&offset=0.1&threshold=0.15&pressure=6")
    assert body["allocation"] == {"H1": 2.0, "H2": 3.0}
    assert body["fair_share"] == 2.5
    assert body["window"] == "in_band"
    assert body["band"] == 6.0


def test_headers_survive_a_restart(config):
    first = build(config)
    register(first.console, "H1", 4.0)
    ok(first.console, "POST", "/api/headers/supply", {"capacity": 8.0})
    ok(first.console, "POST", "/api/headers/drain", {"header_id": "H1", "batch_id": "D-1", "volume": 2.0})
    calibrate(first.console, "H1", 5.0)

    second = build(config)
    assert second.headers.demand("H1") == 4.0
    assert second.headers.supply() == 8.0
    assert second.headers.drain_count("H1") == 1
    assert second.headers.generation("H1") == 1
    assert ok(second.console, "GET", "/api/headers/pressure?header=H1&reading=5.1")["verdict"] == "in_band"
    response = refused(second.console, "POST", "/api/headers/drain", {"header_id": "H1", "batch_id": "D-1", "volume": 2.0})
    assert response.status == 409
