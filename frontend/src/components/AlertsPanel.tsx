import { useEffect, useState } from "react";
import type { Action, ClockedAlert, EventRecord, Network, TwinStatus } from "../types";
import { CATEGORY_LABEL, clock, dayTime, hhmm, hhmmss, hoursText, money } from "../format";
import { TankBar } from "./TankBar";

interface Props {
  alerts: ClockedAlert[]; // dataset alerts and live-twin alerts, already in SPEC order
  network: Network | null;
  nowUnix: number;
  twin: TwinStatus | null;
  selectedFacility: string | null;
  onSelect: (facilityId: string, eventId?: string) => void;
  error: string | null;
}

function useTick(ms = 1000) {
  const [, set] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => set((x) => x + 1), ms);
    return () => window.clearInterval(id);
  }, [ms]);
}

export function AlertsPanel(p: Props) {
  useTick();
  const failures = p.alerts.filter((a) => a.category === "pump_failure");
  const leaks = p.alerts.filter((a) => a.category === "pipeline_leak");
  const worn = p.alerts.filter((a) => a.category === "pump_degradation");
  const warning = p.network?.warning_pct ?? 85;
  const shutdown = p.network?.shutdown_pct ?? 92;
  return (
    <section className="panel alerts" aria-label="Alerts">
      <div className="panel-head">
        <h2>Alerts</h2>
        <div className="counts">
          <span className="count sev-critical-text">{failures.length} pump failure{failures.length === 1 ? "" : "s"}</span>
          <span className="count sev-urgent-text">{leaks.length} leak{leaks.length === 1 ? "" : "s"}</span>
          <span className="count sev-warning-text">{worn.length} worn</span>
        </div>
      </div>
      {p.twin?.configured && <TwinStrip twin={p.twin} onSelect={p.onSelect} />}
      {p.error && <div className="error">Could not load alerts: {p.error}</div>}
      {!p.error && !p.network && (
        <div className="empty"><span className="spinner" /> <div>Loading the network…</div></div>
      )}
      {!p.error && p.network && p.alerts.length === 0 && (
        <div className="empty">
          <div className="empty-dot" />
          <div>
            <strong>No active problems</strong>
            <p>Every pump, pipeline and tank is reading normally at {dayTime(p.nowUnix)} UTC.</p>
          </div>
        </div>
      )}
      <ol className="alert-list">
        {p.alerts.map((a) => (
          <li key={a.event_id}>
            {a.category === "pump_failure" ? (
              <FailureCard a={a} live={a.clockNow + (Date.now() - a.clockLoadedAt) / 1000}
                warning={a.live ? p.twin?.setpoints.warning_pct ?? warning : warning}
                shutdown={a.live ? p.twin?.setpoints.shutdown_pct ?? shutdown : shutdown}
                selected={p.selectedFacility === a.facility_id} onSelect={p.onSelect} />
            ) : (
              <InspectionCard a={a} nowUnix={p.nowUnix}
                selected={p.selectedFacility === a.facility_id} onSelect={p.onSelect} />
            )}
          </li>
        ))}
      </ol>
      <footer className="credit">Built for the EOG Resources HackUTD challenge</footer>
    </section>
  );
}

/** Warning triangle so "critical" never relies on red alone. */
function WarnIcon() {
  return (
    <svg className="sev-icon" viewBox="0 0 16 16" aria-hidden="true">
      <path d="M8 1.5 15 14.5H1z" fill="currentColor" />
      <rect x="7.2" y="6" width="1.6" height="4.6" rx="0.8" fill="var(--surface)" />
      <rect x="7.2" y="11.6" width="1.6" height="1.6" rx="0.8" fill="var(--surface)" />
    </svg>
  );
}

function FailureCard({ a, live, warning, shutdown, selected, onSelect }: {
  a: ClockedAlert; live: number; warning: number; shutdown: number; selected: boolean;
  onSelect: Props["onSelect"];
}) {
  const plan = a.plan;
  const h = a.hours_to_limit;
  const tripped = plan?.kind === "tripped" || (h?.p50 ?? 1) <= 0;
  const deadline = a.onset_unix && h?.p50 != null ? (plan?.plan_start_unix ?? live) + h.p50 * 3600 : null;
  const remaining = deadline ? deadline - live : 0;
  const repairEnd = plan ? plan.plan_start_unix + plan.horizon_min * 60 : null;
  const sev = tripped || remaining < 2 * 3600 ? "critical" : "high";
  const saving = plan ? plan.nothing_cost - plan.est_cost : 0;
  return (
    <article className={`card card-${sev} ${selected ? "card-selected" : ""}`}>
      <header className="card-head">
        <span className="order">{a.order}</span>
        <div className="card-title">
          <div className="card-kicker">
            <span className={`sev-pill sev-${sev}`}>
              {sev === "critical" && <WarnIcon />}{sev === "critical" ? "Critical" : "High"}
            </span>
            {CATEGORY_LABEL[a.category]}
            {a.live && <LiveBadge />}
          </div>
          <h3>{a.facility_id}</h3>
          <div className="card-sub">{a.asset_id} · since {(a.live ? hhmmss : hhmm)(a.onset_unix)} UTC</div>
        </div>
        <button className="btn btn-small" onClick={() => onSelect(a.facility_id, a.event_id)}>Details</button>
      </header>

      {tripped ? (
        <div className="countdown countdown-tripped">
          <div className="countdown-label">Tank at shutdown · wells shut in</div>
          <div className="countdown-value">SHUT IN</div>
          {repairEnd && <div className="countdown-sub">Wells restart when the repair finishes at {hhmm(repairEnd)} UTC</div>}
        </div>
      ) : (
        <div className="countdown">
          <div className="countdown-label">Time to shutdown ({shutdown}%)</div>
          <div className="countdown-value" aria-live="off">{clock(remaining)}</div>
          {deadline && h && (
            <div className="countdown-sub">
              at {hhmm(deadline)} UTC
              {h.p10 != null && h.p90 != null && plan &&
                ` · range ${hhmm(plan.plan_start_unix + h.p10 * 3600)}–${hhmm(plan.plan_start_unix + h.p90 * 3600)}`}
              {" · "}{hoursText(h.p50)} from data time
            </div>
          )}
        </div>
      )}

      <TankBar level={a.level_pct} warning={warning} shutdown={shutdown} />

      {plan && (
        <div className="plan">
          <div className="plan-head">
            <span className="plan-title">{tripped ? "Lost production until repair" : "Recommended plan"}</span>
            <span className="plan-cost">{money(plan.est_cost)}</span>
          </div>
          <ul className="actions">
            {tripped ? (
              <li className="action action-none">Nothing to dispatch: trucks or shut-ins can't restart the wells early</li>
            ) : plan.kind === "accept_trip" ? (
              <li className="action action-none">Accept the trip: nothing cheaper prevents it</li>
            ) : plan.recommended_actions.map((x, i) => (
              <li key={i} className={`action action-${x.type}`}>{actionText(x, plan.plan_start_unix)}</li>
            ))}
          </ul>
          {!tripped && <CostCompare plan={plan.est_cost} nothing={plan.nothing_cost} tripped={tripped} />}
          {saving > 0.5 && <div className="saving">Saves {money(saving)} vs doing nothing</div>}
          <details className="why">
            <summary>Why this plan</summary>
            <p>{plan.explanation}</p>
            {plan.alternatives.length > 0 && (
              <p className="muted">Other options considered: {plan.alternatives.join("; ")}</p>
            )}
          </details>
        </div>
      )}
      {a.lost_revenue_per_hour != null && (
        <div className="card-foot">
          {tripped ? "Losing" : "Lost production if it trips:"} {money(a.lost_revenue_per_hour)}/h{tripped ? " while shut in" : ""}
        </div>
      )}
    </article>
  );
}

function CostCompare({ plan, nothing, tripped }: { plan: number; nothing: number; tripped: boolean }) {
  const max = Math.max(plan, nothing, 1);
  return (
    <div className="costs" aria-label="Cost comparison">
      <div className="cost-row">
        <span className="cost-name">{tripped ? "Lost production" : "This plan"}</span>
        <span className="cost-bar"><span className="cost-fill cost-plan" style={{ width: `${(plan / max) * 100}%` }} /></span>
        <span className="cost-val">{money(plan)}</span>
      </div>
      {!tripped && (
        <div className="cost-row">
          <span className="cost-name">Do nothing</span>
          <span className="cost-bar"><span className="cost-fill cost-nothing" style={{ width: `${(nothing / max) * 100}%` }} /></span>
          <span className="cost-val">{money(nothing)}</span>
        </div>
      )}
    </div>
  );
}

function actionText(x: Action, start: number): string {
  switch (x.type) {
    case "dispatch_truck":
      return x.trucks === 1
        ? `Dispatch 1 truck now, ${x.trips_per_truck} trip${x.trips_per_truck === 1 ? "" : "s"}`
        : `Dispatch ${x.trucks} trucks now, ${x.trips_per_truck} trip${x.trips_per_truck === 1 ? "" : "s"} each`;
    case "shut_in_wells": {
      const m = x.start_after_min ?? 0;
      const when = m === 0 ? "now" : `at ${hhmm(start + m * 60)} UTC (+${m} min)`;
      return `Shut in ${x.wells?.join(", ")} ${when}`;
    }
    case "field_inspection":
      return `Field inspection of ${x.asset_id}`;
    default:
      return "No action needed";
  }
}

function InspectionCard({ a, nowUnix, selected, onSelect }: {
  a: EventRecord; nowUnix: number; selected: boolean; onSelect: Props["onSelect"];
}) {
  const leak = a.category === "pipeline_leak";
  const sev = leak ? "urgent" : "warning";
  const age = (nowUnix - a.onset_unix) / 3600;
  const conf = Math.round(a.confidence * 100);
  return (
    <article className={`card card-${sev} ${selected ? "card-selected" : ""}`}>
      <header className="card-head">
        <span className="order">{a.order}</span>
        <div className="card-title">
          <div className="card-kicker">
            <span className={`sev-pill sev-${sev}`}>{leak ? "Urgent" : "Watch"}</span>
            {CATEGORY_LABEL[a.category]}
            {a.unverified && <span className="tag tag-unverified" title={a.evidence[a.evidence.length - 1]}>Unverified</span>}
          </div>
          <h3>{a.asset_id}</h3>
          <div className="card-sub">{a.facility_id} · since {dayTime(a.onset_unix)} UTC ({hoursText(age)})</div>
          {a.detected_unix != null && (
            <div className="card-sub detect-lag"
              title="Time from the estimated onset to the first reading that tripped the detector">
              Detected {hoursText(Math.max(0, a.detected_unix - a.onset_unix) / 3600)} after onset
              {" "}(flagged {hhmm(a.detected_unix)} UTC)
            </div>
          )}
        </div>
        <button className="btn btn-small" onClick={() => onSelect(a.facility_id, a.event_id)}>Details</button>
      </header>
      <div className="confidence">
        <span>Confidence</span>
        <span className="conf-bar"><span className={`conf-fill sev-${sev}-bg`} style={{ width: `${conf}%` }} /></span>
        <span className="conf-val">{conf}%</span>
      </div>
      {a.unverified && (
        <div className="unverified-note">
          The pump has not run since this was last seen, so a repair would not show yet. It will be
          re-checked when the pump next starts.
        </div>
      )}
      <ul className="evidence">
        {a.evidence.slice(0, leak ? 3 : 2).map((e, i) => <li key={i}>{e}</li>)}
      </ul>
      <ul className="actions">
        {a.recommended_actions.map((x, i) => <li key={i} className={`action action-${x.type}`}>{actionText(x, nowUnix)}</li>)}
      </ul>
    </article>
  );
}

function LiveBadge() {
  return <span className="live-badge" title="Streaming from the hardware twin"><i />Live</span>;
}

const PUMP_STATE: Record<number, string> = { 0: "stopped", 1: "running", 2: "FAULT" };

/** Always-on strip for the hardware twin: online state and its latest reading. */
function TwinStrip({ twin, onSelect }: { twin: TwinStatus; onSelect: Props["onSelect"] }) {
  const l = twin.last;
  return (
    <div className={`twin-strip ${twin.online ? "twin-online" : "twin-offline"}`}>
      <div className="twin-head">
        {twin.online ? <LiveBadge /> : <span className="tag tag-unverified">Offline</span>}
        <strong>Hardware twin</strong>
        <span className="muted small">{twin.facility_id}</span>
        {l && <button className="btn btn-small" onClick={() => onSelect(twin.facility_id)}>Details</button>}
      </div>
      {l ? (
        <div className="twin-readings">
          <span>Tank <strong>{l.level_pct.toFixed(1)}%</strong></span>
          <span>Pump <strong className={l.pump_status === 2 ? "sev-critical-text" : ""}>{PUMP_STATE[l.pump_status] ?? l.pump_status}</strong></span>
          <span>Vibration <strong>{l.vibration.toFixed(3)} g</strong></span>
          <span className="muted">{twin.online ? `${(twin.age_s ?? 0).toFixed(0)} s ago` : `last ${hhmmss(l.ts)} UTC`}</span>
        </div>
      ) : (
        <div className="muted small">Waiting for the ESP32 (or <code>python scripts/fake_twin.py</code>) to post to /twin/ingest.</div>
      )}
    </div>
  );
}
