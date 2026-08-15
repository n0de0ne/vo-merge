import { useEffect, useRef, useState, type ReactNode } from "react";
import type { DL } from "../api";
import { fmtBytes, fmtEta, fmtSpeed, queueLabel } from "../lib/format";

/* Shared presentational pieces. Every one of these carries a decision that was got wrong once
   and is easy to get wrong again, so they live here rather than being re-typed per page. */

/** `ready` means "download finished, waiting its turn to merge". Saying "ready" made it read as
   "ready to watch", which is the opposite of what it means. */
export const STATE_LABEL: Record<string, string> = { ready: "queued" };

export const STATES = ["pending", "searching", "no_release", "grabbed", "downloading",
                       "ready", "merging", "merged", "review", "sync_fail", "error", "ignored"];
export const TV_STATES = STATES.filter(s => s !== "review");

export function Pill({ s }: { s: string }) {
  return <span className={`pill ${s}`}>{STATE_LABEL[s] ?? s.replace("_", " ")}</span>;
}

const AI_LABEL: Record<string, string> = {
  pending: "🤖 AI working", resolved: "🤖 AI resolved",
  failed: "🤖 AI couldn’t fix", needs_human: "🤖 needs you",
};
export function AiPill({ s }: { s?: string | null }) {
  if (!s) return null;
  return <span className={`pill ai_${s}`}>{AI_LABEL[s] ?? s}</span>;
}
export const aiUnfixed = (s?: string | null) => s === "failed" || s === "needs_human";

const LANG_NAME: Record<string, string> = {
  fre: "French", eng: "English", jpn: "Japanese", spa: "Spanish", ger: "German",
  ita: "Italian", por: "Portuguese", rus: "Russian", kor: "Korean", zho: "Chinese",
};
export const lang = (c: string) => LANG_NAME[c] ?? c.toUpperCase();

export function LiveDot() {
  return <span className="livedot" title="Auto-refreshing while this tab is visible">live</span>;
}

export function Poster({ src, alt, sm }: { src?: string | null; alt: string; sm?: boolean }) {
  const cls = "poster" + (sm ? " sm" : "");
  return src
    ? <img className={cls} src={src} alt="" aria-hidden="true" loading="lazy" />
    : <div className={cls + " placeholder"} aria-label={`no poster for ${alt}`}>🎞</div>;
}

/** Per-row action menu: a native <details> dropdown so a row never sprawls into many buttons.
 *  Clicking any child closes it. Its container must not clip — see the CSS notes. */
export function RowMenu({ children, label = "Actions" }: { children: ReactNode; label?: string }) {
  const ref = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node))
        ref.current.removeAttribute("open");
    };
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, []);
  return (
    <details className="rowmenu" ref={ref}>
      <summary className="btn sec">{label} ▾</summary>
      <div className="menu-pop" onClick={() => ref.current?.removeAttribute("open")}>{children}</div>
    </details>
  );
}

const DL_LABEL: Record<string, string> = {
  metaDL: "fetching metadata", queuedDL: "queued", stalledDL: "stalled",
  checkingDL: "checking", pausedDL: "paused", allocating: "allocating", moving: "moving",
};

export function DownloadBar({ dl }: { dl?: DL }) {
  if (!dl) return null;
  const pct = Math.round((dl.progress || 0) * 100);
  const st = dl.state || "";
  const active = (dl.dlspeed || 0) > 0;
  const label = active ? fmtSpeed(dl.dlspeed) : pct >= 100 ? "done" : (DL_LABEL[st] ?? "waiting");
  const meta = [label, active ? fmtEta(dl.eta) : "", `${dl.seeds || 0} seeds`]
    .filter(Boolean).join(" · ");
  const cls = active ? "live" : st === "stalledDL" ? "stalled" : st === "queuedDL" ? "queued" : "";
  return (
    <div className="dlbar" title={`${st} · ${pct}% of ${fmtBytes(dl.size)}`}>
      <div className="dlbar-track"><div className={"dlbar-fill " + cls} style={{ width: pct + "%" }} /></div>
      <div className="dlbar-meta">{pct}% · {meta}</div>
    </div>
  );
}

export function QueuedLine({ pos }: { pos?: number | null }) {
  return <div className="sub queued">⏳ {queueLabel(pos)}</div>;
}

/** A rate stretch was applied (PAL 25fps vs 23.976 etc). Worth showing — it is not a plain
 *  offset, and it is the single most useful thing to see on a title that keeps failing. */
export function DriftBadge({ d }: { d?: number | null }) {
  if (!d || Math.abs(d - 1) < 1e-6) return null;
  const pct = (d - 1) * 100;
  return <span className="drift" title={`audio time-stretched ${pct.toFixed(2)}% to match the video's rate`}>
    ⏩ rate ×{d.toFixed(4)} ({pct > 0 ? "+" : ""}{pct.toFixed(2)}%)</span>;
}

/** What the library file actually contains, read off the FILE by mkvmerge — not from
 *  Radarr/Sonarr metadata — plus what it is still missing. */
export function Tracks({ a, s, na, ns }:
  { a?: string | null; s?: string | null; na?: string | null; ns?: string | null }) {
  if (!a && !s && !na && !ns) return null;
  return (
    <div className="sub tracks" title="languages read from the file itself">
      🔊 {a || <span className="muted">none tagged</span>}
      {s ? <> · 💬 {s}</> : <> · <span className="muted">no subs</span></>}
      {na && <span className="needs">+ {na} audio</span>}
      {ns && <span className="needs">+ {ns} subs</span>}
    </div>
  );
}

export function Tile({ label, value, sub, tone, onClick, spark }:
  { label: string; value: ReactNode; sub?: ReactNode; tone?: "good" | "warn" | "bad";
    onClick?: () => void; spark?: ReactNode }) {
  return (
    <div className={"tile" + (tone ? " " + tone : "") + (onClick ? " click" : "")}
      onClick={onClick} role={onClick ? "button" : undefined} tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? e => { if (e.key === "Enter" || e.key === " ") onClick(); } : undefined}>
      <div className="t-label">{label}</div>
      <div className="t-num">{value}</div>
      {sub && <div className="t-sub">{sub}</div>}
      {spark && <div className="t-spark">{spark}</div>}
    </div>
  );
}

export function Modal({ title, onClose, children, wide, foot }:
  { title: ReactNode; onClose: () => void; children: ReactNode; wide?: boolean; foot?: ReactNode }) {
  useEffect(() => {
    const on = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [onClose]);
  return (
    <div className="modal-bg" onClick={onClose} role="dialog" aria-modal="true">
      <div className="modal" onClick={e => e.stopPropagation()}
        style={wide ? { width: "min(1040px, 96vw)" } : undefined}>
        <div className="modal-head">
          <b className="h2">{title}</b><div className="spacer" />
          <button className="btn sec" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {children}
        {foot && <div className="row panel-foot">{foot}</div>}
      </div>
    </div>
  );
}

/** An empty state that says what to DO. "Nothing here" is a dead end; "nothing here because no
 *  scan has run — here is the button" is not. */
export function Empty({ children }: { children: ReactNode }) {
  return <div className="muted" style={{ padding: "14px 2px", lineHeight: 1.6 }}>{children}</div>;
}

/** Async button: disables itself and shows a spinner word while its action runs, so a slow
 *  endpoint can't be clicked four times. */
export function Act({ children, run, cls = "btn sec", title, disabled, busyLabel = "…" }:
  { children: ReactNode; run: () => Promise<unknown>; cls?: string; title?: string;
    disabled?: boolean; busyLabel?: string }) {
  const [busy, setBusy] = useState(false);
  return (
    <button className={cls} title={title} disabled={busy || disabled}
      onClick={async () => { setBusy(true); try { await run(); } finally { setBusy(false); } }}>
      {busy ? busyLabel : children}
    </button>
  );
}
