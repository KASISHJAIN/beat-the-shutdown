"""Grade detection against every answer_key.json under a root folder.

Usage: python scripts/score.py [ROOT] [--tol-hours H] [-v]

The only file in this project allowed to read answer_key.json.
Matching: same asset and category, onset within the tolerance, one-to-one (closest first).
Exit code 0 when every folder has no missed and no false events.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.detect import detect_events  # noqa: E402
from backend.loader import load_dataset, truncate  # noqa: E402


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
    mean_on = sum(onset_errs) / len(onset_errs) / 60 if onset_errs else 0
    max_on = max(onset_errs) / 60 if onset_errs else 0
    mean_end = sum(end_errs) / len(end_errs) / 60 if end_errs else 0
    print(f"  -> matched {len(matched)}/{len(truth)}, missed {len(missed)}, false {len(false)}, "
          f"status mismatches {status_bad} (+{unobservable} unobservable), onset |err| mean {mean_on:.1f} min max {max_on:.0f} min, "
          f"end |err| mean {mean_end:.1f} min")
    return dict(folder=folder.name, truth=len(truth), matched=len(matched), missed=len(missed),
                false=len(false), status_bad=status_bad, unobservable=unobservable, mean_onset_min=mean_on, max_onset_min=max_on)


def sweep(folder: Path, step_h: float, tol_s: float, grace_h: float) -> bool:
    """Cut the full dataset at every step_h hours and grade each cut against the full key.

    At a cut, the truth is every event with onset <= now; events that started less than
    grace_h before now may be too small to see yet, so missing them is not counted.
    """
    key = json.loads((folder / "answer_key.json").read_text())
    full = load_dataset(folder)
    step = int(step_h * 3600)
    cuts = range(full.start_unix + 12 * 3600, full.now_unix + 1, step)
    tot = dict(cuts=0, missed=0, false=0, status=0, unobs=0, onset=[], lag=[])
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
        good = r["missed"] == 0 and r["false"] == 0 and r["status_bad"] == 0
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {r['folder']:28} {r['matched']:2}/{r['truth']:<2} "
              f"missed {r['missed']} false {r['false']} status {r['status_bad']}"
              f"{'+' + str(r['unobservable']) + ' unobs' if r['unobservable'] else ''} "
              f"onset mean {r['mean_onset_min']:.1f} min max {r['max_onset_min']:.0f} min")
    if a.sweep:
        full = [f for f in folders if f.name == "full_week"]
        for f in full:
            ok &= sweep(f, a.sweep, a.tol_hours * 3600, a.grace_hours)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
