"""Shift-handoff agent (NVIDIA Nemotron, tool-using).

The agent works in steps: it reads the alerts, checks plans and what-ifs for the pump
failures, looks at trends where useful, then writes the handoff with write_handoff. Every
number comes from the tools, which call the existing modules (detect, forecast, optimize,
alerts) on the same dataset and "now" as the console.

Setup: put NVIDIA_API_KEY=... in .env at the repo root (see .env.example). Without a key,
or if the LLM call fails, generate_handoff() builds the handoff from a template using the
same tools, so the console never breaks.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import numpy as np

from .alerts import build
from .detect import detect_events
from .loader import Dataset
from .optimize import (_context, _do_nothing, _make_plan, plan_ongoing, simulate, well_revenue_per_hour,
                       facility_revenue_per_hour)

ROOT = Path(__file__).resolve().parent.parent
HANDOFF_DIR = ROOT / "handoffs"
MAX_STEPS = 8
TIME_BUDGET_S = 120  # whole agent run; past this the console gets the template instead

DEFAULTS = {
    "NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
    "NVIDIA_MODEL": "nvidia/nemotron-3-ultra-550b-a55b",
}


# --- configuration ---------------------------------------------------------------------
def _env() -> dict[str, str]:
    """Process env overrides .env (KEY=VALUE lines, # comments)."""
    vals = dict(DEFAULTS)
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("NVIDIA_API_KEY", "NVIDIA_BASE_URL", "NVIDIA_MODEL"):
        if os.environ.get(k):
            vals[k] = os.environ[k]
    return vals


def llm_available() -> bool:
    return bool(_env().get("NVIDIA_API_KEY"))


# --- the one LLM function -------------------------------------------------------------
def llm_chat(messages: list[dict], tools: list[dict] | None = None, timeout: float = 90.0) -> dict:
    """One chat-completions call to NVIDIA's OpenAI-compatible API. Returns the assistant
    message dict ({"content": ..., "tool_calls": [...]}). Raises on any failure."""
    env = _env()
    key = env.get("NVIDIA_API_KEY")
    if not key:
        raise RuntimeError("NVIDIA_API_KEY not set")
    body: dict[str, Any] = {"model": env["NVIDIA_MODEL"], "messages": messages,
                            "temperature": 0.2, "top_p": 0.7, "max_tokens": 2048}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    r = httpx.post(f"{env['NVIDIA_BASE_URL'].rstrip('/')}/chat/completions", json=body, timeout=timeout,
                   headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    if r.status_code >= 400:
        # Surface NVIDIA's own one-line explanation (e.g. a retired model), not httpx boilerplate.
        try:
            j = r.json()
            detail = j.get("detail") or (j.get("error") or {}).get("message") or j.get("title") or r.text
        except ValueError:
            detail = r.text
        raise RuntimeError(f"NVIDIA API {r.status_code}: {' '.join(str(detail).split())[:200]}")
    return r.json()["choices"][0]["message"]


# --- tools ----------------------------------------------------------------------------
def _iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _r(x, n=2):
    return None if x is None else round(float(x), n)


@dataclass
class Toolbox:
    """Tools bound to one dataset at one "now" (plus the live twin when it's streaming)."""
    ds: Dataset
    events: list
    extra: list[dict] = field(default_factory=list)  # e.g. live-twin alerts, already built
    trace: list[dict] = field(default_factory=list)
    corpus: list[str] = field(default_factory=list)  # every tool output, for the grounding check
    handoff: str | None = None
    _built: dict | None = None

    # -- helpers
    def _alerts_full(self) -> list[dict]:
        if self._built is None:
            self._built = build(self.ds, self.events, include_resolved_plans=False)
        return self._built["alerts"] + self.extra

    def _event(self, event_id: str):
        return next((e for e in self.events if e.event_id == event_id), None)

    # -- tools the model can call
    def get_alerts(self) -> dict:
        out = []
        for a in self._alerts_full():
            h = a.get("hours_to_limit") or {}
            p = a.get("plan") or {}
            start = p.get("plan_start_unix", self.ds.now_unix)
            # Absolute times are computed here so the model never does clock arithmetic.
            shutdown_at = _iso(start + h["p50"] * 3600) if h.get("p50") else None
            repair_due = _iso(start + p["horizon_min"] * 60) if p.get("horizon_min") is not None else None
            out.append({
                "event_id": a["event_id"], "category": a["category"], "facility_id": a["facility_id"],
                "asset_id": a["asset_id"], "since": _iso(a["onset_unix"]), "live_twin": a.get("live", False),
                "confidence": a["confidence"], "unverified": a.get("unverified", False),
                "level_pct": a.get("level_pct"), "hours_to_limit_p50": _r(h.get("p50"), 3),
                "shutdown_at": shutdown_at, "repair_due": repair_due,
                "lost_revenue_per_hour": a.get("lost_revenue_per_hour"),
                "plan": p.get("summary"), "plan_cost": p.get("est_cost"), "do_nothing_cost": p.get("nothing_cost"),
                "actions": a.get("recommended_actions"),
                "evidence": a.get("evidence", [])[:3] + (a.get("evidence", [])[-1:] if a.get("unverified") else []),
            })
        recent = [{"event_id": e.event_id, "category": e.category, "asset_id": e.asset_id,
                   "onset": _iso(e.onset_unix), "end": _iso(e.end_unix)}
                  for e in self.events if e.status == "resolved" and e.end_unix
                  and self.ds.now_unix - e.end_unix <= 12 * 3600]
        return {"now": _iso(self.ds.now_unix), "ongoing_in_spec_order": out, "resolved_last_12h": recent}

    def get_series(self, asset_id: str, metric: str, hours: float = 6) -> dict:
        if not self.ds.has(asset_id, metric):
            return {"error": f"no series {asset_id}.{metric}"}
        s = self.ds.series(asset_id, metric).loc[self.ds.now_unix - int(hours * 3600):].dropna()
        if s.empty:
            return {"error": "no data in window"}
        step = max(1, len(s) // 24)
        pts = s.iloc[::step]
        slope = float(np.polyfit((s.index - s.index[0]) / 3600.0, s.values, 1)[0]) if len(s) > 2 else 0.0
        return {"asset_id": asset_id, "metric": metric, "hours": hours, "first": _r(s.iloc[0]),
                "last": _r(s.iloc[-1]), "min": _r(s.min()), "max": _r(s.max()), "mean": _r(s.mean()),
                "trend_per_hour": _r(slope, 3),
                "points": [[_iso(t)[11:16], _r(v)] for t, v in zip(pts.index, pts.values)]}

    def optimize_plan(self, event_id: str) -> dict:
        ev = self._event(event_id)
        if ev is None or ev.category != "pump_failure" or ev.status != "ongoing":
            return {"error": "optimize_plan needs an ongoing pump_failure event_id from get_alerts"}
        p = plan_ongoing(self.ds, self.events).get(event_id)
        if p is None:
            return {"error": "repair already due; nothing to plan"}
        d = p.to_dict()
        fac = self.ds.facilities[ev.facility_id]
        rev = well_revenue_per_hour(self.ds, fac)
        so = self.ds.ops["systems"][ev.system_id]["facilities"][fac.id]
        end = p.start_unix + p.horizon_min * 60
        # Each action spelled out, so the model copies figures instead of deriving them.
        shutins = [{"well": w, "start_at": _iso(p.start_unix + s * 60), "start_after_min": s,
                    "lost_per_hour": _r(rev[w]), "hours_shut": _r((p.horizon_min - s) / 60),
                    "cost": _r(rev[w] * (p.horizon_min - s) / 60)} for w, s in p.shutins]
        arrivals = [_iso(p.start_unix + (so["response_min"] + i * so["cycle_min"]) * 60)
                    for i in range(p.trips_per_truck)] if p.trucks else []
        return {k: d[k] for k in ("summary", "kind", "est_cost", "nothing_cost", "truck_cost", "shutin_cost",
                                  "trucks_free", "recommended_actions", "explanation", "alternatives")} | {
            "peak_pct": d["peak_pct"], "trips_at": _iso(d["trips_at_unix"]), "repair_due": _iso(end),
            "truck_arrivals_at": arrivals, "trucks": p.trucks, "trips_per_truck": p.trips_per_truck,
            "shutin_details": shutins}

    def what_if(self, event_id: str, trucks: int = 0, trips_per_truck: int = 1,
                shut_in_wells: list[str] | None = None, start_after_min: int = 0) -> dict:
        """Evaluate any plan (trucks and/or shut-ins) against doing nothing."""
        ev = self._event(event_id)
        if ev is None or ev.category != "pump_failure" or ev.status != "ongoing":
            return {"error": "what_if needs an ongoing pump_failure event_id"}
        c = _context(self.ds, ev, self.ds.now_unix)
        if c is None:
            return {"error": "repair already due"}
        wells = [w for w in (shut_in_wells or []) if w in c.st.well_bpd]
        unknown = sorted(set(shut_in_wells or []) - set(wells))
        trucks = int(max(0, trucks))
        p = _make_plan(c, trucks, int(max(1, trips_per_truck)) if trucks else 0,
                       [(w, int(max(0, start_after_min))) for w in wells])
        n = _do_nothing(c)
        out = {"plan": p.summary(), "trips": p.trips_at_unix is not None, "trips_at": _iso(p.trips_at_unix),
               "peak_pct": _r(p.peak_pct, 1), "cost": _r(p.cost), "do_nothing_trips_at": _iso(n.trips_at_unix),
               "do_nothing_cost": _r(n.cost), "fleet_size": self.ds.ops["systems"][ev.system_id]["emergency_fleet_size"],
               "producing_wells": {w: _r(q, 0) for w, q in c.st.well_bpd.items()}}
        if unknown:
            out["ignored_unknown_wells"] = unknown
        return out

    def write_handoff(self, markdown: str) -> dict:
        self.handoff = markdown.strip()
        HANDOFF_DIR.mkdir(exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{self.ds.path.name}_{self.ds.now_unix}.md")
        (HANDOFF_DIR / name).write_text(self.handoff, encoding="utf-8")
        return {"ok": True, "saved_as": f"handoffs/{name}", "chars": len(self.handoff)}

    # -- dispatch
    def call(self, name: str, args: dict) -> dict:
        fn: Callable | None = {"get_alerts": self.get_alerts, "get_series": self.get_series,
                               "optimize_plan": self.optimize_plan, "what_if": self.what_if,
                               "write_handoff": self.write_handoff}.get(name)
        t0 = time.time()
        try:
            out = fn(**args) if fn else {"error": f"unknown tool {name}"}
        except TypeError as e:
            out = {"error": f"bad arguments: {e}"}
        if name != "write_handoff":
            self.corpus.append(json.dumps(out, default=str))
        self.trace.append({"tool": name, "args": {k: v for k, v in args.items() if k != "markdown"},
                           "ms": int((time.time() - t0) * 1000),
                           "ok": "error" not in out})
        return out


TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_alerts",
        "description": "Every problem in progress at 'now' in SPEC order (pump failures by soonest shutdown, then leaks and worn pumps), with countdown, absolute shutdown_at and repair_due times, plan, costs and evidence; plus events resolved in the last 12 h.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_series",
        "description": "Summary and ~24 points of one telemetry series up to now. Metrics: tank level_pct; pump status, flow_bpd, discharge_psi; pipeline receive_flow_bpd, pressure_psi; facility inflow_bpd.",
        "parameters": {"type": "object", "properties": {
            "asset_id": {"type": "string"}, "metric": {"type": "string"},
            "hours": {"type": "number", "description": "window length, default 6"}},
            "required": ["asset_id", "metric"]}}},
    {"type": "function", "function": {
        "name": "optimize_plan",
        "description": "Cheapest plan from now for an ongoing pump failure (trucks shared across the system's failures, shut-ins, mix, or accept the trip), with cost vs doing nothing and the reasoning.",
        "parameters": {"type": "object", "properties": {"event_id": {"type": "string"}}, "required": ["event_id"]}}},
    {"type": "function", "function": {
        "name": "what_if",
        "description": "Evaluate a custom plan for an ongoing pump failure against doing nothing: does the tank trip, when, peak level, cost.",
        "parameters": {"type": "object", "properties": {
            "event_id": {"type": "string"},
            "trucks": {"type": "integer", "description": "trucks sent now"},
            "trips_per_truck": {"type": "integer"},
            "shut_in_wells": {"type": "array", "items": {"type": "string"}},
            "start_after_min": {"type": "integer", "description": "minutes from now until the shut-in starts"}},
            "required": ["event_id"]}}},
    {"type": "function", "function": {
        "name": "write_handoff",
        "description": "Save the finished shift handoff (Markdown). Call exactly once, last.",
        "parameters": {"type": "object", "properties": {"markdown": {"type": "string"}}, "required": ["markdown"]}}},
]

SYSTEM_PROMPT = """You are the outgoing control-room lead for a produced-water disposal network.
Write the shift handoff for the incoming operator.

Work in steps with the tools:
1. Call get_alerts.
2. For every ongoing pump failure, call optimize_plan. Use what_if only to check a real doubt
   (for example the fleet is short or a cheaper option looks plausible).
3. Use get_series sparingly, only when a trend changes what the operator should do.
4. Call write_handoff exactly once with the finished Markdown, then stop.

Handoff format (Markdown, concise, plain English, times in UTC):
# Shift handoff - <now>
## Act now      (pump failures in the given order: facility, time to shutdown, the plan's concrete actions and when, cost vs doing nothing)
## Inspect      (pipeline leaks first, marked URGENT, then worn pumps; say if a leak is unverified)
## Watch        (anything trending toward trouble, the live hardware twin if present)
## Resolved this shift
## Notes        (fleet constraints, assumptions)

Rules:
- Every number, time, well and cost must come from a tool result. Never invent events or facts.
- Use shutdown_at and repair_due exactly as given; never compute clock times yourself.
- Repair times are estimates (failure onset + the expected repair time). Say "repair due HH:MM";
  never describe who scheduled a repair or why.
- Recommend only the actions the tools give (the plans and field inspections). Do not add new
  instructions such as holding a pump, calling crews or changing setpoints.
- If there are no problems, say so plainly."""


# --- template fallback ----------------------------------------------------------------
def _money(x) -> str:
    return "-" if x is None else f"${x:,.0f}"


def _hours_text(h) -> str:
    if h is None:
        return "-"
    m = int(round(h * 60))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def template_handoff(tb: Toolbox, reason: str | None = None) -> str:
    data = tb.call("get_alerts", {})
    alerts = data["ongoing_in_spec_order"]
    fails = [a for a in alerts if a["category"] == "pump_failure"]
    leaks = [a for a in alerts if a["category"] == "pipeline_leak"]
    worn = [a for a in alerts if a["category"] == "pump_degradation"]
    lines = [f"# Shift handoff - {data['now']}", ""]
    if not alerts:
        lines += ["No problems in progress. Every pump, pipeline and tank is reading normally.", ""]

    lines.append("## Act now")
    if not fails:
        lines.append("- No pump failures.")
    for i, a in enumerate(fails, 1):
        p = tb.call("optimize_plan", {"event_id": a["event_id"]}) if not a["live_twin"] else {}
        plan = p.get("summary") or a["plan"] or "-"
        twin = " (LIVE hardware twin)" if a["live_twin"] else ""
        if (a["hours_to_limit_p50"] or 0) <= 0:
            when = "already at shutdown, wells shut in until the repair"
        else:
            when = f"{_hours_text(a['hours_to_limit_p50'])} to shutdown, tank at {a['level_pct']}%"
        lines.append(f"{i}. **{a['facility_id']}**{twin}, pump down since {a['since']}: {when}.")
        lines.append(f"   - Plan: {plan}. Cost {_money(p.get('est_cost', a['plan_cost']))} vs "
                     f"{_money(p.get('nothing_cost', a['do_nothing_cost']))} if nothing is done.")
    lines.append("")

    lines.append("## Inspect")
    if not leaks and not worn:
        lines.append("- Nothing to inspect.")
    for a in leaks:
        flag = " (unverified: the pump has not run since it was last seen)" if a["unverified"] else ""
        lines.append(f"- **URGENT** leak on {a['asset_id']} ({a['facility_id']}) since {a['since']}, "
                     f"confidence {a['confidence']:.0%}{flag}. Field inspection.")
        if a["evidence"]:
            lines.append(f"  - {a['evidence'][0]}")
    for a in worn:
        lines.append(f"- Worn pump {a['asset_id']} ({a['facility_id']}) since {a['since']}, "
                     f"confidence {a['confidence']:.0%}. Field inspection.")
        if a["evidence"]:
            lines.append(f"  - {a['evidence'][0]}")
    lines.append("")

    lines.append("## Watch")
    watch = []
    for a in leaks:
        if a["unverified"]:
            note = next((e for e in a["evidence"] if "should start" in e), "")
            when = note[note.find("pump should start"):].rstrip(")") if "pump should start" in note else ""
            watch.append(f"- {a['asset_id']}: may already be repaired; re-checked when the pump next runs"
                         + (f" ({when})." if when else "."))
    for a in worn:
        trend = next((e for e in a["evidence"] if e.startswith("Flow ratio")), None)
        if trend:
            watch.append(f"- {a['asset_id']} is still losing flow: {trend}. A worn pump can go on to fail.")
    for a in fails:
        if a["live_twin"]:
            watch.append(f"- Hardware twin {a['facility_id']}: live pump fault; plan {a['plan'] or '-'}.")
    lines += watch or ["- Nothing beyond the items above."]
    lines.append("")

    lines.append("## Resolved this shift")
    rec = data["resolved_last_12h"]
    lines += [f"- {r['category'].replace('_', ' ')} on {r['asset_id']}: {r['onset']} to {r['end']}" for r in rec] \
        or ["- None in the last 12 h."]
    lines.append("")
    lines.append("## Notes")
    by_sys: dict[str, list[str]] = {}
    for a in fails:
        ev = tb._event(a["event_id"])
        if ev is not None:
            by_sys.setdefault(ev.system_id, []).append(a["facility_id"])
    for sid, facs in by_sys.items():
        fleet = tb.ds.ops["systems"][sid]["emergency_fleet_size"]
        if len(facs) > 1:
            lines.append(f"- {sid}: {len(facs)} failures share the system's {fleet} trucks; "
                         f"the plans above were made together.")
    lines.append("- Generated from the console's detection, forecast and optimizer (template mode).")
    if reason:
        lines.append(f"- Nemotron not used: {reason}.")
    md = "\n".join(lines)
    tb.call("write_handoff", {"markdown": md})
    return md


# --- the agent loop -------------------------------------------------------------------
def run_agent(tb: Toolbox, budget_s: float = TIME_BUDGET_S) -> str:
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": "Write the shift handoff for now."}]
    deadline = time.time() + budget_s
    for _ in range(MAX_STEPS):
        left = deadline - time.time()
        if left < 5:
            raise TimeoutError(f"agent exceeded its {budget_s:.0f} s budget")
        msg = llm_chat(messages, TOOL_SPECS, timeout=left)
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls} if calls
                        else {"role": "assistant", "content": msg.get("content") or ""})
        if not calls:
            break
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            out = tb.call(name, args)
            messages.append({"role": "tool", "tool_call_id": c.get("id", name), "name": name,
                             "content": json.dumps(out, default=str)[:12000]})
        if tb.handoff:
            break
    if tb.handoff:
        return tb.handoff
    final = (messages[-1].get("content") or "").strip()
    if not final:
        raise RuntimeError("model finished without writing a handoff")
    tb.write_handoff(final)
    return final


_TIME = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
_NUM = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)")
_RATE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?/\s?h(?:our|r)?\b")
_RATE_FULL = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s?/\s?h(?:our|r)?\b")


def ungrounded(markdown: str, corpus: list[str]) -> list[str]:
    """Clock times and dollar amounts in the handoff that appear in no tool output.
    The model is told to copy numbers from tools; this catches it when it doesn't."""
    text = "\n".join(corpus)
    times = {f"{int(h):02d}:{m}" for h, m in _TIME.findall(text)}
    nums = set()
    for n in _NUM.findall(text):
        try:
            nums.add(round(float(n.replace(",", ""))))
        except ValueError:
            pass
    # Rates must match a rate, not just any number (a $62.5 total is not a $62.5/h rate).
    rates = {round(float(n.replace(",", ""))) for n in _RATE.findall(text)}
    for k in re.findall(r'"lost_revenue_per_hour":\s*([\d.]+)', text):
        rates.add(round(float(k)))
    bad = []
    for h, m in _TIME.findall(markdown):
        t = f"{int(h):02d}:{m}"
        if t not in times and t not in bad:
            bad.append(t)
    for n in _RATE.findall(markdown):
        v = round(float(n.replace(",", "")))
        if not ({v - 1, v, v + 1} & rates) and f"${n}/h" not in bad:
            bad.append(f"${n}/h")
    for n in _MONEY.findall(_RATE_FULL.sub("", markdown)):
        v = round(float(n.replace(",", "")))
        if not ({v - 1, v, v + 1} & nums) and f"${n}" not in bad:
            bad.append(f"${n}")
    return bad


def generate_handoff(ds: Dataset, events: list | None = None, extra_alerts: list[dict] | None = None,
                     use_llm: bool = True) -> dict:
    """Shift handoff for one dataset at its "now". Nemotron when a key is set, else template."""
    events = detect_events(ds) if events is None else events
    t0 = time.time()
    env = _env()
    if use_llm and env.get("NVIDIA_API_KEY"):
        tb = Toolbox(ds, events, extra=list(extra_alerts or []))
        try:
            md = run_agent(tb)
            return {"source": "nemotron", "model": env["NVIDIA_MODEL"], "markdown": md, "steps": tb.trace,
                    "ungrounded": ungrounded(md, tb.corpus),
                    "now_unix": ds.now_unix, "seconds": round(time.time() - t0, 1)}
        except Exception as e:  # network, auth, quota, malformed output: fall back, never break
            reason = f"{type(e).__name__}: {str(e)[:160]}"
    else:
        reason = "no NVIDIA_API_KEY in .env" if use_llm else "template requested"
    tb = Toolbox(ds, events, extra=list(extra_alerts or []))
    md = template_handoff(tb, reason)
    return {"source": "template", "model": None, "markdown": md, "steps": tb.trace, "reason": reason,
            "ungrounded": ungrounded(md, tb.corpus),
            "now_unix": ds.now_unix, "seconds": round(time.time() - t0, 1)}


if __name__ == "__main__":
    import sys
    from .loader import load_dataset
    out = generate_handoff(load_dataset(sys.argv[1] if len(sys.argv) > 1 else
                                        ROOT / "HackUTD-ZeroDay" / "history" / "snapshot_2026-10-10T0900"))
    print(f"[{out['source']}] {len(out['steps'])} tool calls\n")
    print(out["markdown"])
