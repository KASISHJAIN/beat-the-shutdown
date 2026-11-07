"""Tank state at "now" and hours_to_limit for ongoing pump failures.

Everything comes from the telemetry up to "now" plus network.json ratings:
  level   : line fit over the current regime (pump/well states unchanged) evaluated at now,
            so one noisy reading doesn't move the countdown
  inflow  : mean of the facility's measured inflow_bpd over the recent window
  rate    : inflow / 24 / capacity * 100  (% per hour, pump is down so no outflow)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .loader import Dataset, Facility

RECENT = 12  # samples (1 h at 5-minute data) used for level / inflow estimates
Z90 = 1.2816  # one-sided z for p10 / p90


@dataclass
class TankState:
    facility_id: str
    now_unix: int
    level_pct: float          # best estimate at now
    level_sd: float           # standard error of that estimate (% points)
    inflow_bpd: float         # current inflow into the tank
    inflow_sd: float          # sample-level spread of inflow (short-term variability)
    capacity_bbl: float
    shutdown_pct: float
    well_bpd: dict[str, float] = field(default_factory=dict)   # producing wells only
    shut_wells: list[str] = field(default_factory=list)          # wells already shut in

    @property
    def volume_bbl(self) -> float:
        return self.level_pct / 100 * self.capacity_bbl

    @property
    def limit_bbl(self) -> float:
        return self.shutdown_pct / 100 * self.capacity_bbl

    @property
    def tripped(self) -> bool:
        return self.level_pct >= self.shutdown_pct - 0.05 or (self.inflow_bpd <= 1 and bool(self.shut_wells))


def _regime_start(ds: Dataset, fac: Facility) -> int:
    """Index of the first sample of the current regime (pump status and well statuses unchanged)."""
    cols = [f"{fac.pump_id}.status"] + [f"{w.id}.status" for w in fac.wells if ds.has(w.id, "status")]
    s = ds.wide[cols].ffill().to_numpy()
    n = len(s)
    i = n - 1
    while i > 0 and np.array_equal(s[i - 1], s[i]):
        i -= 1
    return i


def _fit_at(t: np.ndarray, y: np.ndarray, t_eval: float) -> tuple[float, float, float]:
    """Line fit -> (value at t_eval, standard error of that value, residual sd)."""
    ok = np.isfinite(y)
    t, y = t[ok], y[ok]
    if len(y) == 0:
        return float("nan"), float("nan"), float("nan")
    if len(y) < 3:
        return float(y[-1]), 0.1, 0.1
    x = (t - t_eval) / 3600.0
    A = np.vstack([np.ones_like(x), x]).T
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = y - A @ coef
    sd = float(np.sqrt(np.sum(resid ** 2) / max(len(y) - 2, 1)))
    cov = sd ** 2 * np.linalg.pinv(A.T @ A)
    return float(coef[0]), float(np.sqrt(max(cov[0, 0], 0))), sd


def _well_rates(ds: Dataset, fac: Facility, n: int) -> tuple[dict[str, float], list[str]]:
    """Producing wells' water rates. The network rating is the noise-free rate, so use it
    whenever the measured mean agrees with it within noise; otherwise trust the measurement."""
    rates, shut = {}, []
    for w in fac.wells:
        st = ds.series(w.id, "status").ffill().to_numpy()[-1] if ds.has(w.id, "status") else 1
        if st == 0:
            shut.append(w.id)
            continue
        q = ds.series(w.id, "water_bpd").to_numpy()[max(n - RECENT, 0):] if ds.has(w.id, "water_bpd") else np.array([])
        q = q[np.isfinite(q)]
        if len(q) < 3:
            rates[w.id] = w.baseline_water_bpd
            continue
        se = q.std(ddof=1) / np.sqrt(len(q))
        rates[w.id] = w.baseline_water_bpd if abs(q.mean() - w.baseline_water_bpd) <= 3 * se + 1e-9             else float(q.mean())
    return rates, shut


def tank_state(ds: Dataset, fac: Facility) -> TankState:
    t = ds.wide.index.to_numpy().astype(float)
    lvl = ds.series(fac.tank_id, "level_pct").to_numpy()
    n = len(lvl)
    r0 = _regime_start(ds, fac)
    well_bpd, shut = _well_rates(ds, fac, n)

    meter = ds.series(fac.id, "inflow_bpd").to_numpy()
    recent = meter[max(n - RECENT, 0):]
    recent = recent[np.isfinite(recent)]
    inflow_bpd = sum(well_bpd.values())
    inflow_sd = float(recent.std(ddof=1)) if len(recent) > 2 else 0.0
    # Only readings since the last pump/well state change describe the current inflow.
    cur = meter[max(r0, n - RECENT):]
    cur = cur[np.isfinite(cur)]
    if len(cur) >= 3:
        sd = cur.std(ddof=1)
        if abs(cur.mean() - inflow_bpd) > 3 * sd / np.sqrt(len(cur)) + 1e-9:
            inflow_bpd = float(cur.mean())  # ratings disagree with the meter: trust the meter

    pump_down = ds.series(fac.pump_id, "status").ffill().to_numpy()[-1] != 1
    if pump_down and inflow_bpd <= 1:
        # Nothing flowing in or out (e.g. automatic shut-in): the level just holds.
        cur = lvl[r0:]
        cur = cur[np.isfinite(cur)]
        level = float(np.median(cur)) if len(cur) else float(lvl[np.isfinite(lvl)][-1])
        level_se = 0.05
    elif pump_down and n - r0 >= 2:
        # Known fill rate (no outflow): each reading since the change projects to now.
        rate_h = inflow_bpd / 24.0 / fac.capacity_bbl * 100
        lo = max(r0, n - RECENT)
        proj = lvl[lo:] + rate_h * (t[-1] - t[lo:]) / 3600.0
        proj = proj[np.isfinite(proj)]
        level = float(proj.mean())
        level_se = float(proj.std(ddof=1) / np.sqrt(len(proj))) if len(proj) > 2 else 0.1
    elif n - r0 >= 3:
        lo = max(r0, n - RECENT)
        level, level_se, _ = _fit_at(t[lo:], lvl[lo:], t[-1])
    else:
        # Regime just changed (e.g. the pump failed this sample): extrapolate the previous
        # regime's trend to now and average with the new readings.
        p0 = max(_regime_start_before(ds, fac, r0), r0 - RECENT)
        prev, prev_se, _ = _fit_at(t[p0:r0], lvl[p0:r0], t[-1]) if r0 - p0 >= 3 else (np.nan, np.nan, np.nan)
        cur = lvl[r0:]
        cur = cur[np.isfinite(cur)]
        if np.isfinite(prev) and len(cur):
            level = float((prev * 3 + cur.mean()) / 4)  # previous trend is the steadier estimate
            level_se = float(prev_se)
        elif np.isfinite(prev):
            level, level_se = prev, prev_se
        else:
            level, level_se = float(cur[-1]), 0.1
    return TankState(fac.id, ds.now_unix, level, level_se, inflow_bpd, inflow_sd,
                     fac.capacity_bbl, ds.shutdown_pct, well_bpd, shut)


def _regime_start_before(ds: Dataset, fac: Facility, r0: int) -> int:
    if r0 <= 0:
        return 0
    cols = [f"{fac.pump_id}.status"] + [f"{w.id}.status" for w in fac.wells if ds.has(w.id, "status")]
    s = ds.wide[cols].ffill().to_numpy()
    i = r0 - 1
    while i > 0 and np.array_equal(s[i - 1], s[i]):
        i -= 1
    return i


def hours_to_limit(st: TankState) -> dict:
    """{'p10','p50','p90'} hours until the tank reaches shutdown with no action.

    p10/p90 propagate the level estimate's standard error and the inflow's short-term
    variability; p10 is the pessimistic (sooner) end.
    """
    if st.tripped:
        return {"p10": 0.0, "p50": 0.0, "p90": 0.0}
    head = (st.shutdown_pct - st.level_pct) / 100 * st.capacity_bbl
    rate = st.inflow_bpd / 24.0
    if rate <= 0:
        return {"p10": None, "p50": None, "p90": None}
    p50 = head / rate
    # relative uncertainty of head and rate, combined
    rel_head = (st.level_sd / 100 * st.capacity_bbl) / head if head > 0 else 0
    rel_rate = st.inflow_sd / st.inflow_bpd if st.inflow_bpd > 0 else 0
    rel = float(np.hypot(rel_head, rel_rate))
    return {"p10": round(max(p50 * (1 - Z90 * rel), 0), 2), "p50": round(p50, 2),
            "p90": round(p50 * (1 + Z90 * rel), 2)}
