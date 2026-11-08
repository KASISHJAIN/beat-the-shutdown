"""Alerts view (SPEC.md "Alerts view") and events.json export (SPEC.md "events.json").

Alerts: every problem still in progress, with its action.
  1. pump failures, smallest hours_to_limit (p50) first; ties -> higher lost revenue per hour
  2. then leaks and worn pumps (field inspection), oldest first
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .detect import Event, detect_events
from .forecast import hours_to_limit, tank_state
from .loader import Dataset, load_dataset
from .optimize import Plan, facility_revenue_per_hour, plan_ongoing, replay_onset_plans


def _event_record(ds: Dataset, e: Event, plan: Plan | None, ongoing_failure: bool) -> dict:
    fac = ds.facilities[e.facility_id]
    rec = {
        "event_id": e.event_id, "system_id": e.system_id, "facility_id": e.facility_id,
        "asset_id": e.asset_id, "category": e.category, "status": e.status,
        "onset_ts": e.onset_ts, "onset_unix": e.onset_unix,
        "end_ts": e.end_ts, "end_unix": e.end_unix,
        "confidence": e.confidence, "evidence": list(e.evidence),
        "level_pct": None, "hours_to_limit": None, "recommended_actions": [],
    }
    if e.category == "pump_failure":
        if ongoing_failure:
            st = tank_state(ds, fac)
            rec["level_pct"] = round(st.level_pct, 1)
            rec["hours_to_limit"] = hours_to_limit(st)
        # the facility's whole production (what a trip shuts in); also the alerts tie-break
        rec["lost_revenue_per_hour"] = round(facility_revenue_per_hour(ds, fac), 2)
        if plan is not None:
            rec["recommended_actions"] = plan.actions()
            rec["est_cost"] = round(plan.cost, 2)
            rec["plan"] = plan.to_dict()
            rec["evidence"].append(plan.explanation)
        else:
            rec["recommended_actions"] = [{"type": "none"}]
    else:
        rec["recommended_actions"] = [{"type": "field_inspection", "asset_id": e.asset_id}]
        if e.unverified:
            rec["unverified"] = True
    return rec


def build(ds: Dataset, events: list[Event] | None = None, include_resolved_plans: bool = True) -> dict:
    """Everything the UI needs: ordered alerts (ongoing only) and all events with actions."""
    events = detect_events(ds) if events is None else events
    now_plans = plan_ongoing(ds, events)
    onset_plans = replay_onset_plans(ds, events) if include_resolved_plans else {}
    records = []
    for e in events:
        ongoing_fail = e.category == "pump_failure" and e.status == "ongoing"
        plan = now_plans.get(e.event_id) if ongoing_fail else onset_plans.get(e.event_id)
        records.append(_event_record(ds, e, plan, ongoing_fail))
    alerts = order_alerts([r for r in records if r["status"] == "ongoing"])
    return {"dataset": ds.path.name, "now_unix": ds.now_unix, "alerts": alerts, "events": records}


def order_alerts(ongoing: list[dict]) -> list[dict]:
    def key(r: dict):
        if r["category"] == "pump_failure":
            h = (r.get("hours_to_limit") or {}).get("p50")
            return (0, h if h is not None else float("inf"), -r.get("lost_revenue_per_hour", 0), r["onset_unix"])
        return (1, 0, 0, r["onset_unix"])
    out = sorted(ongoing, key=key)
    for i, r in enumerate(out, 1):
        r["order"] = i
    return out


def export_events(ds: Dataset, path: str | Path | None = None) -> list[dict]:
    """events.json in the SPEC shape (plus a few extra informational fields)."""
    data = build(ds)["events"]
    if path is not None:
        Path(path).write_text(json.dumps(data, indent=2))
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="Detect, forecast and plan; write events.json")
    ap.add_argument("dataset", help="dataset folder (network.json, ops.json, telemetry.csv)")
    ap.add_argument("-o", "--out", default="events.json")
    ap.add_argument("--alerts", action="store_true", help="also print the alerts view")
    a = ap.parse_args()
    ds = load_dataset(a.dataset)
    out = build(ds)
    Path(a.out).write_text(json.dumps(out["events"], indent=2))
    print(f"wrote {len(out['events'])} events to {a.out}")
    if a.alerts:
        for r in out["alerts"]:
            h = (r.get("hours_to_limit") or {}).get("p50")
            act = r.get("plan", {}).get("summary") or r["recommended_actions"][0]["type"]
            print(f"  {r['order']:2}. {r['asset_id']:15} {r['category']:17} "
                  f"{'' if h is None else f'{h:5.2f} h':>7}  {act}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
