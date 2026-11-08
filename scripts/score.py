"""Grade detection, forecasts, plans and alert order against every answer_key.json.

Usage: python scripts/score.py [ROOT] [--tol-hours H] [-v] [--sweep HOURS]

The only file in this project allowed to read answer_key.json.
Detection: same asset and category, onset within the tolerance, one-to-one (closest first).
Plans:     full_week plans each pump failure at its onset with earlier plans holding their
           trucks; snapshots plan every ongoing failure from "now". A plan matches when it
           uses the same trucks, trips per truck and wells as reference_action, the shut-in
           starts within START_TOL_MIN and the cost is within COST_TOL.
Exit code 0 when every folder passes detection, every plan matches and alert order matches.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.alerts import build as build_alerts  # noqa: E402
from backend.detect import detect_events  # noqa: E402
from backend.loader import load_dataset, truncate  # noqa: E402
from backend.optimize import plan_ongoing, replay_onset_plans  # noqa: E402

START_TOL_MIN = 3      # shut-in start: the level at onset is only known to ~0.05 pts (~1 min)
COST_TOL = (0.02, 25)  # relative, absolute $
WELL_RE = re.compile(r"[A-Za-z0-9]+_WELL_[A-Za-z0-9]+")


def _fmt_min(sec: float | None) -> str:
    return "   -  " if sec is None else f"{sec / 60:+6.0f}"


def match(truth: list[dict], found: list, tol_s: float):
    pairs = []
    for ti, t in enumerate(truth):
        for fi, f in enumerate(found):
            if f.asset_id == t["asset"] and f.category == t["category"]:
                d = abs(f.onset_unix - t["onset_unix"])
                if d <= tol_s:
                    pairs.append((d, ti, fi))
    pairs.sort()
    used_t, used_f, matched = set(), set(), []
    for d, ti, fi in pairs:
        if ti in used_t or fi in used_f:
            continue
        used_t.add(ti)
        used_f.add(fi)
        matched.append((truth[ti], found[fi]))
    missed = [t for i, t in enumerate(truth) if i not in used_t]
    false = [f for i, f in enumerate(found) if i not in used_f]
    return matched, missed, false


# --- plans -----------------------------------------------------------------------------
def _ref_core(text: str) -> str:
    """reference_action without the trailing '(shares ...)' / '(1 of 2 trucks free ...)' note."""
    return re.split(r"\s\((?:shares|\d+ of \d+ trucks)", text.strip())[0]


def parse_reference(text: str) -> dict:
    """Parse '1 truck, 2 trips (~$1,810)', '2 trucks, 1 trip each (~$1,810)',
    'shut in S4_WELL_B1 at +306 min, ~31 min before the trip (~$478)',
    '1 truck, 1 trip + shut in S4_WELL_A2 at +291 min (~$975)', 'none (peaks ~76.7%, ...)'."""
    core = _ref_core(text)
    out = {"trucks": 0, "trips": 0, "wells": set(), "start": None, "cost": 0.0, "accept": False}
    if core.lower().startswith("none"):
        return out
    low = core.lower()
    out["accept"] = "accept" in low or "let it trip" in low
    m = re.search(r"(\d+) trucks?, (\d+) trips?", core)
    if m:
        out["trucks"], out["trips"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"shut in (.+?) at \+(\d+) min", core)
    if m:
        out["wells"] = set(WELL_RE.findall(m.group(1)))
        out["start"] = int(m.group(2))
    m = re.search(r"~\$([\d,]+(?:\.\d+)?)\)", core)
    if m:
        out["cost"] = float(m.group(1).replace(",", ""))
    return out


def compare_plan(ref: dict, plan) -> tuple[bool, str]:
    wells = {w for w, _ in plan.shutins}
    probs = []
    if (ref["trucks"], ref["trips"]) != (plan.trucks, plan.trips_per_truck):
        probs.append(f"trucks {plan.trucks}x{plan.trips_per_truck} vs {ref['trucks']}x{ref['trips']}")
    if ref["wells"] != wells:
        probs.append(f"wells {sorted(wells)} vs {sorted(ref['wells'])}")
    elif ref["start"] is not None and plan.shutins:
        s = max(s for _, s in plan.shutins)
        if abs(s - ref["start"]) > START_TOL_MIN:
            probs.append(f"start +{s} vs +{ref['start']}")
    if ref["accept"] != (plan.kind == "accept_trip"):
        probs.append("accept-trip mismatch")
    if abs(plan.cost - ref["cost"]) > max(COST_TOL[0] * ref["cost"], COST_TOL[1]):
        probs.append(f"cost {plan.cost:,.0f} vs {ref['cost']:,.0f}")
    return not probs, "; ".join(probs)


def score_plans(folder: Path, ds, events, matched) -> dict:
    """Compare our plans with every pump failure's reference_action in this folder."""
    snapshot = folder.name != "full_week"
    plans = plan_ongoing(ds, events) if snapshot else replay_onset_plans(ds, events)
    rows = [(t, f) for t, f in matched if t["category"] == "pump_failure"
            and (not snapshot or t["status"] == "ongoing")]
    if not rows:
        return {"plans_ok": 0, "plans_bad": 0}
    how = "from now, fleet shared" if snapshot else "at each onset, earlier plans hold their trucks"
    print(f"  plans ({how}):")
    good = bad = 0
    for t, f in rows:
        p = plans.get(f.event_id)
        ref = parse_reference(t["reference_action"])
        ok, why = (False, "no plan") if p is None else compare_plan(ref, p)
        good += ok
        bad += not ok
        delta = ""
        if p is not None:
            if ref["start"] is not None and p.shutins:
                delta += f"  start {max(s for _, s in p.shutins) - ref['start']:+d} min"
            delta += f"  cost {p.cost - ref['cost']:+.0f} $"
        print(f"    {'OK  ' if ok else 'DIFF'} {t['id']:9} {t['asset']:11} "
              f"ours: {p.summary() if p else '-':50} ref: {_ref_core(t['reference_action']):50}"
              f"{delta}{'' if ok else '  <- ' + why}")
    return {"plans_ok": good, "plans_bad": bad}


def score_forecast_and_order(ds, events, matched) -> dict:
    """Snapshots: level, hours_to_limit and lost revenue at now, and the alerts-view order."""
    rows = [(t, f) for t, f in matched if t["category"] == "pump_failure" and t["status"] == "ongoing"]
    if not rows:
        return {"order_bad": 0, "h_err": []}
    built = build_alerts(ds, events, include_resolved_plans=False)
    by_id = {r["event_id"]: r for r in built["events"]}
    by_id.update({r["event_id"]: r for r in built["alerts"]})
    h_err, order_bad = [], 0
    print("  forecast & alerts order (ours vs reference):")
    for t, f in sorted(rows, key=lambda p: p[0].get("order", 99)):
        r = by_id[f.event_id]
        h = r["hours_to_limit"]
        h_err.append(abs(h["p50"] - t["hours_to_limit_final"]) * 60)
        ok = r.get("order") == t.get("order")
        order_bad += not ok
        print(f"    {'OK  ' if ok else 'DIFF'} {t['id']:9} order {r.get('order')} vs {t.get('order')}  "
              f"level {r['level_pct']:5.1f} vs {t['level_final_pct']:5.1f}  hours_to_limit "
              f"{h['p50']:.2f} [p10 {h['p10']:.2f}, p90 {h['p90']:.2f}] vs {t['hours_to_limit_final']:.2f}  "
              f"lost $/h {r['lost_revenue_per_hour']:.2f} vs {t['lost_revenue_per_hour']:.2f}")
    cats = [a["category"] for a in built["alerts"]]
    n_fail = cats.count("pump_failure")
    if any(c != "pump_failure" for c in cats[:n_fail]):
        order_bad += 1
        print("    DIFF a leak or worn pump is listed before a pump failure")
    return {"order_bad": order_bad, "h_err": h_err}


# --- per folder ------------------------------------------------------------------------
def score_folder(folder: Path, tol_s: float, verbose: bool) -> dict:
    truth = json.loads((folder / "answer_key.json").read_text())
    t0 = time.time()
    ds = load_dataset(folder)
    events = detect_events(ds)
    secs = time.time() - t0
    matched, missed, false = match(truth, events, tol_s)
    matched.sort(key=lambda p: p[0]["onset_unix"])

    print(f"\n=== {folder.name}  (now {ds.wide.index[-1]}, {len(truth)} true, "
          f"{len(events)} detected, {secs:.1f}s)")
    print(f"  {'id':9} {'asset':15} {'category':17} {'onset_err':>9} {'end_err':>8} "
          f"{'status':>18} {'conf':>5} {'detect_lag':>10}")
    onset_errs, end_errs, status_bad, unobservable = [], [], 0, 0
    for t, f in matched:
        oe = f.onset_unix - t["onset_unix"]
        onset_errs.append(abs(oe))
        ee = None
        if t.get("end_unix") is not None and f.end_unix is not None:
            ee = f.end_unix - t["end_unix"]
            end_errs.append(abs(ee))
        st_ok = t["status"] == f.status
        # Repaired while the pump sat idle and it has not run since: not visible in the data.
        unobs = (not st_ok and f.unverified and t["status"] == "resolved")
        unobservable += unobs
        status_bad += not st_ok and not unobs
        st = f"{t['status']}" + ("" if st_ok else f"!={f.status}") + (" (unobs)" if unobs else "")
        lag = (f.detected_unix - t["onset_unix"]) / 3600 if f.detected_unix else None
        print(f"  {t['id']:9} {t['asset']:15} {t['category']:17} {_fmt_min(oe):>9} {_fmt_min(ee):>8} "
              f"{st:>18} {f.confidence:5.2f} {'' if lag is None else f'{lag:8.1f} h':>10}")
        if verbose:
            for line in f.evidence:
                print(f"      - {line}")
    for t in missed:
        print(f"  MISSED  {t['id']:9} {t['asset']:15} {t['category']:17} onset {t['onset']} "
              f"status {t['status']}")
    for f in false:
        print(f"  FALSE   {f.event_id:9} {f.asset_id:15} {f.category:17} onset {f.onset_ts} "
              f"end {f.end_ts} conf {f.confidence:.2f}")
        if verbose:
            for line in f.evidence:
                print(f"      - {line}")
    pl = score_plans(folder, ds, events, matched)
    fo = score_forecast_and_order(ds, events, matched) if folder.name != "full_week" \
        else {"order_bad": 0, "h_err": []}
    mean_on = sum(onset_errs) / len(onset_errs) / 60 if onset_errs else 0
    max_on = max(onset_errs) / 60 if onset_errs else 0
    mean_end = sum(end_errs) / len(end_errs) / 60 if end_errs else 0
    print(f"  -> matched {len(matched)}/{len(truth)}, missed {len(missed)}, false {len(false)}, "
          f"status mismatches {status_bad} (+{unobservable} unobservable), onset |err| mean "
          f"{mean_on:.1f} min max {max_on:.0f} min, end |err| mean {mean_end:.1f} min, "
          f"plans {pl['plans_ok']}/{pl['plans_ok'] + pl['plans_bad']}")
    return dict(folder=folder.name, truth=len(truth), matched=len(matched), missed=len(missed),
                false=len(false), status_bad=status_bad, unobservable=unobservable,
                mean_onset_min=mean_on, max_onset_min=max_on, **pl, **fo)


def detection_lag_report(first_seen: dict, first_cut: int) -> dict:
    """How long after the TRUE onset each fault type is first flagged.

    flagged  = the sample at which the detector's flag first tripped, in the earliest sweep cut
               that contains the event (computed only from data up to that cut, so causal)
    on screen = that earliest cut's time (an upper bound at the sweep's resolution)
    Events that started before the first cut are left out of "on screen".
    """
    import statistics
    out = {}
    print("  detection lag after true onset (minutes):")
    print(f"    {'category':17} {'n':>2}  {'flagged median':>14} {'flagged worst':>13}  {'on screen median':>16} {'worst':>6}")
    for cat in ("pump_failure", "pipeline_leak", "pump_degradation"):
        rows = [(t, d, c) for t, d, c in first_seen.values() if t["category"] == cat]
        if not rows:
            continue
        flag = [(d - t["onset_unix"]) / 60 for t, d, _ in rows]
        seen = [(c - t["onset_unix"]) / 60 for t, _, c in rows if t["onset_unix"] >= first_cut]
        out[cat] = {"n": len(rows), "flag_median": statistics.median(flag), "flag_worst": max(flag),
                    "seen_median": statistics.median(seen) if seen else None,
                    "seen_worst": max(seen) if seen else None,
                    "per_event": {t["id"]: round((d - t["onset_unix"]) / 60) for t, d, _ in rows}}
        r = out[cat]

        def fmt(x):
            return "" if x is None else f"{x:.0f}"
        print(f"    {cat:17} {r['n']:>2}  {fmt(r['flag_median']):>14} {fmt(r['flag_worst']):>13}  "
              f"{fmt(r['seen_median']):>16} {fmt(r['seen_worst']):>6}")
        print(f"      per event: {r['per_event']}")
    return out


def sweep(folder: Path, step_h: float, tol_s: float, grace_h: float) -> bool:
    """Cut the full dataset at every step_h hours and grade each cut against the full key.

    At a cut, the truth is every event with onset <= now; events that started less than
    grace_h before now may be too small to see yet, so missing them is not counted.
    """
    key = json.loads((folder / "answer_key.json").read_text())
    full = load_dataset(folder)
    step = int(step_h * 3600)
    cuts = range(full.start_unix + 12 * 3600, full.now_unix + 1, step)
    tot = dict(cuts=0, missed=0, false=0, status=0, unobs=0, onset=[])
    first_seen: dict[str, tuple[dict, int, int]] = {}  # event id -> (truth, detected_unix, cut)
    print(f"\n=== sweep {folder.name}: {len(cuts)} cuts every {step_h} h, grace {grace_h} h")
    for now in cuts:
        ds = truncate(full, now)
        events = detect_events(ds)
        truth = []
        for e in key:
            if e["onset_unix"] > ds.now_unix:
                continue
            e = dict(e)
            if e.get("end_unix") is not None and e["end_unix"] > ds.now_unix:
                e["status"], e["end_unix"] = "ongoing", None
            truth.append(e)
        matched, missed, false = match(truth, events, tol_s)
        for t, f in matched:  # earliest cut that sees each event: a causal "first flagged" time
            if t["id"] not in first_seen and f.detected_unix is not None:
                first_seen[t["id"]] = (t, f.detected_unix, ds.now_unix)
        missed = [m for m in missed if ds.now_unix - m["onset_unix"] > grace_h * 3600]
        bad_st = sum(1 for t, f in matched if t["status"] != f.status
                     and not (f.unverified and t["status"] == "resolved"))
        tot["unobs"] += sum(1 for t, f in matched if t["status"] != f.status and f.unverified)
        tot["cuts"] += 1
        tot["missed"] += len(missed)
        tot["false"] += len(false)
        tot["status"] += bad_st
        tot["onset"] += [abs(f.onset_unix - t["onset_unix"]) / 60 for t, f in matched]
        big = [(t, f) for t, f in matched if abs(f.onset_unix - t["onset_unix"]) > 3600]
        if missed or false or bad_st or big:
            print(f"  cut {ds.now_unix} ({time.strftime('%m-%d %H:%M', time.gmtime(ds.now_unix))})"
                  + "".join(f"\n    MISSED {m['id']} {m['asset']} {m['category']} onset {m['onset']}" for m in missed)
                  + "".join(f"\n    FALSE {f.asset_id} {f.category} onset {f.onset_ts} end {f.end_ts}" for f in false)
                  + "".join(f"\n    STATUS {t['id']} truth {t['status']} got {f.status}"
                            for t, f in matched if t["status"] != f.status and not f.unverified)
                  + "".join(f"\n    ONSET {t['id']} {t['asset']} err {(f.onset_unix - t['onset_unix']) / 60:+.0f} min"
                            for t, f in big))
    detection_lag_report(first_seen, cuts[0])
    on = tot["onset"]
    print(f"  -> {tot['cuts']} cuts: missed {tot['missed']}, false {tot['false']}, status mismatches "
          f"{tot['status']} (+{tot['unobs']} unobservable), onset |err| mean "
          f"{sum(on) / max(len(on), 1):.1f} min, p95 {sorted(on)[int(0.95 * (len(on) - 1))] if on else 0:.0f} min,"
          f" max {max(on) if on else 0:.0f} min")
    return tot["missed"] == 0 and tot["false"] == 0 and tot["status"] == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=str(ROOT / "HackUTD-ZeroDay" / "history"))
    ap.add_argument("--tol-hours", type=float, default=6.0, help="max onset error to count as a match")
    ap.add_argument("-v", "--verbose", action="store_true", help="print evidence lines")
    ap.add_argument("--sweep", type=float, metavar="HOURS",
                    help="also cut full_week every HOURS and grade each cut")
    ap.add_argument("--grace-hours", type=float, default=4.0,
                    help="sweep: don't count events younger than this as missed")
    a = ap.parse_args()
    root = Path(a.root)
    folders = sorted(p.parent for p in root.rglob("answer_key.json"))
    if not folders:
        print(f"no answer_key.json under {root}")
        return 2
    results = [score_folder(f, a.tol_hours * 3600, a.verbose) for f in folders]
    print("\n=== summary")
    ok = True
    for r in results:
        good = (r["missed"] == 0 and r["false"] == 0 and r["status_bad"] == 0
                and r["plans_bad"] == 0 and r["order_bad"] == 0)
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {r['folder']:26} {r['matched']:2}/{r['truth']:<2} "
              f"missed {r['missed']} false {r['false']} status {r['status_bad']}"
              f"{'+' + str(r['unobservable']) + ' unobs' if r['unobservable'] else ''} "
              f"onset mean {r['mean_onset_min']:.1f} max {r['max_onset_min']:.0f} min | "
              f"plans {r['plans_ok']}/{r['plans_ok'] + r['plans_bad']}"
              + (f" | order diffs {r['order_bad']}, hours_to_limit |err| max {max(r['h_err']):.1f} min"
                 if r["h_err"] else ""))
    if a.sweep:
        for f in (f for f in folders if f.name == "full_week"):
            ok &= sweep(f, a.sweep, a.tol_hours * 3600, a.grace_hours)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
