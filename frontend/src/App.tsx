import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import type { ClockedAlert, Dataset, EventRecord, Network, TwinStatus } from "./types";
import { PALETTES, useTheme } from "./theme";
import { Header } from "./components/Header";
import { AlertsPanel } from "./components/AlertsPanel";
import { MapView } from "./components/MapView";
import { FacilityDetail } from "./components/FacilityDetail";
import { HandoffPanel } from "./components/HandoffPanel";

type Tab = "alerts" | "map" | "detail";
const PLAY_STEP_S = 15 * 60;   // replay speed: 15 data-minutes per tick
const PLAY_TICK_MS = 1200;
const TWIN_POLL_MS = 2000;

/** SPEC order across both sources: pump failures by hours_to_limit (ties: lost revenue/h),
 *  then leaks and worn pumps. Renumbers `order`. */
function mergeAlerts(a: ClockedAlert[]): ClockedAlert[] {
  const key = (r: ClockedAlert): [number, number, number] => {
    if (r.category !== "pump_failure") return [1, 0, r.onset_unix];
    return [0, r.hours_to_limit?.p50 ?? Infinity, -(r.lost_revenue_per_hour ?? 0)];
  };
  return [...a]
    .sort((x, y) => {
      const kx = key(x), ky = key(y);
      return kx[0] - ky[0] || kx[1] - ky[1] || kx[2] - ky[2];
    })
    .map((r, i) => ({ ...r, order: i + 1 }));
}

// Deep links: ?dataset=history/full_week&now=1791460800&facility=S2_FACILITY_A
const URL_PARAMS = new URLSearchParams(window.location.search);

function savedDataset(): string | null {
  try {
    return URL_PARAMS.get("dataset") ?? localStorage.getItem("bts-dataset");
  } catch {
    return URL_PARAMS.get("dataset");
  }
}

function updateUrl(params: Record<string, string | null>) {
  try {
    const u = new URL(window.location.href);
    for (const [k, v] of Object.entries(params)) {
      if (v === null) u.searchParams.delete(k);
      else u.searchParams.set(k, v);
    }
    window.history.replaceState(null, "", u);
  } catch {
    /* ignore */
  }
}

export default function App() {
  const [theme, toggleTheme] = useTheme();
  const palette = PALETTES[theme];
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [dataset, setDataset] = useState<string>("");
  const [now, setNow] = useState<number | null>(() => Number(URL_PARAMS.get("now")) || null); // null = latest
  const [pending, setPending] = useState<number | null>(null);
  const [network, setNetwork] = useState<Network | null>(null);
  const [alerts, setAlerts] = useState<EventRecord[]>([]);
  const [loadedAt, setLoadedAt] = useState(Date.now());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(URL_PARAMS.get("facility"));
  const [focusEvent, setFocusEvent] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>(() => {
    const t = URL_PARAMS.get("tab");
    if (t === "alerts" || t === "map" || t === "detail") return t;
    return URL_PARAMS.get("facility") ? "detail" : "alerts";
  });
  const [playing, setPlaying] = useState(false);
  const [twin, setTwin] = useState<TwinStatus | null>(null);
  const [twinLoadedAt, setTwinLoadedAt] = useState(Date.now());
  const [twinEnabled, setTwinEnabled] = useState(false);
  const [handoffOpen, setHandoffOpen] = useState(false);
  const req = useRef(0);

  useEffect(() => {
    api.datasets().then((r) => {
      setDatasets(r.datasets);
      setTwinEnabled(Boolean(r.twin?.configured));
      const want = savedDataset();
      const pick = r.datasets.find((d) => d.id === want) ?? r.datasets[0];
      if (pick) setDataset(pick.id);
      else setError("No datasets found. Put the challenge folder next to the backend (see README).");
    }).catch((e) => setError(`API unreachable (${e.message}). Is the backend running on :8000?`));
  }, []);

  useEffect(() => {
    if (!dataset) return;
    const id = ++req.current;
    setLoading(true);
    Promise.all([api.network(dataset, now), api.alerts(dataset, now)])
      .then(([n, a]) => {
        if (id !== req.current) return; // a newer request superseded this one
        setNetwork(n);
        setAlerts(a.alerts);
        setLoadedAt(Date.now());
        setError(null);
        setPending(null);
      })
      .catch((e) => id === req.current && setError(String(e.message ?? e)))
      .finally(() => id === req.current && setLoading(false));
  }, [dataset, now]);

  // Live twin: poll its status; it runs on its own clock, independent of time travel.
  useEffect(() => {
    if (!twinEnabled) return;
    let stop = false;
    const tick = () => api.twin()
      .then((t) => { if (!stop) { setTwin(t); setTwinLoadedAt(Date.now()); } })
      .catch(() => { /* backend restarting: keep the last state */ });
    tick();
    const id = window.setInterval(tick, TWIN_POLL_MS);
    return () => { stop = true; window.clearInterval(id); };
  }, [twinEnabled]);

  // Replay: step "now" forward while playing, waiting for each step to load.
  useEffect(() => {
    if (!playing || loading || !network) return;
    if (network.now_unix >= network.end_unix) {
      setPlaying(false);
      return;
    }
    const id = window.setTimeout(() => setNow(Math.min(network.now_unix + PLAY_STEP_S, network.end_unix)), PLAY_TICK_MS);
    return () => window.clearTimeout(id);
  }, [playing, loading, network]);

  const chooseDataset = useCallback((id: string) => {
    setDataset(id);
    setNow(null);
    setPending(null);
    setSelected(null);
    setPlaying(false);
    setNetwork(null);
    setAlerts([]);
    try {
      localStorage.setItem("bts-dataset", id);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    if (dataset) updateUrl({ dataset, now: now === null ? null : String(now), facility: selected });
  }, [dataset, now, selected]);

  const commit = useCallback((t: number) => {
    setPending(t);
    setNow(network && t >= network.end_unix ? null : t);
  }, [network]);

  const select = useCallback((facilityId: string, eventId?: string) => {
    setSelected(facilityId);
    setFocusEvent(eventId ?? null);
    setTab("detail");
  }, []);

  const dataNow = network?.now_unix ?? 0;
  const twinSystem = twin?.system ?? null;
  const isTwin = Boolean(twinSystem && selected === twin?.facility_id);
  const facility = (isTwin ? twinSystem!.facilities : network?.systems.flatMap((s) => s.facilities) ?? [])
    .find((f) => f.id === selected) ?? null;

  const allAlerts = useMemo(() => mergeAlerts([
    ...alerts.map((a) => ({ ...a, clockNow: dataNow, clockLoadedAt: loadedAt })),
    ...(twin?.alerts ?? []).map((a) => ({ ...a, live: true, clockNow: twin!.now_unix ?? 0, clockLoadedAt: twinLoadedAt })),
  ]), [alerts, dataNow, loadedAt, twin, twinLoadedAt]);

  // The map shows the dataset's systems plus the live twin's.
  const mapNetwork = useMemo(() => (network && twinSystem
    ? { ...network, systems: [...network.systems, twinSystem] } : network), [network, twinSystem]);
  const liveFacilities = useMemo(() => new Set(twinSystem && twin?.online ? twinSystem.facilities.map((f) => f.id) : []),
    [twinSystem, twin?.online]);

  return (
    <div className="app">
      <Header datasets={datasets} dataset={dataset} onDataset={chooseDataset} time={network}
        pending={pending} onScrub={setPending} onCommit={commit} playing={playing}
        onTogglePlay={() => setPlaying((x) => !x)} loading={loading} theme={theme} onToggleTheme={toggleTheme}
        onHandoff={() => { setPlaying(false); setHandoffOpen(true); }} />
      {handoffOpen && dataset && (
        <HandoffPanel dataset={dataset} now={network?.now_unix ?? now} onClose={() => setHandoffOpen(false)} />
      )}
      <main className={`layout ${facility ? "with-detail" : ""}`} data-tab={tab}>
        <div className="col col-alerts">
          <AlertsPanel alerts={allAlerts} network={network} nowUnix={dataNow} twin={twin}
            selectedFacility={selected} onSelect={select} error={error} />
        </div>
        <div className="col col-map">
          <MapView network={mapNetwork} palette={palette} selected={selected} live={liveFacilities}
            onSelect={(id) => select(id)} layoutKey={`${tab}-${facility ? 1 : 0}`} />
        </div>
        {facility && network && (
          <div className="col col-detail">
            <FacilityDetail key={isTwin ? "twin" : dataset}
              dataset={isTwin ? twin!.dataset : dataset}
              now={isTwin ? twin!.now_unix ?? 0 : dataNow}
              limits={isTwin ? twin!.setpoints : network}
              live={isTwin} facility={facility}
              focusEventId={focusEvent} palette={palette}
              onClose={() => { setSelected(null); setTab("map"); }} />
          </div>
        )}
        {!facility && (
          <div className="col col-detail col-detail-empty">
            <p className="muted">Pick a facility on the map or an alert's <strong>Details</strong> to see its trends and what-if replay.</p>
          </div>
        )}
      </main>
      <nav className="tabs" aria-label="Views">
        {(["alerts", "map", "detail"] as Tab[]).map((t) => (
          <button key={t} className={`tab ${tab === t ? "tab-on" : ""}`} onClick={() => setTab(t)}>
            {t === "alerts" ? `Alerts${allAlerts.length ? ` (${allAlerts.length})` : ""}` : t === "map" ? "Map" : "Detail"}
          </button>
        ))}
      </nav>
    </div>
  );
}
