import os
import tempfile
from datetime import datetime, timedelta

import pytest

# Point the database at a throw-away file before importing the app.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TIDE_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _payload():
    return {
        "samples": [
            {"time": f"2026-09-21T{h:02d}:00:00Z", "height_m": h_m}
            for h, h_m in [
                (0, 1.0), (1, 3.0), (2, 6.0), (3, 8.0),
                (4, 8.0), (5, 6.0), (6, 3.0), (7, 1.0),
            ]
        ],
        "query_start": "2026-09-21T00:00:00Z",
        "query_end": "2026-09-21T07:00:00Z",
        "draft_m": 4.0,
        "clearance_m": 1.0,
        "max_gap_seconds": 3700,
    }


def test_create_and_get_task(client):
    resp = client.post("/tasks", json=_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    task_id = body["task_id"]
    assert body["status"] in {"pending", "done"}
    assert body["request"]["draft_m"] == 4.0

    got = client.get(f"/tasks/{task_id}")
    assert got.status_code == 200
    result = got.json()["result"]
    assert got.json()["status"] == "done"
    assert result["threshold_m"] == 5.0
    assert result["unknown"] == []
    assert len(result["windows"]) == 1
    w = result["windows"][0]
    # Threshold crossings lie inside (01:00,02:00) and (05:00,06:00).
    assert w["start"].startswith("2026-09-21T01:")
    assert w["end"].startswith("2026-09-21T05:")
    assert w["min_height_m"] >= 5.0


def test_task_not_found(client):
    assert client.get("/tasks/doesnotexist").status_code == 404


def test_validation_rejects_non_increasing(client):
    payload = _payload()
    payload["samples"][3]["time"] = payload["samples"][2]["time"]
    resp = client.post("/tasks", json=payload)
    assert resp.status_code == 422
    assert "递增" in resp.text


def test_gap_returns_unknown(client):
    payload = _payload()
    payload["samples"] = [
        {"time": "2026-09-21T00:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T01:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T05:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T06:00:00Z", "height_m": 8.0},
    ]
    resp = client.post("/tasks", json=payload)
    assert resp.status_code == 201, resp.text
    result = client.get(f"/tasks/{resp.json()['task_id']}").json()["result"]
    assert len(result["windows"]) == 2
    reasons = {u["reason"] for u in result["unknown"]}
    assert "sample_gap_exceeds_max_gap" in reasons


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def test_departure_windows_end_to_end(client):
    payload = _payload()
    payload["transit_duration_seconds"] = 1800
    payload["maintenance_blocks"] = [
        {"start": "2026-09-21T03:00:00Z", "end": "2026-09-21T04:00:00Z"}
    ]
    resp = client.post("/tasks", json=payload)
    assert resp.status_code == 201, resp.text
    result = client.get(f"/tasks/{resp.json()['task_id']}").json()["result"]

    assert len(result["windows"]) == 1
    w = result["windows"][0]
    deps = result["departure_windows"]
    assert deps is not None and len(deps) == 2
    # Closure [03:00, 04:00) shifted back by the 30 min transit bars
    # departures in (02:30, 04:00); the cut points remain departable.
    assert deps[0]["start"] == w["start"]
    assert deps[0]["end"] == "2026-09-21T02:30:00Z"
    assert deps[1]["start"] == "2026-09-21T04:00:00Z"
    # Every departure's occupancy [t, t+1800) fits inside the safe window.
    assert _parse(deps[1]["end"]) == _parse(w["end"]) - timedelta(seconds=1800)
    assert deps[0]["duration_seconds"] == int(
        (_parse(deps[0]["end"]) - _parse(deps[0]["start"])).total_seconds()
    )


def test_departure_windows_omitted_by_default(client):
    resp = client.post("/tasks", json=_payload())
    assert resp.status_code == 201, resp.text
    result = client.get(f"/tasks/{resp.json()['task_id']}").json()["result"]
    assert result["departure_windows"] is None


def test_maintenance_requires_transit_duration(client):
    payload = _payload()
    payload["maintenance_blocks"] = [
        {"start": "2026-09-21T03:00:00Z", "end": "2026-09-21T04:00:00Z"}
    ]
    resp = client.post("/tasks", json=payload)
    assert resp.status_code == 422
    assert "transit_duration_seconds" in resp.text


def test_maintenance_block_end_after_start(client):
    payload = _payload()
    payload["transit_duration_seconds"] = 1800
    payload["maintenance_blocks"] = [
        {"start": "2026-09-21T04:00:00Z", "end": "2026-09-21T03:00:00Z"}
    ]
    assert client.post("/tasks", json=payload).status_code == 422
