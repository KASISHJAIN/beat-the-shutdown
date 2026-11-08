import type {
  AlertsResponse,
  Dataset,
  EventsResponse,
  Network,
  PlanResponse,
  HandoffResponse,
  SeriesResponse,
  TwinStatus,
} from "./types";

const BASE = (import.meta as { env?: Record<string, string> }).env?.VITE_API_BASE ?? "";

async function get<T>(path: string, params: Record<string, string | number | undefined | null>,
  method: "GET" | "POST" = "GET"): Promise<T> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  }
  const res = await fetch(`${BASE}${path}?${q.toString()}`, { method });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not json */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  datasets: () => get<{ datasets: Dataset[]; twin?: { id: string; configured: boolean } }>("/api/datasets", {}),
  network: (dataset: string, now?: number | null) => get<Network>("/api/network", { dataset, now }),
  alerts: (dataset: string, now?: number | null) => get<AlertsResponse>("/api/alerts", { dataset, now }),
  events: (dataset: string, now?: number | null, facility_id?: string) =>
    get<EventsResponse>("/api/events", { dataset, now, facility_id }),
  series: (dataset: string, asset: string, metrics: string[], now?: number | null, hours = 48) =>
    get<SeriesResponse>("/api/series", { dataset, asset, metric: metrics.join(","), now, hours }),
  plan: (dataset: string, event_id: string, now?: number | null) =>
    get<PlanResponse>("/api/plan", { dataset, event_id, now }),
  twin: () => get<TwinStatus>("/api/twin", {}),
  handoff: (dataset: string, now?: number | null) => get<HandoffResponse>("/api/handoff", { dataset, now }, "POST"),
};
