"""Load a dataset folder (network.json, ops.json, telemetry.csv) into typed objects.

Never reads answer_key.json. "Now" is the last ts_unix in telemetry.csv.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pandas as pd


@dataclass
class Well:
    id: str
    baseline_water_bpd: float
    oil_bpd: float
    gas_mcfd: float


@dataclass
class Facility:
    id: str
    system_id: str
    lat: float
    lon: float
    wells: list[Well]
    tank_id: str
    capacity_bbl: float
    pump_id: str
    max_flow_bpd: float
    start_level_pct: float
    stop_level_pct: float
    pipeline_id: str
    pipeline_max_bpd: float | None
    pipeline_max_pressure_psi: float | None
    disposal_psi: float


@dataclass
class System:
    id: str
    center: list[float]
    truck_yard: list[float]
    disposal_id: str
    disposal_lat: float
    disposal_lon: float
    disposal_psi: float
    facilities: list[Facility]


@dataclass
class Dataset:
    path: Path
    network_raw: dict
    ops: dict
    systems: list[System]
    facilities: dict[str, Facility]
    warning_pct: float
    shutdown_pct: float
    # Wide telemetry: index = ts_unix (int), columns = "ASSET.metric"
    wide: pd.DataFrame
    start_unix: int
    now_unix: int
    step_s: int = 300
    asset_to_facility: dict[str, str] = field(default_factory=dict)

    def series(self, asset_id: str, metric: str) -> pd.Series:
        return self.wide[f"{asset_id}.{metric}"]

    def has(self, asset_id: str, metric: str) -> bool:
        return f"{asset_id}.{metric}" in self.wide.columns


def _parse_network(net: dict) -> tuple[list[System], dict[str, Facility]]:
    systems: list[System] = []
    facilities: dict[str, Facility] = {}
    for s in net["systems"]:
        d = s["disposal_site"]
        facs = []
        for f in s["facilities"]:
            pump, tank, pipe = f["pump"], f["tank"], f["pipeline"]
            fac = Facility(
                id=f["id"],
                system_id=s["id"],
                lat=f["lat"],
                lon=f["lon"],
                wells=[Well(w["id"], float(w["baseline_water_bpd"]), float(w["oil_bpd"]), float(w["gas_mcfd"]))
                       for w in f["wells"]],
                tank_id=tank["id"],
                capacity_bbl=float(tank["capacity_bbl"]),
                pump_id=pump["id"],
                max_flow_bpd=float(pump["max_flow_bpd"]),
                start_level_pct=float(pump["start_level_pct"]),
                stop_level_pct=float(pump["stop_level_pct"]),
                pipeline_id=pipe["id"],
                pipeline_max_bpd=pipe.get("max_bpd"),
                pipeline_max_pressure_psi=pipe.get("max_pressure_psi"),
                disposal_psi=float(d["pressure_psi"]),
            )
            facs.append(fac)
            facilities[fac.id] = fac
        systems.append(System(
            id=s["id"], center=s.get("center"), truck_yard=s.get("truck_yard"),
            disposal_id=d["id"], disposal_lat=d["lat"], disposal_lon=d["lon"],
            disposal_psi=float(d["pressure_psi"]), facilities=facs,
        ))
    return systems, facilities


def _read_telemetry(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, usecols=["ts_unix", "asset_id", "metric", "value"],
                     dtype={"ts_unix": "int64", "asset_id": "string", "metric": "string", "value": "float64"})
    df["key"] = df["asset_id"] + "." + df["metric"]
    wide = df.pivot_table(index="ts_unix", columns="key", values="value", aggfunc="last")
    wide.columns = [str(c) for c in wide.columns]
    wide = wide.sort_index()
    # Regularise onto the 5-minute grid so gaps (if any) show as NaN, not missing rows.
    if len(wide) > 1:
        step = int(pd.Series(wide.index).diff().mode().iloc[0])
        full = range(int(wide.index[0]), int(wide.index[-1]) + step, step)
        wide = wide.reindex(full)
    wide.index.name = "ts_unix"
    return wide


@lru_cache(maxsize=8)
def _load_cached(folder: str, mtime: float) -> Dataset:
    p = Path(folder)
    net = json.loads((p / "network.json").read_text())
    ops = json.loads((p / "ops.json").read_text())
    systems, facilities = _parse_network(net)
    wide = _read_telemetry(p / "telemetry.csv")
    step = int(pd.Series(wide.index).diff().mode().iloc[0]) if len(wide) > 1 else 300
    limits = net.get("defaults", {}).get("tank_limits_pct", {})
    a2f = {}
    for f in facilities.values():
        for a in [f.id, f.tank_id, f.pump_id, f.pipeline_id, *[w.id for w in f.wells]]:
            a2f[a] = f.id
    return Dataset(
        path=p, network_raw=net, ops=ops, systems=systems, facilities=facilities,
        warning_pct=float(limits.get("warning", 85)), shutdown_pct=float(limits.get("shutdown", 92)),
        wide=wide, start_unix=int(wide.index[0]), now_unix=int(wide.index[-1]), step_s=step,
        asset_to_facility=a2f,
    )


def truncate(ds: Dataset, now_unix: int) -> Dataset:
    """Same dataset stopped at an earlier "now" (for replay and snapshot-style testing)."""
    from dataclasses import replace
    wide = ds.wide.loc[:now_unix]
    return replace(ds, wide=wide, now_unix=int(wide.index[-1]))


def load_dataset(folder: str | Path) -> Dataset:
    p = Path(folder).resolve()
    mtime = (p / "telemetry.csv").stat().st_mtime
    return _load_cached(str(p), mtime)
