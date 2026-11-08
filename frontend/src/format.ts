// All times are UTC: the network's timezone is UTC and "now" is the data's last timestamp.

const pad = (n: number) => String(n).padStart(2, "0");
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function hhmm(unix: number): string {
  const d = new Date(unix * 1000);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

export function dayTime(unix: number): string {
  const d = new Date(unix * 1000);
  return `${DAYS[d.getUTCDay()]} ${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${hhmm(unix)}`;
}

export function fullTime(unix: number): string {
  const d = new Date(unix * 1000);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${hhmm(unix)} UTC`;
}

export function money(x: number | null | undefined): string {
  if (x === null || x === undefined) return "-";
  return `$${Math.round(x).toLocaleString("en-US")}`;
}

/** Seconds -> "1:24:05" (or "0:00:00" when past). */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 3600)}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
}

export function hoursText(h: number | null | undefined): string {
  if (h === null || h === undefined) return "-";
  const m = Math.round(h * 60);
  return m >= 60 ? `${Math.floor(m / 60)} h ${pad(m % 60)} min` : `${m} min`;
}

export const CATEGORY_LABEL: Record<string, string> = {
  pump_failure: "Pump failure",
  pipeline_leak: "Pipeline leak",
  pump_degradation: "Worn pump",
  ok: "Normal",
  other: "Other",
};

export function pct(x: number | null | undefined, digits = 1): string {
  return x === null || x === undefined ? "-" : `${x.toFixed(digits)}%`;
}
