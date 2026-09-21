"""End-to-end API tests for the fleet coordination task."""

import os
import tempfile
from datetime import datetime, timedelta

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TIDE_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


with TestClient(app) as _c:
    client = _c


def _samples():
    return [
        {"time": f"2026-09-21T{h:02d}:00:00Z", "height_m": h_m}
        for h, h_m in [
            (0, 1.0), (1, 3.0), (2, 6.0), (3, 8.0),
            (4, 8.0), (5, 6.0), (6, 3.0), (7, 1.0),
        ]
    ]


def _payload(vessels=None, maintenance=None):
    payload = {
        "samples": _samples(),
        "query_start": "2026-09-21T00:00:00Z",
        "query_end": "2026-09-21T07:00:00Z",
        "max_gap_seconds": 3700,
        "vessels": vessels if vessels is not None else [
            {"name": "远洋号", "draft_m": 4.0, "clearance_m": 1.0,
             "transit_duration_seconds": 1800},
            {"name": "浅吃号", "draft_m": 2.0, "clearance_m": 0.5,
             "transit_duration_seconds": 3600},
        ],
    }
    if maintenance is not None:
        payload["maintenance_blocks"] = maintenance
    return payload


def _create_and_wait(payload):
    resp = client.post("/fleet-tasks", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] in {"pending", "done"}
    assert body["result"] is None
    got = client.get(f"/fleet-tasks/{body['task_id']}")
    assert got.status_code == 200
    doc = got.json()
    assert doc["status"] == "done", doc.get("error")
    return doc


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def test_fleet_create_and_poll():
    doc = _create_and_wait(_payload())
    result = doc["result"]
    assert [v["name"] for v in result["vessels"]] == ["远洋号", "浅吃号"]

    deep, shallow = result["vessels"]
    assert deep["threshold_m"] == 5.0
    assert shallow["threshold_m"] == 2.5
    assert result["unknown"] == []

    # Both ships have one safe window; the deeper ship's window sits inside
    # the shallower ship's (same curve, higher threshold).
    assert len(deep["windows"]) == 1 and len(shallow["windows"]) == 1
    dw, sw = deep["windows"][0], shallow["windows"][0]
    assert _parse(dw["start"]) >= _parse(sw["start"])
    assert _parse(dw["end"]) <= _parse(sw["end"])
    # Departure windows equal the safe window minus the per-vessel transit.
    assert deep["departure_windows"][0]["start"] == dw["start"]
    assert _parse(deep["departure_windows"][0]["end"]) == \
        _parse(dw["end"]) - timedelta(seconds=1800)

    # The deep ship's much narrower safe window binds the common set even
    # though its 30-minute transit is shorter than the shallow ship's 60.
    deep_deps = [(w["start"], w["end"]) for w in deep["departure_windows"]]
    assert [(w["start"], w["end"]) for w in
            result["common_departure_windows"]] == deep_deps

    # Shallow-only tails are exclusions caused by the deep ship; together
    # with the common window they tile the shallow ship's departure domain.
    exclusions = result["exclusions"]
    shallow_deps = [(w["start"], w["end"]) for w in shallow["departure_windows"]]
    assert exclusions[0]["start"] == shallow_deps[0][0]
    assert exclusions[0]["end"] == deep_deps[0][0]
    assert exclusions[-1]["start"] == deep_deps[-1][1]
    assert exclusions[-1]["end"] == shallow_deps[-1][1]
    for ex in exclusions:
        assert ex["vessel_names"] == ["远洋号"]
        assert ex["duration_seconds"] > 0


def test_fleet_task_not_found():
    assert client.get("/fleet-tasks/nope").status_code == 404


def test_fleet_shared_maintenance_block():
    doc = _create_and_wait(_payload(maintenance=[
        {"start": "2026-09-21T03:00:00Z", "end": "2026-09-21T04:00:00Z"}
    ]))
    result = doc["result"]
    deep, shallow = result["vessels"]
    # Closure shifted back by the 30 min transit bars deep departures in
    # (02:30, 04:00): the deep ship splits into two departure pieces.
    assert len(deep["departure_windows"]) == 2

    # Every common departure interval must be feasible for EVERY vessel: it
    # is contained in a departure window of each vessel.
    for w in result["common_departure_windows"]:
        lo, hi = _parse(w["start"]), _parse(w["end"])
        assert any(
            _parse(d["start"]) <= lo and hi <= _parse(d["end"])
            for d in deep["departure_windows"]
        )
        assert any(
            _parse(d["start"]) <= lo and hi <= _parse(d["end"])
            for d in shallow["departure_windows"]
        )
    # No common window crosses the closure occupancy interval.
    for w in result["common_departure_windows"]:
        assert _parse(w["end"]) <= datetime.fromisoformat(
            "2026-09-21T02:30:00+00:00") or _parse(w["start"]) >= \
            datetime.fromisoformat("2026-09-21T04:00:00+00:00")


def test_fleet_unknown_gap_is_fleet_wide():
    payload = _payload()
    payload["samples"] = [
        {"time": "2026-09-21T00:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T01:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T05:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T06:00:00Z", "height_m": 8.0},
    ]
    payload["query_end"] = "2026-09-21T06:00:00Z"
    doc = _create_and_wait(payload)
    result = doc["result"]
    reasons = {u["reason"] for u in result["unknown"]}
    assert "sample_gap_exceeds_max_gap" in reasons
    # Per vessel, two departure windows (one per plateau); the common ones
    # never span the unknown gap.
    for v in result["vessels"]:
        assert len(v["departure_windows"]) == 2
        assert all(w["end"] <= "2026-09-21T01:00:00Z" or
                   w["start"] >= "2026-09-21T05:00:00Z"
                   for w in v["departure_windows"])
    assert len(result["common_departure_windows"]) == 2
    for w in result["common_departure_windows"]:
        assert w["end"] <= "2026-09-21T01:00:00Z" or \
               w["start"] >= "2026-09-21T05:00:00Z"
    # The unknown gap itself is not reported as a vessel-labelled exclusion.
    for ex in result["exclusions"]:
        assert not (ex["start"] < "2026-09-21T05:00:00Z" and
                    ex["end"] > "2026-09-21T01:00:00Z")


def test_fleet_no_common_window_when_one_ship_cannot_transit():
    payload = _payload(vessels=[
        {"name": "浅吃号", "draft_m": 2.0, "clearance_m": 0.5,
         "transit_duration_seconds": 1800},
        {"name": "搁浅号", "draft_m": 9.0, "clearance_m": 0.0,
         "transit_duration_seconds": 1800},
    ])
    doc = _create_and_wait(payload)
    result = doc["result"]
    stuck = next(v for v in result["vessels"] if v["name"] == "搁浅号")
    # Threshold 9 is never reached: no safe window for the deep ship.
    assert stuck["windows"] == []
    assert stuck["departure_windows"] == []
    assert result["common_departure_windows"] == []
    # Feasible stretches for the shallow ship are all blamed on 搁浅号.
    assert result["exclusions"]
    for ex in result["exclusions"]:
        assert ex["vessel_names"] == ["搁浅号"]


def test_fleet_validation_duplicate_names():
    payload = _payload(vessels=[
        {"name": "同号", "draft_m": 4.0, "clearance_m": 1.0,
         "transit_duration_seconds": 1800},
        {"name": "同号", "draft_m": 2.0, "clearance_m": 0.5,
         "transit_duration_seconds": 3600},
    ])
    resp = client.post("/fleet-tasks", json=payload)
    assert resp.status_code == 422
    assert "唯一" in resp.text


def test_fleet_validation_empty_vessels_and_bad_transit():
    payload = _payload(vessels=[])
    assert client.post("/fleet-tasks", json=payload).status_code == 422

    payload = _payload(vessels=[
        {"name": "A", "draft_m": -1.0, "clearance_m": 1.0,
         "transit_duration_seconds": 1800},
    ])
    assert client.post("/fleet-tasks", json=payload).status_code == 422

    payload = _payload(vessels=[
        {"name": "A", "draft_m": 4.0, "clearance_m": 1.0,
         "transit_duration_seconds": 0},
    ])
    assert client.post("/fleet-tasks", json=payload).status_code == 422


def test_fleet_validation_bad_block_and_non_increasing_samples():
    payload = _payload(maintenance=[
        {"start": "2026-09-21T04:00:00Z", "end": "2026-09-21T03:00:00Z"}
    ])
    assert client.post("/fleet-tasks", json=payload).status_code == 422

    payload = _payload()
    payload["samples"][3]["time"] = payload["samples"][2]["time"]
    assert client.post("/fleet-tasks", json=payload).status_code == 422
