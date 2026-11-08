interface Props {
  level: number | null;
  warning: number;
  shutdown: number;
  compact?: boolean;
}

/** Horizontal tank gauge with the warning and shutdown limits marked. */
export function TankBar({ level, warning, shutdown, compact }: Props) {
  const v = level ?? 0;
  const zone = v >= shutdown ? "shutdown" : v >= warning ? "warning" : "normal";
  return (
    <div className={`tankbar ${compact ? "tankbar-compact" : ""}`}
      role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(v)}
      aria-label={`Tank level ${v.toFixed(1)}%`}>
      <div className="tankbar-track">
        <div className={`tankbar-fill tankbar-${zone}`} style={{ width: `${Math.min(v, 100)}%` }} />
        <div className="tankbar-mark tankbar-mark-warning" style={{ left: `${warning}%` }} title={`Warning ${warning}%`} />
        <div className="tankbar-mark tankbar-mark-shutdown" style={{ left: `${shutdown}%` }} title={`Shutdown ${shutdown}%`} />
      </div>
      {!compact && (
        <div className="tankbar-labels">
          <span className="tankbar-value">{level === null ? "-" : `${v.toFixed(1)}%`}</span>
          <span className="tankbar-limits">warn {warning}% · shutdown {shutdown}%</span>
        </div>
      )}
    </div>
  );
}
