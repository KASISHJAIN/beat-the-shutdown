import { useEffect, useMemo, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import type { EventRecord, Facility, PlanResponse } from "../types";
import type { Palette } from "../theme";
import { CATEGORY_LABEL, dayTime, hhmm, hhmmss, money, pct } from "../format";
import { TankBar } from "./TankBar";
import { WhatIf } from "./WhatIf";

interface Props {
  dataset: string;
  now: number;
  limits: { warning_pct: number; shutdown_pct: number };
  live?: boolean; // the hardware twin: short time ranges, vibration, no flow/pressure sensors
  facility: Facility;
  focusEventId: string | null;
  palette: Palette;
  onClose: () => void;
}

type Row = { t: number; [k: string]: number | null };
const RANGES = [{ h: 12, label: "12 h" }, { h: 48, label: "48 h" }, { h: 168, label: "7 d" }];
const LIVE_RANGES = [{ h: 10 / 60, label: "10 min" }, { h: 0.5, label: "30 min" }, { h: 2, label: "2 h" }];
const SHORT: Record<string, string> = { pump_failure: "failure", pipeline_leak: "leak", pump_degradation: "wear" };
const PUMP_STATUS: Record<number, string> = { 0: "stopped", 1: "running", 2: "FAULT" };

function merge(series: Record<string, { t: number[]; v: (number | null)[] }>, prefix = ""): Map<number, Row> {
  const m = new Map<number, Row>();
  for (const [k, s] of Object.entries(series)) {
    s.t.forEach((t, i) => {
      const r = m.get(t) ?? { t };
      r[prefix + k] = s.v[i];
      m.set(t, r);
    });
  }
  return m;
}

export function FacilityDetail({ dataset, now, limits: network, live, facility: f, focusEventId, palette: c, onClose }: Props) {
  const ranges = live ? LIVE_RANGES : RANGES;
  const [hours, setHours] = useState(live ? 10 / 60 : 48);
  const [rows, setRows] = useState<Row[]>([]);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setErr(null);
    Promise.all([
      api.series(dataset, f.tank_id, ["level_pct"], now, hours),
      api.series(dataset, f.pump_id, ["flow_bpd", "discharge_psi", "status", "vibration"], now, hours),
      api.series(dataset, f.pipeline_id, ["receive_flow_bpd", "pressure_psi"], now, hours),
      api.series(dataset, f.id, ["inflow_bpd"], now, hours),
    ]).then(([tank, pump, pipe, fac]) => {
      if (!live) return;
      const m = merge(tank.series, "tank_");
      for (const [src, pre] of [[pump, "pump_"], [pipe, "pipe_"], [fac, "fac_"]] as const) {
        merge(src.series, pre).forEach((r, t) => m.set(t, { ...(m.get(t) ?? { t }), ...r }));
      }
      setRows([...m.values()].sort((a, b) => a.t - b.t));
    }).catch((e) => live && setErr(String(e.message ?? e)));
    return () => { live = false; };
  }, [dataset, f.id, f.tank_id, f.pump_id, f.pipeline_id, now, hours]);

  useEffect(() => {
    let live = true;
    api.events(dataset, now, f.id).then((r) => live && setEvents(r.events)).catch(() => live && setEvents([]));
    return () => { live = false; };
  }, [dataset, f.id, now]);

  // The pump failure to replay: the focused one, else the ongoing one, else the latest resolved one.
  const failure = useMemo(() => {
    const fails = events.filter((e) => e.category === "pump_failure");
    return fails.find((e) => e.event_id === focusEventId)
      ?? fails.find((e) => e.status === "ongoing")
      ?? fails.sort((a, b) => b.onset_unix - a.onset_unix)[0]
      ?? null;
  }, [events, focusEventId]);

  useEffect(() => {
    let live = true;
    setPlan(null);
    if (!failure) return;
    api.plan(dataset, failure.event_id, now).then((r) => live && setPlan(r)).catch(() => live && setPlan(null));
    return () => { live = false; };
  }, [dataset, failure?.event_id, now]); // eslint-disable-line react-hooks/exhaustive-deps

  const t0 = rows[0]?.t ?? now - hours * 3600;
  const shown = events.filter((e) => (e.end_unix ?? now) >= t0);
  const catColor = (cat: string) =>
    cat === "pump_failure" ? c.sev.critical : cat === "pipeline_leak" ? c.sev.urgent : c.sev.warning;
  const marks = shown.flatMap((e) => {
    const out = [];
    if (e.onset_unix >= t0) out.push(
      <ReferenceLine key={`on-${e.event_id}`} x={e.onset_unix} stroke={catColor(e.category)} strokeWidth={1.5}
        label={{ value: SHORT[e.category] ?? "onset", position: "top", fill: catColor(e.category), fontSize: 10 }} />);
    if (e.end_unix && e.end_unix >= t0) out.push(
      <ReferenceLine key={`end-${e.event_id}`} x={e.end_unix} stroke={catColor(e.category)} strokeDasharray="3 3" />);
    return out;
  });
  const fmtTick = (t: number) => (live ? hhmmss(t) : hours > 48 ? dayTime(t).slice(4, 9) : hhmm(t));
  const has = (k: string) => rows.some((r) => r[k] != null);
  const axis = (unit: string) => ({
    x: <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={fmtTick} stroke={c.muted} tick={{ fontSize: 11 }} minTickGap={40} />,
    y: <YAxis stroke={c.muted} tick={{ fontSize: 11 }} unit={unit} width={58} domain={["auto", "auto"]} />,
    tip: <Tooltip labelFormatter={(t) => `${dayTime(Number(t))} UTC`}
      formatter={(v) => (typeof v === "number" ? v.toLocaleString("en-US", { maximumFractionDigits: 1 }) : String(v ?? "-"))}
      contentStyle={{ background: c.surface, border: `1px solid ${c.grid}`, color: c.text }} />,
  });
  const last = rows[rows.length - 1];
  const ongoing = events.filter((e) => e.status === "ongoing");

  return (
    <section className="panel detail" aria-label={`${f.id} detail`}>
      <div className="panel-head">
        <div>
          <h2>{f.id}{live && <span className="live-badge" style={{ marginLeft: 8 }}><i />Live</span>}</h2>
          <div className="muted small">{f.tank_id} · {f.pump_id} · {f.pipeline_id}</div>
        </div>
        <button className="btn btn-small" onClick={onClose} aria-label="Close detail">Close</button>
      </div>

      <div className="stats">
        <div className="stat"><span>Tank</span><strong>{pct(last?.tank_level_pct as number | null)}</strong></div>
        <div className="stat"><span>Pump</span><strong className={last?.pump_status === 2 ? "sev-critical-text" : ""}>
          {last?.pump_status == null ? "-" : PUMP_STATUS[last.pump_status as number] ?? last.pump_status}</strong></div>
        <div className="stat"><span>Inflow</span><strong>{last?.fac_inflow_bpd == null ? "-" : `${Math.round(last.fac_inflow_bpd as number)} bpd`}</strong></div>
        <div className="stat"><span>Capacity</span><strong>{f.capacity_bbl.toLocaleString()} bbl</strong></div>
      </div>
      <TankBar level={f.level_pct} warning={network.warning_pct} shutdown={network.shutdown_pct} />

      {ongoing.length > 0 && (
        <ul className="detail-events">
          {ongoing.map((e) => (
            <li key={e.event_id} className={`chip chip-${e.category}`}>
              {CATEGORY_LABEL[e.category]} since {dayTime(e.onset_unix)}
              {e.unverified && <span className="tag tag-unverified">Unverified</span>}
            </li>
          ))}
        </ul>
      )}

      {plan?.plan && plan.whatif && (
        <WhatIf whatif={plan.whatif} plan={plan.plan} palette={c} replay={plan.status === "resolved"} />
      )}

      <div className="section-head">
        <h3>History</h3>
        <div className="seg" role="group" aria-label="History range">
          {ranges.map((r) => (
            <button key={r.h} className={`seg-btn ${hours === r.h ? "seg-on" : ""}`} onClick={() => setHours(r.h)}>{r.label}</button>
          ))}
        </div>
      </div>
      {err && <div className="error">{err}</div>}

      <h4 className="chart-title">Tank level</h4>
      <div className="chart">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={rows} syncId="fac" margin={{ top: 16, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid stroke={c.grid} strokeDasharray="3 3" />
            {axis("%").x}
            <YAxis stroke={c.muted} tick={{ fontSize: 11 }} unit="%" width={58} domain={[0, 100]} />
            {axis("%").tip}
            <ReferenceLine y={network.shutdown_pct} stroke={c.shutdown} strokeDasharray="6 3" />
            <ReferenceLine y={network.warning_pct} stroke={c.warning} strokeDasharray="6 3" />
            {marks}
            <Line dataKey="tank_level_pct" name="Level" stroke={c.series1} dot={false} strokeWidth={1.8} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>

      {live && has("pump_vibration") && (<>
        <h4 className="chart-title">Pump vibration (g, std-dev of acceleration)</h4>
        <div className="chart">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={rows} syncId="fac" margin={{ top: 16, right: 16, bottom: 0, left: 0 }}>
              <CartesianGrid stroke={c.grid} strokeDasharray="3 3" />
              {axis("").x}{axis("").y}{axis("").tip}
              {marks}
              <Line dataKey="pump_vibration" name="Vibration" stroke={c.series3} dot={false} strokeWidth={1.5} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </>)}

      {(has("pump_flow_bpd") || has("pipe_receive_flow_bpd")) && (<>
      <h4 className="chart-title">Flow (bbl/day)</h4>
      <div className="chart">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={rows} syncId="fac" margin={{ top: 16, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid stroke={c.grid} strokeDasharray="3 3" />
            {axis("").x}{axis("").y}{axis("").tip}
            {marks}
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line dataKey="pump_flow_bpd" name="Pump flow" stroke={c.series1} dot={false} strokeWidth={1.5} isAnimationActive={false} />
            <Line dataKey="pipe_receive_flow_bpd" name="Received at disposal" stroke={c.series2} dot={false} strokeWidth={1.5} isAnimationActive={false} />
            <Line dataKey="fac_inflow_bpd" name="Tank inflow" stroke={c.muted} dot={false} strokeWidth={1} strokeDasharray="4 3" isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      </>)}

      {(has("pump_discharge_psi") || has("pipe_pressure_psi")) && (<>
      <h4 className="chart-title">Pressure (psi)</h4>
      <div className="chart">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={rows} syncId="fac" margin={{ top: 16, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid stroke={c.grid} strokeDasharray="3 3" />
            {axis("").x}{axis("").y}{axis("").tip}
            {marks}
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line dataKey="pump_discharge_psi" name="Pump discharge" stroke={c.series3} dot={false} strokeWidth={1.5} isAnimationActive={false} />
            <Line dataKey="pipe_pressure_psi" name="Pipeline (disposal end)" stroke={c.series2} dot={false} strokeWidth={1.5} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      </>)}

      <h3 className="section-gap">Wells</h3>
      <table className="wells">
        <thead><tr><th>Well</th><th>Water bpd</th><th>Lost $/h if shut</th><th>Status</th></tr></thead>
        <tbody>
          {f.wells.map((w) => (
            <tr key={w.id}>
              <td>{w.id}</td><td>{w.baseline_water_bpd}</td><td>{money(w.revenue_per_hour)}</td>
              <td>{w.status === 0 ? <span className="sev-critical-text">shut in</span> : "producing"}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {events.length > 0 && (
        <>
          <h3 className="section-gap">Events at this facility</h3>
          <ul className="event-log">
            {[...events].sort((a, b) => b.onset_unix - a.onset_unix).map((e) => (
              <li key={e.event_id}>
                <span className={`dot dot-${e.category}`} />
                <span><strong>{CATEGORY_LABEL[e.category]}</strong> {e.asset_id}</span>
                <span className="muted">{dayTime(e.onset_unix)} → {e.end_unix ? dayTime(e.end_unix) : "ongoing"}</span>
                {e.est_cost != null && <span className="muted">plan {money(e.est_cost)}</span>}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
