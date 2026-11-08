import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import type { HandoffResponse } from "../types";

interface Props {
  dataset: string;
  now: number | null;
  onClose: () => void;
}

/** Inline Markdown: **bold** and `code`. Built as React nodes, so nothing is injected as HTML. */
function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    out.push(tok.startsWith("**") ? <strong key={m.index}>{tok.slice(2, -2)}</strong> : <code key={m.index}>{tok.slice(1, -1)}</code>);
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** Small Markdown renderer for the handoff: headings, bullet/numbered lists (one level of
 *  nesting), and paragraphs. */
function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  const lines = text.split(/\r?\n/);
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const h = /^(#{1,3})\s+(.*)$/.exec(line);
    if (h) {
      const level = h[1].length;
      const content = inline(h[2]);
      blocks.push(level === 1 ? <h2 key={i}>{content}</h2> : level === 2 ? <h3 key={i}>{content}</h3> : <h4 key={i}>{content}</h4>);
      i++;
      continue;
    }
    const li = /^(\s*)([-*]|\d+\.)\s+(.*)$/.exec(line);
    if (li) {
      const ordered = /\d+\./.test(li[2]);
      const items: { text: string; sub: string[] }[] = [];
      while (i < lines.length) {
        const m = /^(\s*)([-*]|\d+\.)\s+(.*)$/.exec(lines[i]);
        if (!m) break;
        if (m[1].length >= 2 && items.length) items[items.length - 1].sub.push(m[3]);
        else items.push({ text: m[3], sub: [] });
        i++;
      }
      const children = items.map((it, k) => (
        <li key={k}>
          {inline(it.text)}
          {it.sub.length > 0 && <ul>{it.sub.map((s, j) => <li key={j}>{inline(s)}</li>)}</ul>}
        </li>
      ));
      blocks.push(ordered ? <ol key={i}>{children}</ol> : <ul key={i}>{children}</ul>);
      continue;
    }
    if (line.trim()) blocks.push(<p key={i}>{inline(line)}</p>);
    i++;
  }
  return <div className="md">{blocks}</div>;
}

export function HandoffPanel({ dataset, now, onClose }: Props) {
  const [res, setRes] = useState<HandoffResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  const run = useCallback(() => {
    setBusy(true);
    setErr(null);
    api.handoff(dataset, now).then(setRes).catch((e) => setErr(String(e.message ?? e))).finally(() => setBusy(false));
  }, [dataset, now]);

  useEffect(run, [run]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const copy = () => {
    if (!res) return;
    navigator.clipboard?.writeText(res.markdown).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }).catch(() => setErr("Copy failed: select the text instead"));
  };
  const download = () => {
    if (!res) return;
    const blob = new Blob([res.markdown], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `shift-handoff-${dataset.split("/").pop()}-${res.now_unix}.md`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <section className="modal" role="dialog" aria-modal="true" aria-label="Shift handoff" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>Shift handoff</h2>
          {res && (
            <span className={`tag ${res.source === "nemotron" ? "tag-ai" : "tag-template"}`}
              title={res.source === "nemotron" ? res.model ?? "" : res.reason ?? ""}>
              {res.source === "nemotron" ? "Nemotron" : "Template"}
            </span>
          )}
          <div className="modal-actions">
            <button className="btn btn-small" onClick={copy} disabled={!res || busy}>{copied ? "Copied" : "Copy"}</button>
            <button className="btn btn-small" onClick={download} disabled={!res || busy}>Download .md</button>
            <button className="btn btn-small" onClick={run} disabled={busy}>Regenerate</button>
            <button className="btn btn-small" onClick={onClose} aria-label="Close">Close</button>
          </div>
        </header>
        <div className="modal-body">
          {busy && <div className="empty"><span className="spinner" /> <div>Writing the handoff from the current alerts, plans and trends…</div></div>}
          {err && <div className="error">{err}</div>}
          {res && !busy && (
            <>
              {res.source === "template" && res.reason && (
                <div className="muted small template-note">Template mode ({res.reason}). Add NVIDIA_API_KEY to .env for the Nemotron agent.</div>
              )}
              <Markdown text={res.markdown} />
              <details className="why">
                <summary>Agent steps ({res.steps.length} tool call{res.steps.length === 1 ? "" : "s"}, {res.seconds}s)</summary>
                <ol className="trace">
                  {res.steps.map((s, i) => (
                    <li key={i}><code>{s.tool}</code>{Object.keys(s.args).length > 0 && <span className="muted"> {JSON.stringify(s.args)}</span>}
                      {!s.ok && <span className="sev-critical-text"> (error)</span>}</li>
                  ))}
                </ol>
              </details>
            </>
          )}
        </div>
      </section>
    </div>
  );
}
