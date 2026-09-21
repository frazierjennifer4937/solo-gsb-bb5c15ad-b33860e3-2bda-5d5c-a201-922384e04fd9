"""Fleet coordination: common departure windows for several vessels.

Each vessel has its own draft, under-keel clearance requirement and transit
duration, but the whole fleet shares one tide sample set, one query range, one
``max_gap_seconds`` policy and one set of maintenance closures.

Per vessel we run the ordinary single-vessel computation
(:func:`app.services.tides.compute_navigation_windows`), which already treats
unknown stretches (sample gaps over ``max_gap_seconds`` and areas without
sample coverage) and maintenance closures as hard constraints on the
departure windows.  Because the sample inputs are shared, the resulting
``unknown`` intervals are identical for every vessel and are reported once.

The fleet-level result is then obtained by sweeping an event grid built from
every vessel's departure-window endpoints:

* a stretch where *every* vessel can depart is a **common departure window**;
* a stretch covered by at least one vessel but not all of them is an
  **exclusion** carrying the names of the vessels that cannot complete their
  transit there (i.e. the vessels that rule that stretch out of the common
  departure set);
* a stretch where no vessel can depart (for instance a shared unknown sample
  gap or a shared maintenance closure) is simply not part of the candidate
  domain and is not reported as an exclusion.

All single-vessel departure endpoints are already conservative whole-second
values, so the fleet outputs are whole-second values as well.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from app.services.tides import (
    DepartureWindow,
    NavigationResult,
    UnknownInterval,
    Window,
    compute_navigation_windows,
    to_datetime,
    to_epoch,
)


@dataclass(frozen=True)
class VesselSpec:
    """One vessel's individual parameters."""

    name: str
    draft_m: float
    clearance_m: float
    transit_duration_seconds: float


@dataclass(frozen=True)
class FleetVesselResult:
    """Single-vessel windows inside a fleet result."""

    name: str
    threshold_m: float
    windows: list[Window]
    departure_windows: list[DepartureWindow]


@dataclass(frozen=True)
class FleetExclusion:
    """A stretch ruled out of the common set by one or more vessels."""

    start: datetime
    end: datetime
    # Vessels that cannot complete their transit on this stretch.
    vessel_names: list[str]
    duration_seconds: int


@dataclass(frozen=True)
class FleetResult:
    vessels: list[FleetVesselResult]
    unknown: list[UnknownInterval]
    common_departure_windows: list[DepartureWindow]
    exclusions: list[FleetExclusion]


def _merge_touching(intervals: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge closed intervals that overlap or touch (endpoints are departable)."""
    merged: list[list[float]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _fleet_sweep(
    departures_by_vessel: dict[str, list[tuple[float, float]]],
    names: Sequence[str],
) -> tuple[list[tuple[float, float]], list[tuple[float, float, list[str]]]]:
    """Sweep the endpoint event grid.

    Returns ``(common_intervals, exclusion_intervals)`` where each exclusion
    is ``(start, end, blocking_vessel_names)``.  The sweep distinguishes
    three classes for every timeline cell:

    * covered by every vessel      -> common;
    * covered by some vessel       -> exclusion labelled with the blocked
      vessels (vessels without a covering departure interval);
    * covered by nobody            -> outside the candidate domain.

    Adjacent open cells of the same class (same blocking set) are merged.
    Event-point cells only split runs when they change the classification; in
    particular an instant where every vessel can depart (a common point) does
    not split an exclusion, because endpoint departures are inclusive and a
    zero-length instant is not itself a usable departure interval.
    """
    per_vessel = {name: _merge_touching(departures_by_vessel.get(name, []))
                  for name in names}

    def covering(x: float) -> set[str]:
        return {
            name
            for name, intervals in per_vessel.items()
            if any(s <= x <= e for s, e in intervals)
        }

    events = sorted({
        x
        for intervals in per_vessel.values()
        for s, e in intervals
        for x in (s, e)
    })

    all_names = set(names)

    def _key(covered: set[str]) -> tuple:
        if covered == all_names:
            return ("common",)
        if covered:
            return ("blocked", tuple(n for n in names if n not in covered))
        return ("none",)

    # Classify point cells (at every event) and open-interior cells (between
    # consecutive distinct events).
    point_keys = [_key(covering(x)) for x in events]
    open_cells_k: list[tuple[float, float, tuple]] = [
        (a, b, _key(covering((a + b) / 2.0)))
        for a, b in zip(events, events[1:])
    ]

    # A point cell is absorbed (no boundary) when the open stretches on both
    # sides share a class K and either the point itself is K, or K is an
    # exclusion while the point is common — an instant where every vessel can
    # depart is inclusive and does not split an exclusion into two pieces.
    # All other points are kept as zero-length cells (a "none" point keeps a
    # shared closure/gap open; a different blocked set keeps exclusions
    # separated).
    cells: list[tuple[float, float, tuple]] = list(open_cells_k)
    for i, x in enumerate(events):
        pk = point_keys[i]
        lk = open_cells_k[i - 1][2] if i > 0 else None
        rk = open_cells_k[i][2] if i < len(open_cells_k) else None
        absorbed = (
            lk is not None and lk == rk
            and (pk == lk or (lk[0] == "blocked" and pk == ("common",)))
        )
        if not absorbed:
            cells.append((x, x, pk))
    cells.sort(key=lambda c: (c[0], c[1]))

    common: list[tuple[float, float]] = []
    exclusions: list[tuple[float, float, list[str]]] = []

    run_lo: float | None = None
    run_hi: float | None = None
    run_key: tuple | None = None

    def _flush() -> None:
        nonlocal run_lo, run_hi, run_key
        if run_key is not None and run_lo is not None and run_hi is not None:
            if run_hi > run_lo:
                if run_key[0] == "common":
                    common.append((run_lo, run_hi))
                elif run_key[0] == "blocked":
                    exclusions.append((run_lo, run_hi, list(run_key[1])))
        run_lo = run_hi = run_key = None

    for lo, hi, key in cells:
        if key == ("none",):
            _flush()
            continue
        if run_key != key:
            _flush()
            run_lo, run_hi, run_key = lo, hi, key
        elif hi > (run_hi or lo):
            run_hi = hi
    _flush()

    common.sort()
    exclusions.sort(key=lambda x: (x[0], x[1]))
    return common, exclusions


def compute_fleet_task(
    vessels: Sequence[VesselSpec],
    sample_times: Sequence[datetime],
    sample_heights: Sequence[float],
    query_start: datetime,
    query_end: datetime,
    max_gap_seconds: float,
    maintenance_blocks: Sequence[tuple[datetime, datetime]] | None = None,
) -> FleetResult:
    """Compute per-vessel windows and the fleet's common departure windows.

    Unknown stretches and maintenance closures are shared constraints: they
    enter every vessel's departure-window computation, so a departure reported
    in a common window is feasible for the whole fleet simultaneously.
    """
    if not vessels:
        raise ValueError("vessels 不能为空")

    vessel_results: list[tuple[VesselSpec, NavigationResult]] = []
    for vessel in vessels:
        result = compute_navigation_windows(
            sample_times=sample_times,
            sample_heights=sample_heights,
            query_start=query_start,
            query_end=query_end,
            draft_m=vessel.draft_m,
            clearance_m=vessel.clearance_m,
            max_gap_seconds=max_gap_seconds,
            # A transit duration is mandatory for fleet tasks: the fleet goal
            # is a common *departure* interval, not just a common high tide.
            transit_duration_seconds=vessel.transit_duration_seconds,
            maintenance_blocks=maintenance_blocks,
        )
        vessel_results.append((vessel, result))

    # Identical sample/query/gap inputs make the unknown intervals identical
    # for every vessel; report them once as the shared fleet constraint.
    shared_unknown = vessel_results[0][1].unknown

    departures_by_vessel: dict[str, list[tuple[float, float]]] = {}
    fleet_vessels: list[FleetVesselResult] = []
    names = [v.name for v in vessels]
    for vessel, result in vessel_results:
        departures_by_vessel[vessel.name] = [
            (to_epoch(d.start), to_epoch(d.end))
            for d in (result.departure_windows or [])
        ]
        fleet_vessels.append(
            FleetVesselResult(
                name=vessel.name,
                threshold_m=result.threshold_m,
                windows=result.windows,
                departure_windows=result.departure_windows or [],
            )
        )

    common_raw, exclusions_raw = _fleet_sweep(departures_by_vessel, names)
    common_windows = [
        DepartureWindow(
            start=to_datetime(a),
            end=to_datetime(b),
            duration_seconds=int(b - a),
        )
        for a, b in common_raw
    ]
    exclusions = [
        FleetExclusion(
            start=to_datetime(a),
            end=to_datetime(b),
            vessel_names=blocked,
            duration_seconds=int(b - a),
        )
        for a, b, blocked in exclusions_raw
    ]

    return FleetResult(
        vessels=fleet_vessels,
        unknown=shared_unknown,
        common_departure_windows=common_windows,
        exclusions=exclusions,
    )
