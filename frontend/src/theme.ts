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

const SEV_LIGHT = { critical: "#d13438", high: "#e8590c", urgent: "#8e4ec6", warning: "#b88700", ok: "#2b9a66" };
const SEV_DARK = { critical: "#ff6369", high: "#ff8b3e", urgent: "#bf7af0", warning: "#f5c542", ok: "#3dd68c" };
// Esri grey canvas basemaps: keyless, low-contrast, so status colours stand out.
const ESRI = "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors";

export const PALETTES: Record<ThemeName, Palette> = {
  light: {
    text: "#1c2024", muted: "#60646c", grid: "#e0e1e6", surface: "#ffffff", accent: "#0b68cb",
    series1: "#0b68cb", series2: "#2b9a66", series3: "#8e4ec6",
    warning: "#b88700", shutdown: "#d13438", plan: "#0b68cb", nothing: "#d13438", onset: "#e8590c",
    sev: SEV_LIGHT,
    tiles: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
    tileAttribution: ESRI,
  },
  dark: {
    text: "#edeef0", muted: "#9ba1a6", grid: "#2b3138", surface: "#151a20", accent: "#4ca8ff",
    series1: "#4ca8ff", series2: "#3dd68c", series3: "#bf7af0",
    warning: "#f5c542", shutdown: "#ff6369", plan: "#4ca8ff", nothing: "#ff6369", onset: "#ff8b3e",
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
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", PALETTES[theme].surface);
    try {
      localStorage.setItem("bts-theme", theme);
    } catch {
      /* ignore */
    }
  }, [theme]);
  return [theme, () => setTheme((t) => (t === "dark" ? "light" : "dark"))];
}
