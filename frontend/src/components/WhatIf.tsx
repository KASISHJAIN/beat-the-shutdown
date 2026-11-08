import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Plan, WhatIf as WhatIfData } from "../types";
import type { Palette } from "../theme";
import { dayTime, hhmm, hhmmss, money } from "../format";

interface Props {
  whatif: WhatIfData;
  plan: Plan;
  palette: Palette;
  replay: boolean; // a resolved event re-planned at its onset
}

export function WhatIf({ whatif: w, plan, palette: c, replay }: Props) {
  const nothingTrip = w.nothing.find((p) => p.level >= w.shutdown_pct - 0.01);
  const planTrip = plan.kind === "accept_trip" || plan.kind === "tripped";
  const peak = Math.max(...w.plan.map((p) => p.level));
  return (
    <div className="whatif">
      <div className="section-head">
        <h3>What-if replay</h3>
        <span className="muted">{replay ? `planned at onset ${dayTime(w.start_unix)}` : `from now ${hhmm(w.start_unix)}`} to repair {hhmm(w.repair_end_unix)} UTC</span>
      </div>
      <div className="chart chart-whatif">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart margin={{ top: 8, right: 12, bottom: 0, left: -12 }}>
            <CartesianGrid stroke={c.grid} strokeDasharray="3 3" />
            <XAxis dataKey="t" type="number" domain={[w.start_unix, w.repair_end_unix]}
              tickFormatter={w.repair_end_unix - w.start_unix < 3600 ? hhmmss : hhmm}
              stroke={c.muted} tick={{ fontSize: 11 }} allowDuplicatedCategory={false} />
            <YAxis domain={[(min: number) => Math.max(0, Math.floor(Math.min(min, w.warning_pct - 15) / 10) * 10), 100]} stroke={c.muted}
              tick={{ fontSize: 11 }} unit="%" />
            <Tooltip labelFormatter={(t) => `${hhmm(Number(t))} UTC`}
              formatter={(v) => (typeof v === "number" ? `${v.toFixed(1)}%` : String(v))}
              contentStyle={{ background: c.surface, border: `1px solid ${c.grid}`, color: c.text }} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <ReferenceLine y={w.shutdown_pct} stroke={c.shutdown} strokeDasharray="6 3"
              label={{ value: `shutdown ${w.shutdown_pct}%`, position: "insideBottomLeft", fill: c.shutdown, fontSize: 11 }} />
            <ReferenceLine y={w.warning_pct} stroke={c.warning} strokeDasharray="6 3"
              label={{ value: `warning ${w.warning_pct}%`, position: "insideTopLeft", fill: c.warning, fontSize: 11 }} />
            {w.truck_arrivals_unix.map((t, i) => (
              <ReferenceLine key={`tr${i}`} x={t} stroke={c.accent} strokeDasharray="2 2"
                label={{ value: "truck", position: "insideBottomRight", fill: c.accent, fontSize: 10 }} />
            ))}
            {w.shutins.map((s, i) => (
              <ReferenceLine key={`si${i}`} x={s.start_unix} stroke={c.series3} strokeDasharray="2 2"
                label={{ value: "shut-in", position: "insideTopRight", fill: c.series3, fontSize: 10 }} />
            ))}
            <Line data={w.nothing} dataKey="level" name={`Do nothing (${money(w.nothing_cost)})`} stroke={c.nothing}
              strokeWidth={2} dot={false} strokeDasharray="5 4" isAnimationActive={false} />
            <Line data={w.plan} dataKey="level" name={`Recommended plan (${money(w.plan_cost)})`} stroke={c.plan}
              strokeWidth={2.5} dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="whatif-summary">
        <div>
          <span className="key-dot" style={{ background: c.plan }} />
          <strong>{plan.summary}</strong>: {planTrip ? "the tank still reaches shutdown"
            : peak > w.shutdown_pct - 0.1 ? `tops out just under ${w.shutdown_pct}% as the repair finishes, never trips`
            : `peaks at ${peak.toFixed(1)}%, never trips`}.
        </div>
        <div>
          <span className="key-dot" style={{ background: c.nothing }} />
          Do nothing: {nothingTrip ? `trips at ${hhmm(nothingTrip.t)} UTC, ${money(w.nothing_cost)} lost production` : "never trips"}.
        </div>
      </div>
    </div>
  );
}
