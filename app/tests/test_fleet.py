"""Unit tests for fleet coordination: per-vessel windows, common windows and
the exclusion labelling."""

from datetime import datetime, timedelta, timezone

from app.services.fleet import VesselSpec, compute_fleet_task


def dt(hours: float) -> datetime:
    return datetime.fromtimestamp(hours * 3600, tz=timezone.utc)


TRIANGLE_HEIGHTS = [0, 2, 4, 6, 8, 10, 8, 6, 4, 2, 0, 2, 4]
# Threshold 5 safe window is exactly [02:30, 07:30].
TIMES = [dt(h) for h in range(13)]


def _vessel(name, draft, clearance, transit):
    return VesselSpec(
        name=name,
        draft_m=draft,
        clearance_m=clearance,
        transit_duration_seconds=transit,
    )


def _fleet(vessels, maintenance=None, heights=None, query_end=12):
    return compute_fleet_task(
        vessels=vessels,
        sample_times=TIMES if heights is None else [dt(h) for h in range(len(heights))],
        sample_heights=heights or TRIANGLE_HEIGHTS,
        query_start=dt(0),
        query_end=dt(query_end),
        max_gap_seconds=4000,
        maintenance_blocks=maintenance,
    )


def test_single_vessel_common_equals_its_departure_windows():
    res = _fleet([_vessel("A", 4.0, 1.0, 3600)])
    assert len(res.vessels) == 1
    v = res.vessels[0]
    assert v.threshold_m == 5.0
    assert v.departure_windows == res.common_departure_windows
    # One vessel blocks nothing extra: every stretch it cannot cover is simply
    # outside its own domain, so no exclusions are reported.
    assert res.exclusions == []
    assert res.unknown == []
    # Common departure window: safe [02:30, 07:30] minus the 1 h occupancy.
    assert [(w.start, w.end) for w in res.common_departure_windows] == [
        (dt(2.5), dt(6.5))
    ]


def test_common_is_intersection_of_different_transit_durations():
    # Same safe window [02:30, 07:30]; A needs 1 h, B needs 2 h.
    res = _fleet([
        _vessel("A", 4.0, 1.0, 3600),
        _vessel("B", 4.0, 1.0, 7200),
    ])
    a_deps = res.vessels[0].departure_windows
    b_deps = res.vessels[1].departure_windows
    assert [(w.start, w.end) for w in a_deps] == [(dt(2.5), dt(6.5))]
    assert [(w.start, w.end) for w in b_deps] == [(dt(2.5), dt(5.5))]
    # The common set is the intersection; B rules out (05:30, 06:30].
    common = [(w.start, w.end) for w in res.common_departure_windows]
    assert common == [(dt(2.5), dt(5.5))]
    assert len(res.exclusions) == 1
    ex = res.exclusions[0]
    assert (ex.start, ex.end) == (dt(5.5), dt(6.5))
    assert ex.vessel_names == ["B"]
    assert ex.duration_seconds == 3600


def test_different_drafts_change_safe_windows_and_label_blocker():
    # Shallow threshold 5 -> safe window exactly [02:30, 07:30]; the deep
    # ship's threshold-9 window is shorter and centred on the 05:00 peak.
    res = _fleet([
        _vessel("shallow", 4.0, 1.0, 3600),
        _vessel("deep", 8.0, 1.0, 3600),
    ])
    deep = next(v for v in res.vessels if v.name == "deep")
    shallow = next(v for v in res.vessels if v.name == "shallow")
    assert [(w.start, w.end) for w in shallow.departure_windows] == [
        (dt(2.5), dt(6.5))
    ]
    # The deep ship has one short departure window inside the high-water peak.
    assert len(deep.windows) == 1
    assert len(deep.departure_windows) == 1
    deep_dep = deep.departure_windows[0]
    safe = deep.windows[0]
    assert deep_dep.start == safe.start
    assert deep_dep.end + timedelta(seconds=3600) == safe.end

    # The common set is exactly the deep ship's (more restrictive) window;
    # the shallow-only tails are exclusions caused by the deep ship.
    common = [(w.start, w.end) for w in res.common_departure_windows]
    assert common == [(deep_dep.start, deep_dep.end)]
    assert [(x.start, x.end, x.vessel_names) for x in res.exclusions] == [
        (dt(2.5), deep_dep.start, ["deep"]),
        (deep_dep.end, dt(6.5), ["deep"]),
    ]


def test_vessel_unable_to_transit_marks_whole_other_domain_excluded():
    # The deep ship's threshold-9 safe window (~1.2 h) is shorter than its
    # 6-hour transit, so it has no departure interval at all: the shallow
    # ship's whole feasible domain is then an exclusion blocked by it.
    res = _fleet([
        _vessel("shallow", 4.0, 1.0, 3600),
        _vessel("deep", 8.0, 1.0, 6 * 3600),
    ])
    deep = next(v for v in res.vessels if v.name == "deep")
    assert deep.departure_windows == []
    assert res.common_departure_windows == []
    assert [(x.start, x.end, x.vessel_names) for x in res.exclusions] == [
        (dt(2.5), dt(6.5), ["deep"])
    ]


def test_maintenance_block_is_shared_constraint_not_an_exclusion():
    # Closure [04:00, 05:00) bars both vessels' departures in (03:00, 05:00):
    # a stretch where NO vessel can depart is omitted from exclusions.
    res = _fleet(
        [
            _vessel("A", 4.0, 1.0, 3600),
            _vessel("B", 4.0, 1.0, 7200),
        ],
        maintenance=[(dt(4), dt(5))],
    )
    deps = {
        v.name: [(w.start, w.end) for w in v.departure_windows]
        for v in res.vessels
    }
    assert deps["A"] == [(dt(2.5), dt(3)), (dt(5), dt(6.5))]
    assert deps["B"] == [(dt(5), dt(5.5))]
    # Common = [05:00, 05:30]; the A-only stretch [05:30, 06:30] is excluded
    # by B; the (02:30, 03:00] stretch is A-only as well (B is barred by the
    # shared closure), hence it is also an exclusion caused by B.
    common = [(w.start, w.end) for w in res.common_departure_windows]
    assert common == [(dt(5), dt(5.5))]
    assert [(x.start, x.end, x.vessel_names) for x in res.exclusions] == [
        (dt(2.5), dt(3), ["B"]),
        (dt(5.5), dt(6.5), ["B"]),
    ]


def test_unknown_gap_is_a_fleet_wide_constraint():
    # Two safe plateaus separated by a 3-hour forbidden gap; both vessels
    # share the unknown stretch, which must never appear as a common window
    # nor as a vessel-labelled exclusion.
    times = [dt(h) for h in (0, 1, 2, 5, 6, 7)]
    heights = [8, 9, 8, 8, 9, 8]
    res = compute_fleet_task(
        vessels=[
            _vessel("A", 5.0, 0.0, 3600),
            _vessel("B", 5.0, 0.0, 1800),
        ],
        sample_times=times,
        sample_heights=heights,
        query_start=dt(0),
        query_end=dt(7),
        max_gap_seconds=3660,
    )
    reasons = {u.reason for u in res.unknown}
    assert "sample_gap_exceeds_max_gap" in reasons
    # A dep: [00,01] and [05,06]; B dep: [00,01:30] and [05,06:30].
    common = [(w.start, w.end) for w in res.common_departure_windows]
    assert common == [(dt(0), dt(1)), (dt(5), dt(6))]
    # The half-hour tails are blocked by A (longer transit).
    assert [(x.start, x.end, x.vessel_names) for x in res.exclusions] == [
        (dt(1), datetime.fromtimestamp(1.5 * 3600, tz=timezone.utc), ["A"]),
        (dt(6), datetime.fromtimestamp(6.5 * 3600, tz=timezone.utc), ["A"]),
    ]
    # Nothing crosses the forbidden gap [02:00, 05:00).
    for w in res.common_departure_windows:
        assert w.end <= dt(2) or w.start >= dt(5)


def test_multiple_vessels_block_one_stretch():
    # A: 1 h transit -> dep [02:30, 06:30]; B & C: 3 h transit -> [02:30, 04:30]
    res = _fleet([
        _vessel("A", 4.0, 1.0, 3600),
        _vessel("B", 4.0, 1.0, 3 * 3600),
        _vessel("C", 4.0, 1.0, 3 * 3600),
    ])
    common = [(w.start, w.end) for w in res.common_departure_windows]
    assert common == [(dt(2.5), dt(4.5))]
    assert [(x.start, x.end, x.vessel_names) for x in res.exclusions] == [
        (dt(4.5), dt(6.5), ["B", "C"])
    ]


def test_durations_are_consistent_and_vessel_order_preserved():
    res = _fleet([
        _vessel("first", 4.0, 1.0, 7200),
        _vessel("second", 4.0, 1.0, 3600),
    ])
    assert [v.name for v in res.vessels] == ["first", "second"]
    for w in res.common_departure_windows:
        assert w.duration_seconds == int(
            (w.end - w.start).total_seconds()
        )
    for x in res.exclusions:
        assert x.duration_seconds == int((x.end - x.start).total_seconds())
        # Labels follow the request order.
        assert x.vessel_names == ["first"]
