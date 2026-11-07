"""Cheapest response to pump failures (SPEC.md "Plans").

Per pump failure the options are:
  * trucks: k trucks x n trips each, all called at plan start, first arrival at response_min,
    then every cycle_min; a trip only counts if it arrives before the tank trips and before
    the repair ends; it removes min(truck_capacity_bbl, water in tank) per truck
  * shut-ins: any subset of producing wells, each from start_after_min to the repair end,
    cost = lost revenue/h x hours shut in. Searched both with one common (latest safe) start
    and staggered: cheapest $/bbl wells from the start, the last one as late as possible
  * any mix of the two
  * doing nothing: free if the tank never trips, otherwise the automatic shut-in costs the
    facility's lost revenue/h from the trip until the repair ends
All ongoing failures in one system share its fleet and are planned jointly.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .forecast import TankState, tank_state
from .loader import Dataset, Facility, truncate

EPS_BBL = 1e-6


def _hhmm(t: float) -> str:
    return datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%H:%M")


def _money(x: float) -> str:
    return f"${x:,.0f}"


@dataclass
class Plan:
    event_id: str
    facility_id: str
    start_unix: int
    horizon_min: float
    trucks: int = 0
    trips_per_truck: int = 0
    shutins: list[tuple[str, int]] = field(default_factory=list)  # (well_id, start_after_min)
    truck_cost: float = 0.0
    shutin_cost: float = 0.0
    trip_cost: float = 0.0           # automatic shut-in cost if the plan accepts the trip
    trips_at_unix: int | None = None  # when the tank reaches shutdown under this plan (None = never)
    peak_pct: float = 0.0
    trucks_free: int = 0
    explanation: str = ""
    alternatives: list[str] = field(default_factory=list)
    already_tripped: bool = False     # tank was at shutdown before the plan started

    @property
    def cost(self) -> float:
        return self.truck_cost + self.shutin_cost + self.trip_cost

    @property
    def kind(self) -> str:
        if self.already_tripped:
            return "tripped"
        if self.trips_at_unix is not None:
            return "accept_trip"
        if self.trucks and self.shutins:
            return "mix"
        if self.trucks:
            return "trucks"
        if self.shutins:
            return "shut_in"
        return "none"

    @property
    def busy_until_unix(self) -> int | None:
        return None if not self.trucks else self._busy_until

    _busy_until: int = 0

    def summary(self) -> str:
        parts = []
        if self.trucks:
            trips = (f"{self.trips_per_truck} trip" + ("s" if self.trips_per_truck > 1 else "")
                     + (" each" if self.trucks > 1 else ""))
            parts.append(f"{self.trucks} truck{'s' if self.trucks > 1 else ''}, {trips}")
        for w, s in self.shutins:
            parts.append(f"shut in {w} at +{s} min")
        if self.already_tripped:
            return f"none: already shut in until the repair (~{_money(self.cost)} lost production)"
        if not parts:
            parts.append("accept the trip" if self.trips_at_unix is not None else "none")
        return " + ".join(parts) + f" (~{_money(self.cost)})"

    def actions(self) -> list[dict]:
        acts = []
        if self.trucks:
            acts.append({"type": "dispatch_truck", "facility_id": self.facility_id,
                         "trucks": self.trucks, "trips_per_truck": self.trips_per_truck})
        by_start: dict[int, list[str]] = {}
        for w, s in self.shutins:
            by_start.setdefault(s, []).append(w)
        for s, wells in sorted(by_start.items()):
            acts.append({"type": "shut_in_wells", "wells": wells, "start_after_min": s})
        if not acts:
            acts.append({"type": "none"})
        return acts

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "facility_id": self.facility_id, "kind": self.kind,
                "summary": self.summary(), "est_cost": round(self.cost, 2),
                "truck_cost": round(self.truck_cost, 2), "shutin_cost": round(self.shutin_cost, 2),
                "trip_cost": round(self.trip_cost, 2), "trucks": self.trucks,
                "trips_per_truck": self.trips_per_truck,
                "shutins": [{"well": w, "start_after_min": s} for w, s in self.shutins],
                "trips_at_unix": self.trips_at_unix, "peak_pct": round(self.peak_pct, 1),
                "trucks_free": self.trucks_free, "busy_until_unix": self.busy_until_unix,
                "plan_start_unix": self.start_unix, "horizon_min": round(self.horizon_min, 1),
                "recommended_actions": self.actions(), "explanation": self.explanation,
                "alternatives": self.alternatives}


# --- economics -------------------------------------------------------------------------
def well_revenue_per_hour(ds: Dataset, fac: Facility) -> dict[str, float]:
    p = ds.ops["prices"]
    return {w.id: (w.oil_bpd * p["oil_per_bbl"] + w.gas_mcfd * p["gas_per_mcf"]) / 24.0
            for w in fac.wells}


def facility_revenue_per_hour(ds: Dataset, fac: Facility, st: TankState | None = None) -> float:
    rev = well_revenue_per_hour(ds, fac)
    if st is not None:
        return sum(rev[w] for w in st.well_bpd)  # producing wells only
    return sum(rev.values())


# --- simulation ------------------------------------------------------------------------
def simulate(st: TankState, horizon_min: float, arrivals: list[tuple[float, float]],
             shutins: list[tuple[float, float]]) -> tuple[float | None, float]:
    """Run the tank forward from now.

    arrivals: (minute, bbl) truck trips; shutins: (minute, bpd removed from inflow).
    Returns (minute the tank reaches shutdown or None, peak volume in bbl).
    """
    vol = st.volume_bbl
    lim = st.limit_bbl
    if vol >= lim - EPS_BBL:
        return 0.0, vol
    rate = st.inflow_bpd / 1440.0  # bbl per minute
    ev = sorted([(t, 0, b) for t, b in arrivals] + [(t, 1, q) for t, q in shutins])
    t, peak = 0.0, vol
    for te, kind, x in ev + [(horizon_min, 2, 0.0)]:
        te = min(te, horizon_min)
        if te > t:
            nv = vol + rate * (te - t)
            if nv >= lim - EPS_BBL and rate > 0:
                return t + (lim - vol) / rate, lim
            vol = nv
            peak = max(peak, vol)
            t = te
        if kind == 0 and te < horizon_min:
            vol -= min(x, vol)
        elif kind == 1:
            rate -= x / 1440.0
        elif kind == 2:
            break
    return None, peak


# --- single-event search ---------------------------------------------------------------
@dataclass
class _Ctx:
    ds: Dataset
    fac: Facility
    st: TankState
    event_id: str
    start_unix: int
    horizon_min: float
    response_min: float
    cycle_min: float
    truck_bbl: float
    trip_price: float
    rev: dict[str, float]


def _latest_start(c: _Ctx, arrivals, fixed: list[tuple[float, float]], q_last: float) -> int | None:
    """Latest whole minute to start one more shut-in of q_last bpd so the tank never trips."""
    def ok(s: float) -> bool:
        return simulate(c.st, c.horizon_min, arrivals, fixed + [(s, q_last)])[0] is None
    hi = int(math.floor(c.horizon_min))
    if not ok(0):
        return None
    if ok(hi):
        return hi
    lo = 0  # ok(lo) True, ok(hi) False
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo


def _candidates(c: _Ctx, k: int) -> list[Plan]:
    """Every valid plan using exactly k trucks (k = 0: shut-ins or nothing)."""
    out: list[Plan] = []
    trip_opts = [0] if k == 0 else [n for n in range(1, 64)
                                    if c.response_min + (n - 1) * c.cycle_min < c.horizon_min]
    if k > 0 and not trip_opts:
        return out
    wells = sorted(c.st.well_bpd, key=lambda w: c.rev[w] / max(c.st.well_bpd[w], 1e-9))
    subsets = [()] + [s for r in range(1, len(wells) + 1) for s in itertools.combinations(wells, r)]
    for n in trip_opts:
        arrivals = [(c.response_min + i * c.cycle_min, k * c.truck_bbl) for i in range(n)]
        for sub in subsets:
            shut_plans: list[list[tuple[str, int]]] = []
            if not sub:
                if simulate(c.st, c.horizon_min, arrivals, [])[0] is None:
                    shut_plans.append([])
            else:
                q = sum(c.st.well_bpd[w] for w in sub)
                s = _latest_start(c, arrivals, [], q)
                if s is not None:
                    shut_plans.append([(w, s) for w in sub])
                if len(sub) >= 2:  # staggered: cheapest $/bbl first from now, last one late
                    first, last = list(sub[:-1]), sub[-1]
                    fixed = [(0.0, c.st.well_bpd[w]) for w in first]
                    s2 = _latest_start(c, arrivals, fixed, c.st.well_bpd[last])
                    if s2 is not None and s2 > 0:
                        shut_plans.append([(w, 0) for w in first] + [(last, s2)])
            for sp in shut_plans:
                if k == 0 and not sp:
                    continue  # "do nothing" is handled separately
                p = _make_plan(c, k, n, sp)
                out.append(p)
    return out


def _make_plan(c: _Ctx, k: int, n: int, shutins: list[tuple[str, int]]) -> Plan:
    arrivals = [(c.response_min + i * c.cycle_min, k * c.truck_bbl) for i in range(n)] if k else []
    shut = [(s, c.st.well_bpd[w]) for w, s in shutins]
    trip, peak = simulate(c.st, c.horizon_min, arrivals, shut)
    p = Plan(event_id=c.event_id, facility_id=c.fac.id, start_unix=c.start_unix,
             horizon_min=c.horizon_min, trucks=k, trips_per_truck=n if k else 0, shutins=shutins,
             truck_cost=k * n * c.trip_price,
             shutin_cost=sum(c.rev[w] * (c.horizon_min - s) / 60.0 for w, s in shutins),
             peak_pct=peak / c.st.capacity_bbl * 100)
    if trip is not None:
        p.trips_at_unix = int(c.start_unix + trip * 60)
        auto = facility_revenue_per_hour(c.ds, c.fac, c.st) - sum(c.rev[w] for w, _ in shutins)
        p.trip_cost = auto * (c.horizon_min - trip) / 60.0
    if k:
        p._busy_until = int(c.start_unix + (c.response_min + n * c.cycle_min) * 60)
    return p


def _already_tripped(c: _Ctx) -> Plan:
    """Tank already at shutdown: wells are auto shut in until the repair; nothing can help."""
    p = Plan(event_id=c.event_id, facility_id=c.fac.id, start_unix=c.start_unix,
             horizon_min=c.horizon_min, trips_at_unix=c.start_unix,
             peak_pct=c.st.level_pct, already_tripped=True)
    p.trip_cost = facility_revenue_per_hour(c.ds, c.fac) * c.horizon_min / 60.0
    return p


def _do_nothing(c: _Ctx) -> Plan:
    return _make_plan(c, 0, 0, [])


def _plan_key(p: Plan):
    # cheapest first; ties: fewer trucks, fewer trips, fewer wells, later shut-in
    return (round(p.cost, 2), p.trucks, p.trucks * p.trips_per_truck, len(p.shutins),
            -min([s for _, s in p.shutins], default=0))


def _context(ds: Dataset, event, start_unix: int) -> _Ctx | None:
    fac = ds.facilities[event.facility_id]
    repair_end = event.onset_unix + ds.ops["expected_repair_hours"]["pump_failure"] * 3600
    horizon = (repair_end - start_unix) / 60.0
    if horizon <= 0:
        return None
    so = ds.ops["systems"][fac.system_id]["facilities"][fac.id]
    tr = ds.ops["emergency_trucking"]
    return _Ctx(ds, fac, tank_state(ds, fac), event.event_id, start_unix, horizon,
                so["response_min"], so["cycle_min"], tr["truck_capacity_bbl"], tr["cost_per_trip"],
                well_revenue_per_hour(ds, fac))


def _best_by_trucks(c: _Ctx, max_trucks: int) -> dict[int, Plan]:
    """Cheapest plan for each exact truck count 0..max_trucks (0 includes doing nothing)."""
    if c.st.tripped:
        return {0: _already_tripped(c)}
    best: dict[int, Plan] = {}
    for k in range(0, max_trucks + 1):
        cands = _candidates(c, k)
        if k == 0:
            cands.append(_do_nothing(c))
        valid = [p for p in cands if p.trips_at_unix is None or (k == 0 and not p.shutins)]
        if valid:
            best[k] = min(valid, key=_plan_key)
    return best


# --- system-level joint planning -------------------------------------------------------
def plan_system(ds: Dataset, events: list, trucks_free: int, start_unix: int | None = None) -> dict[str, Plan]:
    """Jointly plan the given ongoing pump failures of one system sharing trucks_free trucks."""
    start = ds.now_unix if start_unix is None else start_unix
    ctxs = [c for c in (_context(ds, e, start) for e in events) if c is not None]
    if not ctxs:
        return {}
    options = [_best_by_trucks(c, trucks_free) for c in ctxs]
    best_combo, best_key = None, None
    for alloc in itertools.product(*[sorted(o) for o in options]):
        if sum(alloc) > trucks_free:
            continue
        plans = [o[k] for o, k in zip(options, alloc)]
        key = (round(sum(p.cost for p in plans), 2), sum(alloc),
               sum(p.trucks * p.trips_per_truck for p in plans))
        if best_key is None or key < best_key:
            best_key, best_combo = key, plans
    out = {}
    for c, opts, p in zip(ctxs, options, best_combo):
        p.trucks_free = trucks_free
        p.explanation = _explain(c, p, trucks_free, len(ctxs))
        p.alternatives = [f"{alt.summary()}" for k, alt in sorted(opts.items())
                          if alt is not p][:3]
        out[c.event_id] = p
    return out


def _explain(c: _Ctx, p: Plan, free: int, n_events: int) -> str:
    st = c.st
    end = c.start_unix + c.horizon_min * 60
    if p.already_tripped:
        return (f"{c.fac.id} tank is at {st.level_pct:.1f}%, already at the {st.shutdown_pct:.0f}% "
                f"shutdown limit, so its wells are shut in automatically until the repair at "
                f"{_hhmm(end)}. Trucks or shut-ins can't restart them earlier; "
                f"{_money(p.trip_cost)} of production is lost in the meantime. Nothing to dispatch.")
    base_trip, _ = simulate(st, c.horizon_min, [], [])
    head = (f"{c.fac.id} tank at {st.level_pct:.1f}% filling {st.inflow_bpd / 24 / st.capacity_bbl * 100:.1f} "
            f"pts/h; repair due {_hhmm(end)}. ")
    if base_trip is None:
        return head + f"It peaks around {p.peak_pct:.1f}% and never reaches {st.shutdown_pct:.0f}%, so no action is needed."
    head += f"Left alone it hits {st.shutdown_pct:.0f}% at {_hhmm(c.start_unix + base_trip * 60)}. "
    bits = []
    if p.trucks:
        arr = [c.start_unix + (c.response_min + i * c.cycle_min) * 60 for i in range(p.trips_per_truck)]
        bits.append(f"send {p.trucks} truck{'s' if p.trucks > 1 else ''} now, arriving "
                    + ", ".join(_hhmm(a) for a in arr)
                    + f" ({p.trucks * p.trips_per_truck * c.truck_bbl:.0f} bbl, {_money(p.truck_cost)})")
    for w, s in p.shutins:
        bits.append(f"shut in {w} at {_hhmm(c.start_unix + s * 60)} (+{s} min, "
                    f"{_money(c.rev[w])}/h lost)")
    if p.kind == "accept_trip":
        text = head + (f"Every way to prevent the trip costs more than the automatic shut-in "
                       f"({_money(p.trip_cost)} of lost production), so accept it.")
    else:
        if p.peak_pct > st.shutdown_pct - 0.1:
            peak = f"Level tops out just under {st.shutdown_pct:.0f}% as the repair finishes"
        else:
            peak = f"Peak {p.peak_pct:.1f}%"
        nothing = _do_nothing(c)
        text = (head + "Plan: " + "; ".join(bits) + f". {peak}, total {_money(p.cost)} "
                f"vs {_money(nothing.cost)} of lost production if it trips.")
    if n_events > 1:
        text += f" Planned together with {n_events - 1} other failure(s) sharing {free} truck(s)."
    elif free < c.ds.ops["systems"][c.fac.system_id]["emergency_fleet_size"]:
        fleet = c.ds.ops["systems"][c.fac.system_id]["emergency_fleet_size"]
        text += f" {free} of {fleet} trucks free; the rest are on earlier jobs."
    return text


def fleet_size(ds: Dataset, system_id: str) -> int:
    return int(ds.ops["systems"][system_id]["emergency_fleet_size"])


def plan_ongoing(ds: Dataset, events: list) -> dict[str, Plan]:
    """Plans from "now" for every ongoing pump failure; nothing is dispatched yet, so each
    system's whole fleet is shared by its ongoing failures."""
    by_sys: dict[str, list] = {}
    for e in events:
        if e.category == "pump_failure" and e.status == "ongoing":
            by_sys.setdefault(e.system_id, []).append(e)
    out = {}
    for sid, evs in by_sys.items():
        out.update(plan_system(ds, evs, fleet_size(ds, sid)))
    return out


def replay_onset_plans(ds: Dataset, events: list) -> dict[str, Plan]:
    """Plan every pump failure at its own onset, as the operator would have, assuming the
    earlier plans were followed: trucks they sent stay busy until back from their last trip."""
    busy: dict[str, list[int]] = {}
    out = {}
    for e in sorted((e for e in events if e.category == "pump_failure"), key=lambda e: e.onset_unix):
        cut = truncate(ds, e.onset_unix)
        fleet = fleet_size(ds, e.system_id)
        in_use = [b for b in busy.get(e.system_id, []) if b > e.onset_unix]
        free = max(fleet - len(in_use), 0)
        plans = plan_system(cut, [e], free, start_unix=e.onset_unix)
        p = plans.get(e.event_id)
        if p is None:
            continue
        if p.trucks:
            busy.setdefault(e.system_id, []).extend([p.busy_until_unix] * p.trucks)
        out[e.event_id] = p
    return out
