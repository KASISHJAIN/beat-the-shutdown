// Shapes returned by backend/api.py

export type Severity = "critical" | "high" | "urgent" | "warning" | "ok";
export type Category = "pump_failure" | "pipeline_leak" | "pump_degradation" | "other";

export interface Dataset {
  id: string;
  label: string;
  kind: "test" | "full_week" | "mock_test" | "snapshot" | "other";
}

export interface TimeInfo {
  start_unix: number;
  end_unix: number;
  now_unix: number;
  step_s: number;
}

export interface Well {
  id: string;
  baseline_water_bpd: number;
  oil_bpd: number;
  gas_mcfd: number;
  revenue_per_hour: number;
  status: number | null;
}

export interface Facility {
  id: string;
  lat: number;
  lon: number;
  capacity_bbl: number;
  tank_id: string;
  pump_id: string;
  pipeline_id: string;
  max_flow_bpd: number;
  start_level_pct: number;
  stop_level_pct: number;
  level_pct: number | null;
  pump_status: number | null;
  status: "pump_failure" | "pipeline_leak" | "pump_degradation" | "ok";
  severity: Severity;
  alert_ids: string[];
  wells: Well[];
}

export interface System {
  id: string;
  center: [number, number];
  truck_yard: [number, number];
  fleet_size: number;
  disposal: { id: string; lat: number; lon: number; pressure_psi: number };
  facilities: Facility[];
}

export interface Network extends TimeInfo {
  dataset: string;
  warning_pct: number;
  shutdown_pct: number;
  systems: System[];
}

export interface Action {
  type: "dispatch_truck" | "shut_in_wells" | "field_inspection" | "none";
  facility_id?: string;
  trucks?: number;
  trips_per_truck?: number;
  wells?: string[];
  start_after_min?: number;
  asset_id?: string;
}

export interface Plan {
  event_id: string;
  facility_id: string;
  kind: "none" | "trucks" | "shut_in" | "mix" | "accept_trip" | "tripped";
  summary: string;
  est_cost: number;
  truck_cost: number;
  shutin_cost: number;
  trip_cost: number;
  trucks: number;
  trips_per_truck: number;
  shutins: { well: string; start_after_min: number }[];
  trips_at_unix: number | null;
  peak_pct: number;
  trucks_free: number;
  nothing_cost: number;
  plan_start_unix: number;
  horizon_min: number;
  recommended_actions: Action[];
  explanation: string;
  alternatives: string[];
}

export interface HoursToLimit {
  p10: number | null;
  p50: number | null;
  p90: number | null;
}

export interface EventRecord {
  event_id: string;
  system_id: string;
  facility_id: string;
  asset_id: string;
  category: Category;
  status: "ongoing" | "resolved";
  onset_ts: string;
  onset_unix: number;
  end_ts: string | null;
  end_unix: number | null;
  confidence: number;
  evidence: string[];
  level_pct: number | null;
  hours_to_limit: HoursToLimit | null;
  recommended_actions: Action[];
  lost_revenue_per_hour?: number;
  est_cost?: number;
  plan?: Plan;
  unverified?: boolean;
  order?: number;
}

export interface AlertsResponse extends TimeInfo {
  dataset: string;
  alerts: EventRecord[];
}

export interface EventsResponse extends TimeInfo {
  dataset: string;
  events: EventRecord[];
}

export interface SeriesResponse {
  asset: string;
  now_unix: number;
  series: Record<string, { t: number[]; v: (number | null)[] }>;
}

export interface WhatIfPoint {
  t: number;
  level: number;
}

export interface WhatIf {
  event_id: string;
  facility_id: string;
  start_unix: number;
  repair_end_unix: number;
  warning_pct: number;
  shutdown_pct: number;
  plan: WhatIfPoint[];
  nothing: WhatIfPoint[];
  truck_arrivals_unix: number[];
  shutins: { well: string; start_unix: number }[];
  plan_cost: number;
  nothing_cost: number;
}

export interface PlanResponse {
  event_id: string;
  status?: string;
  plan: Plan | null;
  whatif: WhatIf | null;
}

export interface TwinStatus {
  dataset: string;
  configured: boolean;
  online: boolean;
  samples: number;
  facility_id: string;
  setpoints: { start_level_pct: number; stop_level_pct: number; warning_pct: number; shutdown_pct: number };
  now_unix: number | null;
  age_s?: number;
  last: { ts: number; level_pct: number; pump_status: number; vibration: number } | null;
  system: System | null;
  warning_pct?: number;
  shutdown_pct?: number;
  alerts: EventRecord[];
}

/** An alert plus where its clock comes from (the dataset's "now" or the live twin's). */
export interface ClockedAlert extends EventRecord {
  live?: boolean;
  clockNow: number;
  clockLoadedAt: number;
}
