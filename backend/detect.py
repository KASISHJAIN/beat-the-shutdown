"""Detect pump failures, worn pumps and pipeline leaks from a loaded Dataset.

All thresholds are expressed in units of each asset's own noise (sigma) around its own
healthy baseline, both measured from the telemetry, so nothing is tied to one network.

Signals (pump running samples only, status == 1):
  pump_degradation : flow_bpd / max_flow_bpd and discharge_psi fall linearly below baseline
  pipeline_leak    : pipeline pressure_psi falls below its running baseline and
                     flow_bpd - receive_flow_bpd rises above ~0
  pump_failure     : pump status == 2
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .loader import Dataset, Facility

# --- tuning (dimensionless, in sigmas / running samples) -------------------------------
WINDOW = 12            # running samples in the rolling mean used to flag a fault (~1 h of running)
FLAG_Z = 1.5           # rolling mean of the combined z-score that flags a sample (~7 sigma of the mean)
CLEAR_Z = 0.6          # rolling mean below this (over CLEAR_WINDOW) closes a segment
CLEAR_WINDOW = 6
MIN_FLAGGED = 6        # a segment needs at least this many flagged running samples
SAMPLE_Z = 3.0         # a single sample this far out is unambiguously faulty
# Worn pumps: CUSUM of the combined z (flow ratio + discharge pressure). On full_week the healthy
# maximum is ~4.1 at k = 0.5, so H = 6.15 (1.5x) flags wear early without false alarms.
WEAR_CUSUM_K = 0.5
WEAR_CUSUM_H = 6.15
# A leak's pressure deficit appears as a step of about this fraction of the pipeline's
# running-vs-idle pressure offset, then grows linearly (measured on history/full_week).
LEAK_STEP_FRACTION = 0.27


@dataclass
class Event:
    event_id: str
    system_id: str
    facility_id: str
    asset_id: str
    category: str
    status: str
    onset_unix: int
    end_unix: int | None
    confidence: float
    evidence: list[str] = field(default_factory=list)
    detected_unix: int | None = None   # first sample at which the detector was sure
    last_seen_unix: int | None = None  # last running sample that still showed the fault
    unverified: bool = False           # ongoing only because the pump has not run since last_seen

    @property
    def onset_ts(self) -> str:
        return _iso(self.onset_unix)

    @property
    def end_ts(self) -> str | None:
        return _iso(self.end_unix) if self.end_unix is not None else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["onset_ts"] = self.onset_ts
        d["end_ts"] = self.end_ts
        d["detected_ts"] = _iso(self.detected_unix) if self.detected_unix is not None else None
        return d


def _iso(t: int) -> str:
    return datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- robust stats ----------------------------------------------------------------------
def _noise_sigma(x: np.ndarray) -> float:
    """Sample noise from first differences (robust to slow trends and rare steps)."""
    if len(x) < 3:
        return float("nan")
    d = np.diff(x)
    mad = np.median(np.abs(d - np.median(d)))
    return float(1.4826 * mad / np.sqrt(2))


def _healthy_baseline(x: np.ndarray, sigma: float, direction: int) -> float:
    """Median of x after dropping samples that are far out in the fault direction.

    direction = -1: faults push x down (ratio, pressure); +1: faults push x up (loss).

    Faults only push one way, so the healthy level is the dense cluster at the fault-free
    end of the distribution. Seeding from that end (instead of the overall median) keeps the
    baseline right even when a fault covers most of the samples, e.g. early in a dataset.
    """
    q = 0.95 if direction < 0 else 0.05
    base = float(np.quantile(x, q)) + direction * 1.645 * sigma
    for _ in range(3):
        near = np.abs(x - base) < 2.5 * sigma
        if near.sum() < 10:
            break
        base = float(np.median(x[near]))
    return base


@dataclass
class _Signal:
    name: str
    values: np.ndarray
    base: float
    sigma: float
    direction: int  # sign of the fault deviation

    @property
    def z(self) -> np.ndarray:
        return (self.values - self.base) * self.direction / self.sigma


def _make_signal(name: str, values: np.ndarray, direction: int, sigma_floor: float) -> _Signal:
    sigma = max(_noise_sigma(values), sigma_floor)
    base = _healthy_baseline(values, sigma, direction)
    return _Signal(name, values, base, sigma, direction)


# --- segmenting ------------------------------------------------------------------------
def _segments(z: np.ndarray) -> list[tuple[int, int, int]]:
    """Return (first_flag_idx, last_bad_idx, end_idx_or_-1) over a running-sample z series.

    Flag when the rolling mean over WINDOW exceeds FLAG_Z; close when the rolling mean over
    CLEAR_WINDOW falls below CLEAR_Z. end_idx is the first healthy sample after the fault,
    or -1 if the fault is still present at the last sample.
    """
    n = len(z)
    if n < WINDOW:
        return []
    roll = pd.Series(z).rolling(WINDOW, min_periods=WINDOW).mean().to_numpy()
    roll_c = pd.Series(z).rolling(CLEAR_WINDOW, min_periods=CLEAR_WINDOW).mean().to_numpy()
    out = []
    i = 0
    while i < n:
        if not (roll[i] > FLAG_Z):
            i += 1
            continue
        start = i
        j = i + 1
        while j < n and not (roll_c[j] < CLEAR_Z):
            j += 1
        # j is the first index whose trailing CLEAR_WINDOW looks healthy (or n)
        seg_lo = max(start - WINDOW + 1, 0)
        hi = min(j, n - 1)
        bad = np.where(z[seg_lo:hi + 1] > SAMPLE_Z)[0]
        last_bad = seg_lo + int(bad[-1]) if len(bad) else start
        flagged = int(np.sum(roll[start:j] > FLAG_Z)) if j > start else 1
        closed = j < n
        if not closed and _snapped_back(z, last_bad):
            closed = True
        if flagged >= MIN_FLAGGED or not closed:
            out.append((start, last_bad, last_bad + 1 if closed else -1))
        # resume only once the flag window no longer holds samples from this fault
        i = max(j + 1, last_bad + WINDOW)
    return out


def _snapped_back(z: np.ndarray, last_bad: int) -> bool:
    """At the end of the data: did a large fault just return to baseline?

    A mature fault sits many sigma out, so a few in-band samples after it are already
    decisive; a small, young fault needs the full CLEAR_WINDOW.
    """
    tail = z[last_bad + 1:]
    if len(tail) == 0 or np.any(tail > SAMPLE_Z):
        return False
    before = z[max(last_bad - CLEAR_WINDOW + 1, 0):last_bad + 1]
    strength = float(np.median(before))
    if strength > 10 * SAMPLE_Z / 3:      # ~10 sigma
        need = 1
    elif strength > 2 * SAMPLE_Z:
        need = 3
    else:
        need = CLEAR_WINDOW
    return len(tail) >= need and float(np.mean(tail)) < CLEAR_Z * 2


def _hinge_onset(t_run: np.ndarray, z: np.ndarray, lower: int, t_flag: int, t_last: int,
                 step_s: int) -> int:
    """Onset of a ramp that leaves its baseline: least-squares fit of z = s * max(t - t0, 0)
    over running samples in [lower, t_last], searching t0 on the sample grid in [lower, t_flag].

    Uses the healthy samples before the onset as well as the ramp, so it is much steadier
    than back-extrapolating a line fitted to the ramp alone.
    """
    sel = (t_run >= lower) & (t_run <= t_last)
    t = t_run[sel].astype(float)
    y = z[sel]
    grid = np.arange(lower, t_flag + 1, step_s, dtype=float)
    if len(t) < 4 or len(grid) == 0:
        return int(t_flag)
    x = np.clip(t[None, :] - grid[:, None], 0, None) / 3600.0      # (G, N)
    sxx = (x * x).sum(1)
    sxy = (x * y[None, :]).sum(1)
    s = np.where(sxx > 0, sxy / np.where(sxx > 0, sxx, 1), 0.0)
    sse = ((y[None, :] - s[:, None] * x) ** 2).sum(1)
    sse[s <= 0] = np.inf
    return int(grid[int(np.argmin(sse))])


def _end_time(ds: Dataset, t_run: np.ndarray, last_bad: int, end_idx: int) -> int | None:
    """Repair time. If the pump was idle between the last faulty and first healthy sample,
    the repair is unobservable inside that gap, so take its midpoint."""
    if end_idx == -1:
        return None
    a, b = int(t_run[last_bad]), int(t_run[end_idx])
    if b - a <= ds.step_s:
        return b
    return int(round((a + b) / 2 / ds.step_s) * ds.step_s)


def _gap_note(ds: Dataset, t_run: np.ndarray, last_bad: int, end_idx: int) -> str:
    a, b = int(t_run[last_bad]), int(t_run[end_idx])
    if b - a <= ds.step_s:
        return ""
    return f"; pump idle from {_iso(a)} to {_iso(b)}, so the repair happened in that window"


def _next_pump_start(ds: Dataset, fac: Facility) -> str:
    """Plain-English note on when the pump should next run (to re-check an idle-pump fault)."""
    st = _status(ds, fac)
    if st[-1] == 2:
        return "pump is in fault, so the line cannot be checked until it is repaired"
    lvl = ds.series(fac.tank_id, "level_pct").to_numpy()
    k = min(6, len(lvl) - 1)
    rate = (lvl[-1] - lvl[-1 - k]) / (k * ds.step_s / 3600) if k > 0 else 0.0
    if rate <= 0:
        return "pump idle; next start time unknown"
    eta_h = max(fac.start_level_pct - lvl[-1], 0) / rate
    return (f"tank at {lvl[-1]:.1f}% rising {rate:.1f} pts/h, pump should start at "
            f"{fac.start_level_pct:.0f}% around {_iso(ds.now_unix + eta_h * 3600)}")


def _confidence(zbar: float) -> float:
    """Map a mean z-score to a 0..1 confidence; FLAG_Z -> ~0.5, 3*FLAG_Z -> ~0.95."""
    return float(np.clip(1.0 / (1.0 + np.exp(-(zbar - FLAG_Z) * 2.0)), 0.0, 0.999))


# --- detectors -------------------------------------------------------------------------
def _status(ds: Dataset, fac: Facility) -> np.ndarray:
    """Pump status with short telemetry gaps (<= 30 min) filled by the last known state."""
    limit = max(1, 1800 // ds.step_s)
    return ds.series(fac.pump_id, "status").ffill(limit=limit).to_numpy().copy()


def _running(ds: Dataset, fac: Facility, *cols: tuple[str, str]) -> tuple[np.ndarray, np.ndarray]:
    """Times and mask of samples where the pump runs and every listed reading is present."""
    mask = (ds.series(fac.pump_id, "status") == 1).to_numpy().copy()  # observed, not filled
    for asset, metric in cols:
        if not ds.has(asset, metric):
            return np.array([], dtype=np.int64), np.zeros(len(mask), bool)
        mask &= np.isfinite(ds.series(asset, metric).to_numpy())
    return ds.wide.index.to_numpy()[mask], mask


def detect_pump_failures(ds: Dataset, fac: Facility) -> list[dict]:
    st = _status(ds, fac)
    t = ds.wide.index.to_numpy()
    lvl = ds.series(fac.tank_id, "level_pct").to_numpy()
    out = []
    fault = st == 2
    i, n = 0, len(st)
    while i < n:
        if not fault[i]:
            i += 1
            continue
        j = i
        while j < n and fault[j]:
            j += 1
        end = int(t[j]) if j < n else None
        rise = lvl[min(j, n) - 1] - lvl[i]
        hours = (t[min(j, n) - 1] - t[i]) / 3600
        ev = [f"Pump status = 2 (fault) since {_iso(t[i])}",
              f"flow_bpd = 0 and discharge_psi at disposal pressure (~{fac.disposal_psi:.0f} psi)",
              f"Tank {fac.tank_id} level {lvl[i]:.1f}% -> {lvl[min(j, n) - 1]:.1f}% "
              f"({rise:+.1f} pts over {hours:.1f} h)"]
        if end is not None:
            ev.append(f"Pump status left fault at {_iso(end)}")
        out.append(dict(category="pump_failure", asset_id=fac.pump_id, onset=int(t[i]), end=end,
                        confidence=0.99, evidence=ev, detected=int(t[i]), last_seen=None))
        i = j
    return out


def _cusum(z: np.ndarray, k: float, resets: set[int]) -> np.ndarray:
    """One-sided CUSUM of z: accumulates sustained evidence of a drop, forgets noise."""
    s = np.zeros(len(z))
    acc = 0.0
    for i, v in enumerate(z):
        if i in resets:
            acc = 0.0
        acc = max(0.0, acc + v - k)
        s[i] = acc
    return s


def _wear_segments(z: np.ndarray) -> list[tuple[int, int, int, int, float]]:
    """Worn-pump segments: (start, last_bad, end_idx, flag_idx, evidence).

    The rolling-mean segments confirm a wear event and decide when it ends. A CUSUM of the same
    z flags it earlier (flag_idx), and an unconfirmed CUSUM alarm at the end of the data is
    reported as a tentative, low-confidence event. evidence = peak CUSUM, which feeds confidence.
    """
    confirmed = _segments(z)
    resets = {e for _, _, e, in confirmed if e != -1}
    s = _cusum(z, WEAR_CUSUM_K, resets)
    out = []
    for start, last_bad, end_idx in confirmed:
        # walk back through the CUSUM excursion that leads into this segment
        j = start
        while j > 0 and s[j - 1] > 0 and (j - 1) not in resets:
            j -= 1
        above = [i for i in range(j, start + 1) if s[i] > WEAR_CUSUM_H]
        flag = above[0] if above else start
        out.append((start, last_bad, end_idx, flag, float(s[flag:last_bad + 1].max())))
    # tentative: CUSUM alarm still open at the end of the data, not yet confirmed
    open_confirmed = any(e == -1 for _, _, e in confirmed)
    n = len(z)
    if not open_confirmed and n and s[-1] > WEAR_CUSUM_H:
        j = n - 1
        while j > 0 and s[j - 1] > 0 and (j - 1) not in resets:
            j -= 1
        flag = next(i for i in range(j, n) if s[i] > WEAR_CUSUM_H)
        out.append((flag, n - 1, -1, flag, float(s[flag:].max())))
    return out


def _wear_confidence(evidence: float) -> float:
    """Low right at the alarm (~0.4), rising as the CUSUM keeps accumulating (0.86 at 4x)."""
    return float(min(0.999, 1.0 - np.exp(-evidence / (2.0 * WEAR_CUSUM_H))))


def detect_pump_degradation(ds: Dataset, fac: Facility, failures: list[dict]) -> list[dict]:
    t_run, mask = _running(ds, fac, (fac.pump_id, "flow_bpd"), (fac.pump_id, "discharge_psi"))
    if len(t_run) < WINDOW * 2:
        return []
    flow = ds.series(fac.pump_id, "flow_bpd").to_numpy()[mask]
    dis = ds.series(fac.pump_id, "discharge_psi").to_numpy()[mask]
    ratio = flow / fac.max_flow_bpd
    s_ratio = _make_signal("flow_ratio", ratio, -1, 1e-4)
    s_dis = _make_signal("discharge_psi", dis, -1, 0.1)
    z = (s_ratio.z + s_dis.z) / 2
    fail_onsets = sorted(f["onset"] for f in failures)
    fail_ends = sorted(f["end"] for f in failures if f["end"] is not None)
    out = []
    prev_end = ds.start_unix
    for start, last_bad, end_idx, flag, evidence in _wear_segments(z):
        lo = max(min(start, flag) - WINDOW + 1, 0)
        hi = last_bad
        th = (t_run[lo:hi + 1] - t_run[lo]) / 3600.0
        y = ratio[lo:hi + 1]
        if len(y) < 4:
            continue
        slope, icpt = np.polyfit(th, y, 1)
        if slope >= 0:
            continue  # not a decline
        first = min(start, flag)
        lower = max([prev_end] + [e for e in fail_ends if e <= t_run[first]])
        onset = _hinge_onset(t_run, z, lower, int(t_run[first]), int(t_run[hi]), ds.step_s)
        prev_end = int(t_run[end_idx]) if end_idx != -1 else int(t_run[-1])
        # end: repair (snap back) or the pump failing, whichever comes first
        end = _end_time(ds, t_run, last_bad, end_idx)
        nxt_fail = [f for f in fail_onsets if f > t_run[last_bad]]
        if nxt_fail and (end is None or nxt_fail[0] <= end):
            end = nxt_fail[0]
        conf = _wear_confidence(evidence)
        r_now = y[-1]
        ev = [f"Flow ratio (flow/max_flow) trending {slope * 100:+.2f}%/h while running "
              f"(baseline {s_ratio.base:.3f}, latest {r_now:.3f})",
              f"Discharge pressure down {s_dis.base - dis[hi]:.0f} psi from its baseline {s_dis.base:.0f} psi",
              f"Decline extrapolates back to baseline at {_iso(onset)}"]
        if end is not None and end in fail_onsets:
            ev.append(f"Ended when the pump failed at {_iso(end)}")
        elif end is not None:
            ev.append(f"Flow ratio back to baseline by {_iso(end)} (repaired)"
                      + _gap_note(ds, t_run, last_bad, end_idx))
        if conf < 0.6:
            ev.insert(0, f"Early flag: the drop is still small, so confidence is {conf:.0%} and will rise "
                         f"as the trend continues")
        out.append(dict(category="pump_degradation", asset_id=fac.pump_id, onset=onset, end=end,
                        confidence=conf, evidence=ev, detected=int(t_run[first]),
                        last_seen=int(t_run[last_bad])))
    return out


def detect_pipeline_leaks(ds: Dataset, fac: Facility) -> list[dict]:
    t_run, mask = _running(ds, fac, (fac.pump_id, "flow_bpd"),
                           (fac.pipeline_id, "receive_flow_bpd"), (fac.pipeline_id, "pressure_psi"))
    if len(t_run) < WINDOW * 2:
        return []
    flow = ds.series(fac.pump_id, "flow_bpd").to_numpy()[mask]
    recv = ds.series(fac.pipeline_id, "receive_flow_bpd").to_numpy()[mask]
    pres = ds.series(fac.pipeline_id, "pressure_psi").to_numpy()[mask]
    loss = flow - recv
    s_p = _make_signal("pressure_psi", pres, -1, 0.1)
    s_l = _make_signal("loss_bpd", loss, +1, 0.5)
    z = (s_p.z + s_l.z) / 2
    # Idle pipeline pressure (pump off / failed) gives the size of the running offset.
    st = _status(ds, fac)
    idle = ds.series(fac.pipeline_id, "pressure_psi").to_numpy()[st != 1]
    idle = idle[np.isfinite(idle)]
    offset = s_p.base - (float(np.median(idle)) if len(idle) else fac.disposal_psi)
    step = LEAK_STEP_FRACTION * offset
    out = []
    for start, last_bad, end_idx in _segments(z):
        # walk back from the first flagged sample to the first leaking sample
        first = start
        while first - 1 >= 0 and s_p.z[first - 1] > 2.0 and \
                t_run[first] - t_run[first - 1] <= ds.step_s:
            first -= 1
        # the first leaking sample may sit right after the flag window: take earliest z>2 run
        k = max(start - WINDOW + 1, 0)
        cand = [i for i in range(k, start + 1) if s_p.z[i] > 2.0 and np.all(s_p.z[i:start + 1] > 0)]
        if cand:
            first = min(first, cand[0])
        hi = last_bad
        fit_hi = min(hi, first + 6 * 3600 // ds.step_s)  # first ~6 running hours
        th = (t_run[first:fit_hi + 1] - t_run[first]) / 3600.0
        deficit = s_p.base - pres[first:fit_hi + 1]
        onset = int(t_run[first])
        gap_before = first > 0 and (t_run[first] - t_run[first - 1]) > ds.step_s
        if gap_before and len(th) >= 6:
            (slope, icpt), cov = np.polyfit(th, deficit, 1, cov=True)
            # only trust the back-extrapolation once the growth rate is clearly measured
            if slope - 2 * np.sqrt(max(cov[0, 0], 0)) > 0:
                back_h = max(icpt - step, 0.0) / slope
                lower = int(t_run[first - 1]) + ds.step_s
                onset = int(np.clip(t_run[first] - back_h * 3600, lower, t_run[first]))
                onset = int(round(onset / ds.step_s) * ds.step_s)
        end = _end_time(ds, t_run, last_bad, end_idx)
        zbar = float(np.mean(z[start:hi + 1])) if hi >= start else float(z[start])
        ev = [f"Pipeline pressure {s_p.base - pres[hi]:.0f} psi below its running baseline "
              f"{s_p.base:.0f} psi (noise {s_p.sigma:.1f} psi)",
              f"Flow lost in pipeline (pump flow - received) {np.mean(loss[max(hi - 11, start):hi + 1]):.0f} bpd "
              f"(healthy {s_l.base:+.0f} +/- {s_l.sigma:.0f} bpd)",
              _discharge_note(ds, fac, t_run[start], t_run[hi])]
        if end is not None:
            ev.append(f"Pressure and flow back to normal by {_iso(end)} (repaired)"
                      + _gap_note(ds, t_run, last_bad, end_idx))
        out.append(dict(category="pipeline_leak", asset_id=fac.pipeline_id, onset=onset, end=end,
                        confidence=_confidence(zbar), evidence=ev, detected=int(t_run[start]),
                        last_seen=int(t_run[last_bad])))
    return out


def _discharge_note(ds: Dataset, fac: Facility, t0: int, t1: int) -> str:
    """Compare pump discharge pressure during the leak with the pump's own running baseline."""
    _, mask = _running(ds, fac, (fac.pump_id, "discharge_psi"))
    dis = ds.series(fac.pump_id, "discharge_psi")[mask]
    if len(dis) < WINDOW:
        return "Pump discharge pressure not available"
    sig = _make_signal("discharge_psi", dis.to_numpy(), -1, 0.1)
    during = dis.loc[t0:t1]
    shift = float(during.mean() - sig.base) if len(during) else 0.0
    if abs(shift) < 3 * sig.sigma:
        return (f"Pump discharge pressure normal ({shift:+.1f} psi vs baseline), "
                f"so the loss is in the pipeline, not the pump")
    return f"Pump discharge pressure also shifted {shift:+.1f} psi; check the pump too"


def detect_events(ds: Dataset) -> list[Event]:
    raw: list[tuple[Facility, dict]] = []
    for fac in ds.facilities.values():
        fails = detect_pump_failures(ds, fac)
        raw += [(fac, e) for e in fails]
        raw += [(fac, e) for e in detect_pump_degradation(ds, fac, fails)]
        raw += [(fac, e) for e in detect_pipeline_leaks(ds, fac)]
    raw.sort(key=lambda p: (p[1]["onset"], p[1]["asset_id"]))
    events = []
    for n, (fac, e) in enumerate(raw, 1):
        unverified = False
        if e["end"] is None and e["category"] != "pump_failure" and e.get("last_seen") is not None:
            st = _status(ds, fac)
            if st[-1] != 1:
                unverified = True
                e["evidence"].append(
                    f"Last confirmed at {_iso(e['last_seen'])}; the pump has not run since, so a "
                    f"repair would not show yet ({_next_pump_start(ds, fac)})")
        events.append(Event(
            event_id=f"EV-{n:04d}", system_id=fac.system_id, facility_id=fac.id,
            asset_id=e["asset_id"], category=e["category"],
            status="ongoing" if e["end"] is None else "resolved",
            onset_unix=e["onset"], end_unix=e["end"], confidence=round(e["confidence"], 3),
            evidence=e["evidence"], detected_unix=e["detected"],
            last_seen_unix=e.get("last_seen"), unverified=unverified,
        ))
    return events
