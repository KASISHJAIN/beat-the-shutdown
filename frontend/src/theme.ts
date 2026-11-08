import { useEffect, useState } from "react";

export type ThemeName = "light" | "dark";

/** Chart / map colours per theme (CSS variables cover everything else). */
export interface Palette {
  text: string;
  muted: string;
  grid: string;
  surface: string;
  accent: string;
  series1: string;
  series2: string;
  series3: string;
  warning: string;
  shutdown: string;
  plan: string;
  nothing: string;
  onset: string;
  sev: Record<string, string>;
  tiles: string;
  tileAttribution: string;
}

const SEV_LIGHT = { critical: "#c4261b", high: "#b54708", urgent: "#6b3fa0", warning: "#8a6100", ok: "#4f6f5f" };
const SEV_DARK = { critical: "#ff6b5e", high: "#f59e5b", urgent: "#b794f6", warning: "#e8b931", ok: "#8db5a0" };
// Esri grey canvas basemaps: keyless, low-contrast, so status colours stand out.
const ESRI = "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors";

export const PALETTES: Record<ThemeName, Palette> = {
  light: {
    text: "#1f1d1d", muted: "#615d5d", grid: "#dcdada", surface: "#ffffff", accent: "#2f5d8a",
    series1: "#2f5d8a", series2: "#4f6f5f", series3: "#6b3fa0",
    warning: "#8a6100", shutdown: "#c4261b", plan: "#2f5d8a", nothing: "#c4261b", onset: "#b54708",
    sev: SEV_LIGHT,
    tiles: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
    tileAttribution: ESRI,
  },
  dark: {
    text: "#f1f0f0", muted: "#ada8a8", grid: "#383535", surface: "#1c1b1b", accent: "#7fa7d4",
    series1: "#7fa7d4", series2: "#8db5a0", series3: "#b794f6",
    warning: "#e8b931", shutdown: "#ff6b5e", plan: "#7fa7d4", nothing: "#ff6b5e", onset: "#f59e5b",
    sev: SEV_DARK,
    tiles: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
    tileAttribution: ESRI,
  },
};

function initialTheme(): ThemeName {
  const forced = new URLSearchParams(window.location.search).get("theme");
  if (forced === "light" || forced === "dark") return forced;
  try {
    const saved = localStorage.getItem("bts-theme");
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    /* storage unavailable */
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function useTheme(): [ThemeName, () => void] {
  const [theme, setTheme] = useState<ThemeName>(initialTheme);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#161515" : "#373535");
    try {
      localStorage.setItem("bts-theme", theme);
    } catch {
      /* ignore */
    }
  }, [theme]);
  return [theme, () => setTheme((t) => (t === "dark" ? "light" : "dark"))];
}
