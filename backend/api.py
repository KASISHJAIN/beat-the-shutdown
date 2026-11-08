"""Operator console API.

Every endpoint takes `dataset` (a folder id from /api/datasets, e.g. "history/full_week")
and an optional `now` (ts_unix) for time travel: the dataset is cut at `now` with
loader.truncate, so detection, forecasts and plans see only data up to that moment.

Run:  uvicorn backend.api:app --reload
Data: folders under $BTS_DATA_ROOT (default ./HackUTD-ZeroDay) that hold network.json,
      ops.json and telemetry.csv. answer_key.json is never read.
"""
from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .alerts import build
from .detect import detect_events
from .loader import Dataset, load_dataset, truncate
from .optimize import plan_ongoing, replay_onset_plans, well_revenue_per_hour, whatif

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("BTS_DATA_ROOT", ROOT / "HackUTD-ZeroDay")).resolve()
REQUIRED = ("network.json", "ops.json", "telemetry.csv")
MIN_HISTORY_S = 2 * 3600  # earliest "now" allowed: two hours into the data

app = FastAPI(title="Beat the Shutdown", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


# --- datasets & time travel -------------------------------------------------------------
def _discover() -> dict[str, Path]:
    found = {}
    if not DATA_ROOT.exists():
        return found
    for p in sorted(DATA_ROOT.rglob("telemetry.csv")):
        d = p.parent
        if ".git" in d.parts or not all((d / f).exists() for f in REQUIRED):
            continue
        found[d.relative_to(DATA_ROOT).as_posix()] = d
    return found


def _kind(dataset_id: str) -> str:
    name = dataset_id.split("/")[-1]
    for k in ("full_week", "snapshot", "mock_test", "test"):
        if name.startswith(k):
            return k
    return "other"


def _full(dataset_id: str) -> Dataset:
    folders = _discover()
    if dataset_id not in folders:
        raise HTTPException(404, f"unknown dataset {dataset_id!r}; see /api/datasets")
    return load_dataset(folders[dataset_id])


def _snap_now(full: Dataset, now: int | None) -> int:
    """Clamp a requested time onto the dataset's 5-minute grid inside its range."""
    if now is None:
        return full.now_unix
    lo = min(full.start_unix + MIN_HISTORY_S, full.now_unix)
    now = int(min(max(now, lo), full.now_unix))
    return full.start_unix + (now - full.start_unix) // full.step_s * full.step_s


@lru_cache(maxsize=64)
def _cut(dataset_id: str, now: int) -> tuple[Dataset, list]:
    ds = _full(dataset_id)
    cut = ds if now == ds.now_unix else truncate(ds, now)
    return cut, detect_events(cut)


@lru_cache(maxsize=64)
def _alerts(dataset_id: str, now: int) -> dict:
    ds, events = _cut(dataset_id, now)
    return build(ds, events, include_resolved_plans=False)


@lru_cache(maxsize=16)
def _events(dataset_id: str, now: int) -> dict:
    ds, events = _cut(dataset_id, now)
    return build(ds, events, include_resolved_plans=True)


def _ctx(dataset: str, now: int | None) -> tuple[str, int]:
    full = _full(dataset)
    return dataset, _snap_now(full, now)


def _clean(x):
    """JSON-safe: NaN/inf -> None, numpy scalars -> Python."""
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (np.floating, float)):
        f = float(x)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(x, np.integer):
        return int(x)
    return x


def _time(ds_full: Dataset, now: int) -> dict:
    return {"start_unix": ds_full.start_unix, "end_unix": ds_full.now_unix, "now_unix": now,
            "step_s": ds_full.step_s}


# --- endpoints ------------------------------------------------------------------------
@app.get("/api/datasets")
def datasets():
    out = []
    for i, (k, p) in enumerate(_discover().items()):
        out.append({"id": k, "label": k.split("/")[-1], "kind": _kind(k)})
    order = {"test": 0, "full_week": 1, "mock_test": 2, "snapshot": 3, "other": 4}
    out.sort(key=lambda d: (order[d["kind"]], d["id"]))
    return {"data_root": str(DATA_ROOT), "datasets": out}


@app.get("/api/network")
def network(dataset: str, now: int | None = None):
    dataset, now = _ctx(dataset, now)
    ds, _ = _cut(dataset, now)
    alerts = _alerts(dataset, now)["alerts"]
    by_fac: dict[str, list[dict]] = {}
    for a in alerts:
        by_fac.setdefault(a["facility_id"], []).append(a)
    systems = []
    for s in ds.systems:
        facs = []
        for f in s.facilities:
            al = by_fac.get(f.id, [])
            cats = {a["category"] for a in al}
            if "pump_failure" in cats:
                h = min(((a.get("hours_to_limit") or {}).get("p50") or 0) for a in al
                        if a["category"] == "pump_failure")
                status, severity = "pump_failure", ("critical" if h < 2 else "high")
            elif "pipeline_leak" in cats:
                status, severity = "pipeline_leak", "urgent"
            elif "pump_degradation" in cats:
                status, severity = "pump_degradation", "warning"
            else:
                status, severity = "ok", "ok"
            rev = well_revenue_per_hour(ds, f)
            last = lambda a, m: (ds.series(a, m).dropna().iloc[-1] if ds.has(a, m)
                                 and ds.series(a, m).notna().any() else None)
            facs.append({
                "id": f.id, "lat": f.lat, "lon": f.lon, "capacity_bbl": f.capacity_bbl,
                "tank_id": f.tank_id, "pump_id": f.pump_id, "pipeline_id": f.pipeline_id,
                "max_flow_bpd": f.max_flow_bpd, "start_level_pct": f.start_level_pct,
                "stop_level_pct": f.stop_level_pct,
                "level_pct": last(f.tank_id, "level_pct"), "pump_status": last(f.pump_id, "status"),
                "status": status, "severity": severity,
                "alert_ids": [a["event_id"] for a in al],
                "wells": [{"id": w.id, "baseline_water_bpd": w.baseline_water_bpd, "oil_bpd": w.oil_bpd,
                           "gas_mcfd": w.gas_mcfd, "revenue_per_hour": round(rev[w.id], 2),
                           "status": last(w.id, "status")} for w in f.wells],
            })
        so = ds.ops["systems"].get(s.id, {})
        systems.append({
            "id": s.id, "center": s.center, "truck_yard": s.truck_yard,
            "fleet_size": so.get("emergency_fleet_size"),
            "disposal": {"id": s.disposal_id, "lat": s.disposal_lat, "lon": s.disposal_lon,
                         "pressure_psi": s.disposal_psi},
            "facilities": facs,
        })
    return _clean({"dataset": dataset, **_time(_full(dataset), now),
                   "warning_pct": ds.warning_pct, "shutdown_pct": ds.shutdown_pct,
                   "systems": systems})


@app.get("/api/alerts")
def alerts(dataset: str, now: int | None = None):
    dataset, now = _ctx(dataset, now)
    a = _alerts(dataset, now)
    return _clean({"dataset": dataset, **_time(_full(dataset), now), "alerts": a["alerts"]})


@app.get("/api/events")
def events(dataset: str, now: int | None = None, facility_id: str | None = None):
    dataset, now = _ctx(dataset, now)
    ev = _events(dataset, now)["events"]
    if facility_id:
        ev = [e for e in ev if e["facility_id"] == facility_id]
    return _clean({"dataset": dataset, **_time(_full(dataset), now), "events": ev})


@app.get("/api/series")
def series(dataset: str, asset: str, metric: str = Query(..., description="one or more, comma separated"),
           now: int | None = None, hours: float = 48, max_points: int = 1500):
    dataset, now = _ctx(dataset, now)
    ds, _ = _cut(dataset, now)
    lo = now - int(hours * 3600) if hours > 0 else ds.start_unix
    out = {}
    for m in [m.strip() for m in metric.split(",") if m.strip()]:
        if not ds.has(asset, m):
            raise HTTPException(404, f"no series {asset}.{m}")
        s = ds.series(asset, m).loc[lo:now]
        stride = max(1, math.ceil(len(s) / max_points))
        s = s.iloc[::stride]
        out[m] = {"t": [int(t) for t in s.index], "v": _clean(s.to_numpy().tolist())}
    return {"dataset": dataset, "asset": asset, "now_unix": now, "series": out}


@app.get("/api/plan")
def plan(dataset: str, event_id: str, now: int | None = None):
    dataset, now = _ctx(dataset, now)
    ds, evs = _cut(dataset, now)
    ev = next((e for e in evs if e.event_id == event_id), None)
    if ev is None:
        raise HTTPException(404, f"no event {event_id} at now={now}")
    if ev.category != "pump_failure":
        return _clean({"event_id": event_id, "plan": None, "whatif": None,
                       "recommended_actions": [{"type": "field_inspection", "asset_id": ev.asset_id}]})
    if ev.status == "ongoing":
        p = plan_ongoing(ds, evs).get(event_id)
        base = ds
    else:  # resolved: the plan the operator should have used at onset
        p = replay_onset_plans(ds, evs).get(event_id)
        base = truncate(ds, ev.onset_unix)
    if p is None:
        raise HTTPException(409, "repair already finished; nothing to plan")
    return _clean({"event_id": event_id, "status": ev.status, "plan": p.to_dict(),
                   "whatif": whatif(base, ev, p)})


@app.get("/api/health")
def health():
    return {"ok": True, "data_root": str(DATA_ROOT), "datasets": len(_discover())}


# Serve the built frontend (frontend/dist) from the same port when it exists.
DIST = ROOT / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        f = DIST / path
        if path and f.is_file():
            return FileResponse(f)
        # index.html must not be cached: it points at the current hashed bundles
        return FileResponse(DIST / "index.html", headers={"Cache-Control": "no-cache"})
