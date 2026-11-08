import { useEffect, useMemo } from "react";
import L from "leaflet";
import { CircleMarker, MapContainer, Marker, Polyline, TileLayer, Tooltip, useMap } from "react-leaflet";
import type { Facility, Network } from "../types";
import type { Palette } from "../theme";
import { CATEGORY_LABEL, pct } from "../format";

interface Props {
  network: Network | null;
  palette: Palette;
  selected: string | null;
  onSelect: (facilityId: string) => void;
  layoutKey: string; // changes when the container is resized/shown, so Leaflet re-measures
}

const SEV_RANK: Record<string, number> = { critical: 0, high: 1, urgent: 2, warning: 3, ok: 4 };

function FitBounds({ network, dataset, layoutKey, selected }: {
  network: Network | null; dataset: string; layoutKey: string; selected: [number, number] | null;
}) {
  const map = useMap();
  useEffect(() => {
    if (!network) return;
    map.invalidateSize();
    const pts: [number, number][] = [];
    for (const s of network.systems) {
      pts.push([s.disposal.lat, s.disposal.lon], s.truck_yard);
      for (const f of s.facilities) pts.push([f.lat, f.lon]);
    }
    if (pts.length) map.fitBounds(L.latLngBounds(pts), { padding: [24, 24] });
    if (selected) map.panInside(selected, { padding: [40, 40] });
    // refit when the dataset or the layout changes, not on every time-travel step
  }, [dataset, layoutKey, map, network === null]); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

function Resize({ k }: { k: string }) {
  const map = useMap();
  useEffect(() => {
    const id = window.setTimeout(() => map.invalidateSize(), 60);
    return () => window.clearTimeout(id);
  }, [k, map]);
  return null;
}


function shapeIcon(kind: "disposal" | "yard", color: string): L.DivIcon {
  const shape = kind === "disposal"
    ? `<svg width="18" height="18" viewBox="0 0 18 18"><rect x="4" y="4" width="10" height="10" transform="rotate(45 9 9)" fill="${color}" stroke="white" stroke-width="1.5"/></svg>`
    : `<svg width="18" height="18" viewBox="0 0 18 18"><rect x="2.5" y="2.5" width="13" height="13" rx="2" fill="none" stroke="${color}" stroke-width="2.5"/><text x="9" y="13" text-anchor="middle" font-size="9" font-weight="700" fill="${color}" font-family="system-ui, sans-serif">T</text></svg>`;
  return L.divIcon({ className: "map-icon", html: shape, iconSize: [18, 18], iconAnchor: [9, 9], tooltipAnchor: [8, 0] });
}

export function MapView({ network, palette, selected, onSelect, layoutKey }: Props) {
  const center: [number, number] = network?.systems[0]?.center ?? [31.8, -102.3];
  const facs = useMemo(() => {
    const out: (Facility & { system: string; disposal: [number, number] })[] = [];
    network?.systems.forEach((s) =>
      s.facilities.forEach((f) => out.push({ ...f, system: s.id, disposal: [s.disposal.lat, s.disposal.lon] })));
    // draw healthy first so problems sit on top
    return out.sort((a, b) => SEV_RANK[b.severity] - SEV_RANK[a.severity]);
  }, [network]);
  const selFac = facs.find((f) => f.id === selected) ?? null;
  const disposalIcon = useMemo(() => shapeIcon("disposal", palette.muted), [palette]);
  const yardIcon = useMemo(() => shapeIcon("yard", palette.accent), [palette]);

  return (
    <section className="panel map-panel" aria-label="Map">
      <MapContainer center={center} zoom={9} className="map" zoomControl={true} attributionControl={true}>
        <TileLayer key={palette.tiles} url={palette.tiles} attribution={palette.tileAttribution} />
        <FitBounds network={network} dataset={network?.dataset ?? ""} layoutKey={layoutKey}
          selected={selFac ? [selFac.lat, selFac.lon] : null} />
        <Resize k={layoutKey} />
        {facs.map((f) => {
          const leak = f.status === "pipeline_leak";
          return (
            <Polyline key={`p-${f.id}`} positions={[[f.lat, f.lon], f.disposal]}
              pathOptions={{
                color: leak ? palette.sev.urgent : palette.muted,
                weight: leak ? 4 : 2, opacity: leak ? 0.95 : 0.45, dashArray: leak ? "6 6" : undefined,
              }}>
              <Tooltip sticky>{f.pipeline_id}{leak ? " · leak suspected" : ""}</Tooltip>
            </Polyline>
          );
        })}
        {network?.systems.map((s) => (
          <Marker key={`d-${s.id}`} position={[s.disposal.lat, s.disposal.lon]} icon={disposalIcon}>
            <Tooltip>{s.disposal.id} · disposal · {s.disposal.pressure_psi} psi</Tooltip>
          </Marker>
        ))}
        {network?.systems.map((s) => (
          <Marker key={`y-${s.id}`} position={s.truck_yard} icon={yardIcon}>
            <Tooltip>{s.id} truck yard · {s.fleet_size} truck{s.fleet_size === 1 ? "" : "s"}</Tooltip>
          </Marker>
        ))}
        {facs.map((f) => {
          const col = palette.sev[f.severity] ?? palette.sev.ok;
          const isSel = selected === f.id;
          return (
            <CircleMarker key={`f-${f.id}`} center={[f.lat, f.lon]}
              radius={isSel ? 13 : f.severity === "ok" ? 8 : 11}
              pathOptions={{ color: isSel ? palette.text : "#ffffff", weight: isSel ? 3 : 2, fillColor: col, fillOpacity: 0.95 }}
              eventHandlers={{ click: () => onSelect(f.id) }}>
              <Tooltip direction="top" offset={[0, -8]}>
                <strong>{f.id}</strong><br />
                {CATEGORY_LABEL[f.status]} · tank {pct(f.level_pct)}
              </Tooltip>
            </CircleMarker>
          );
        })}
      </MapContainer>
      <div className="legend" aria-label="Legend">
        <span><i className="dot sev-critical-bg" />Failure &lt;2 h</span>
        <span><i className="dot sev-high-bg" />Failure</span>
        <span><i className="dot sev-urgent-bg" />Leak</span>
        <span><i className="dot sev-warning-bg" />Worn pump</span>
        <span><i className="dot sev-ok-bg" />Normal</span>
        <span><i className="diamond" />Disposal</span>
        <span><i className="yard">T</i>Truck yard</span>
      </div>
    </section>
  );
}
