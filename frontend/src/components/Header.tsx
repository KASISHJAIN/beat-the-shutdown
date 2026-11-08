import type { Dataset, TimeInfo } from "../types";
import { dayTime, fullTime } from "../format";
import type { ThemeName } from "../theme";

const MIN_HISTORY_S = 2 * 3600;

interface Props {
  datasets: Dataset[];
  dataset: string;
  onDataset: (id: string) => void;
  onRefreshDatasets: () => void; // re-scan folders (e.g. a freshly pulled test/) when the picker opens
  time: TimeInfo | null;
  pending: number | null; // slider position while dragging / before data arrives
  onScrub: (t: number) => void; // live while dragging
  onCommit: (t: number) => void; // on release / button
  playing: boolean;
  onTogglePlay: () => void;
  loading: boolean;
  theme: ThemeName;
  onToggleTheme: () => void;
  onHandoff: () => void;
}

export function Header(p: Props) {
  const t = p.time;
  const lo = t ? Math.min(t.start_unix + MIN_HISTORY_S, t.end_unix) : 0;
  const shown = p.pending ?? t?.now_unix ?? 0;
  const atEnd = t ? shown >= t.end_unix : true;
  const step = (d: number) => {
    if (!t) return;
    const v = Math.min(Math.max(shown + d, lo), t.end_unix);
    p.onCommit(v);
  };
  return (
    <header className="header">
      <div className="brand">
        <svg viewBox="0 0 32 32" width="22" height="22" aria-hidden>
          <rect x="7" y="4" width="18" height="24" rx="3" fill="none" stroke="currentColor" strokeWidth="3" />
          <rect x="10" y="14" width="12" height="11" rx="1" fill="currentColor" />
        </svg>
        <span className="brand-name">Beat the Shutdown</span>
      </div>

      <label className="field">
        <span className="field-label">Dataset</span>
        <select value={p.dataset} onChange={(e) => p.onDataset(e.target.value)} aria-label="Dataset"
          onFocus={p.onRefreshDatasets} onMouseDown={p.onRefreshDatasets}>
          {p.datasets.map((d) => (
            <option key={d.id} value={d.id}>
              {d.label}
            </option>
          ))}
        </select>
      </label>

      <div className="now" title={t ? fullTime(shown) : ""}>
        <span className="field-label">Now (UTC){!atEnd && <span className="tag tag-travel">time travel</span>}</span>
        <span className="now-value">
          {t ? dayTime(shown) : "-"}
          {p.loading && <span className="spinner" aria-label="loading" />}
        </span>
      </div>

      <div className="timeline">
        <div className="timeline-buttons">
          <button className="btn" onClick={() => step(-3600)} disabled={!t} title="Back 1 hour">−1h</button>
          <button className="btn" onClick={() => step(-300)} disabled={!t} title="Back 5 minutes">−5m</button>
          <button className="btn btn-primary" onClick={p.onTogglePlay} disabled={!t || (atEnd && !p.playing)}
            title={p.playing ? "Pause replay" : "Replay forward from here"}>
            {p.playing ? "Pause" : "Play"}
          </button>
          <button className="btn" onClick={() => step(300)} disabled={!t || atEnd} title="Forward 5 minutes">+5m</button>
          <button className="btn" onClick={() => step(3600)} disabled={!t || atEnd} title="Forward 1 hour">+1h</button>
          <button className="btn" onClick={() => t && p.onCommit(t.end_unix)} disabled={!t || atEnd}
            title="Jump to the dataset's latest reading">Latest</button>
        </div>
        <input
          className="slider"
          type="range"
          aria-label="Time travel"
          min={lo}
          max={t?.end_unix ?? 1}
          step={t?.step_s ?? 300}
          value={shown}
          disabled={!t}
          onChange={(e) => p.onScrub(Number(e.target.value))}
          onPointerUp={(e) => p.onCommit(Number((e.target as HTMLInputElement).value))}
          onKeyUp={(e) => p.onCommit(Number((e.target as HTMLInputElement).value))}
        />
        <div className="timeline-range">
          <span>{t ? dayTime(t.start_unix) : ""}</span>
          <span>{t ? dayTime(t.end_unix) : ""}</span>
        </div>
      </div>

      <button className="btn btn-primary handoff-btn" onClick={p.onHandoff} disabled={!t}
        title="Write the shift handoff for this moment">Shift handoff</button>
      <button className="btn theme-toggle" onClick={p.onToggleTheme} aria-label="Toggle dark theme"
        title={p.theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}>
        {p.theme === "dark" ? "Light" : "Dark"}
      </button>
    </header>
  );
}
