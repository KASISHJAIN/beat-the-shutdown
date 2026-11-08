"""Live hardware twin: an ESP32 streams {level_pct, pump_status, vibration, ts} for one extra
facility (TWIN_FACILITY in system TWIN). The samples are turned into the same telemetry schema
as the datasets, so detection, countdown and planning run on it unchanged.

The ESP32 has no flow meter. The tank's inflow is estimated from how fast the level rises
while the pump is not running and spread over the wells by their baseline share, so the
countdown matches the physical tank's real fill rate.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .loader import Dataset, _parse_network

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "twin_config.json"
DATASET_ID = "twin/live"
INFLOW_WINDOW_S = 30      # level-slope window for the inflow estimate
MIN_SLOPE_SAMPLES = 4


@dataclass
class Sample:
    ts: float
    level_pct: float
    pump_status: int
    vibration: float


class TwinStore:
    def __init__(self, config_path: Path = CONFIG_PATH):
        self.config_path = config_path
        self.cfg = json.loads(config_path.read_text())
        self.samples: list[Sample] = []
        self.lock = threading.Lock()
        self.version = 0          # bumps on every ingest; used as a cache key
        self.last_received = 0.0  # wall clock, only for the online badge

    # --- config -> network.json / ops.json shapes ------------------------------------
    @property
    def facility_id(self) -> str:
        return self.cfg["facility"]["id"]

    def network_raw(self) -> dict:
        s, f = self.cfg["system"], self.cfg["facility"]
        return {
            "defaults": {"tank_limits_pct": self.cfg.get("tank_limits_pct", {"warning": 85, "shutdown": 92})},
            "systems": [{
                "id": s["id"], "center": s["center"], "truck_yard": s["truck_yard"],
                "disposal_site": s["disposal_site"],
                "facilities": [{k: f[k] for k in ("id", "lat", "lon", "wells", "tank", "pump", "pipeline")}],
            }],
        }

    def ops(self) -> dict:
        o, s, f = self.cfg["ops"], self.cfg["system"], self.cfg["facility"]
        return {
            "dataset_id": "twin",
            "prices": o["prices"],
            "expected_repair_hours": o["expected_repair_hours"],
            "emergency_trucking": o["emergency_trucking"],
            "systems": {s["id"]: {
                "emergency_fleet_size": o["emergency_fleet_size"],
                "facilities": {f["id"]: {"response_min": o["response_min"], "cycle_min": o["cycle_min"]}},
            }},
        }

    def setpoints(self) -> dict:
        p = self.cfg["facility"]["pump"]
        lim = self.cfg.get("tank_limits_pct", {})
        return {"start_level_pct": p["start_level_pct"], "stop_level_pct": p["stop_level_pct"],
                "warning_pct": lim.get("warning", 85), "shutdown_pct": lim.get("shutdown", 92)}

    # --- ingest ----------------------------------------------------------------------
    def ingest(self, level_pct: float, pump_status: int, vibration: float, ts: float | None) -> Sample:
        now = time.time()
        # Device clocks: epoch seconds, epoch ms, or none (millis since boot) -> use receive time.
        if ts is not None and ts > 1e12:
            ts = ts / 1000.0
        if ts is None or ts < 1e9:
            ts = now
        s = Sample(float(ts), float(np.clip(level_pct, 0, 100)), int(pump_status), float(max(vibration, 0.0)))
        with self.lock:
            if self.samples and s.ts <= self.samples[-1].ts:
                s.ts = self.samples[-1].ts + 0.001  # keep time strictly increasing
            self.samples.append(s)
            cap = int(self.cfg.get("max_samples", 10800))
            if len(self.samples) > cap:
                del self.samples[: len(self.samples) - cap]
            self.version += 1
            self.last_received = now
        return s

    def reset(self) -> None:
        with self.lock:
            self.samples.clear()
            self.version += 1

    def online(self) -> bool:
        return bool(self.samples) and time.time() - self.last_received < self.cfg.get("online_timeout_s", 10)

    # --- dataset ---------------------------------------------------------------------
    def _wide(self, samples: list[Sample]) -> pd.DataFrame:
        f = self.cfg["facility"]
        tank, pump, fac = f["tank"]["id"], f["pump"]["id"], f["id"]
        t = np.array([round(s.ts) for s in samples], dtype=np.int64)
        lvl = np.array([s.level_pct for s in samples])
        st = np.array([s.pump_status for s in samples], dtype=float)
        vib = np.array([s.vibration for s in samples])
        inflow = self._inflow_estimate(t.astype(float), lvl, st)
        df = pd.DataFrame({
            "ts_unix": t,
            f"{tank}.level_pct": lvl,
            f"{pump}.status": st,
            f"{pump}.vibration": vib,
            f"{fac}.inflow_bpd": inflow,
        })
        base = np.array([w["baseline_water_bpd"] for w in f["wells"]], dtype=float)
        share = base / base.sum() if base.sum() > 0 else np.full(len(base), 1 / max(len(base), 1))
        for w, sh in zip(f["wells"], share):
            df[f"{w['id']}.water_bpd"] = inflow * sh
            df[f"{w['id']}.status"] = 1.0
        # several posts can land in one second: keep the last
        return df.groupby("ts_unix").last().sort_index()

    def _inflow_estimate(self, t: np.ndarray, lvl: np.ndarray, st: np.ndarray) -> np.ndarray:
        """Inflow (bbl/day) from the level slope over recent samples with the pump not running.
        While the pump runs the outflow is unknown, so the last estimate is held."""
        cap = float(self.cfg["facility"]["tank"]["capacity_bbl"])
        default = float(sum(w["baseline_water_bpd"] for w in self.cfg["facility"]["wells"]))
        out = np.empty(len(t))
        est = default
        for i in range(len(t)):
            if st[i] != 1:
                # only the current non-running stretch (no pump run in between)
                j = i
                while j > 0 and st[j - 1] != 1 and t[j - 1] >= t[i] - INFLOW_WINDOW_S:
                    j -= 1
                idx = np.arange(j, i + 1)
                if len(idx) >= MIN_SLOPE_SAMPLES and t[idx[-1]] > t[idx[0]]:
                    slope = np.polyfit(t[idx] - t[idx[0]], lvl[idx], 1)[0]  # % per second
                    if slope > 0:
                        est = slope / 100.0 * cap * 86400.0
            out[i] = est
        return out

    def dataset(self) -> Dataset | None:
        with self.lock:
            samples = list(self.samples)
            version = self.version
        if len(samples) < 2:
            return None
        cached = getattr(self, "_cached", None)
        if cached is not None and cached[0] == version:
            return cached[1]
        ds = self._build(samples)
        self._cached = (version, ds)
        return ds

    def _build(self, samples: list[Sample]) -> Dataset:
        net = self.network_raw()
        systems, facilities = _parse_network(net)
        wide = self._wide(samples)
        step = int(pd.Series(wide.index).diff().mode().iloc[0]) if len(wide) > 1 else 2
        lim = net["defaults"]["tank_limits_pct"]
        a2f = {}
        for f in facilities.values():
            for a in [f.id, f.tank_id, f.pump_id, f.pipeline_id, *[w.id for w in f.wells]]:
                a2f[a] = f.id
        return Dataset(path=Path(DATASET_ID), network_raw=net, ops=self.ops(), systems=systems,
                       facilities=facilities, warning_pct=float(lim["warning"]),
                       shutdown_pct=float(lim["shutdown"]), wide=wide,
                       start_unix=int(wide.index[0]), now_unix=int(wide.index[-1]),
                       step_s=max(step, 1), asset_to_facility=a2f)


_store: TwinStore | None = None


def store() -> TwinStore | None:
    """The process-wide twin, or None when twin_config.json is absent."""
    global _store
    if _store is None and CONFIG_PATH.exists():
        _store = TwinStore()
    return _store
