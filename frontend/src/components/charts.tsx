import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { fmtDay, fmtNum } from "../lib/format";

/* ======================================================================
   A small SVG chart kit — no dependencies, because a charting library is
   500 KB to draw four charts and this app ships as one container image.

   Rules baked into the primitives rather than left to each call site:

   - ONE axis. Never two y-scales on one plot: the alignment between them
     is arbitrary, so the chart invents a correlation the data doesn't
     have. Two measures of different scale get two charts.
   - Colour follows the ENTITY, not its rank. Series take a fixed palette
     slot by key, so filtering one out never repaints the survivors.
   - A gap is not a zero. `null` breaks the line; it does not plot at the
     baseline. The coverage series is sampled daily and genuinely has
     holes, and drawing them as zero would show a library that collapsed
     and recovered overnight.
   - Every chart has a hover layer and a table twin. A tooltip enhances,
     it never gates: the numbers are always reachable without a pointer.
   ====================================================================== */

export const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)",
                       "var(--series-4)", "var(--series-5)", "var(--series-6)"];
export const STATUS = {
  good: "var(--st-good)", warn: "var(--st-warn)", serious: "var(--st-serious)",
  crit: "var(--st-crit)", none: "var(--st-none)",
};

export interface Series { key: string; label: string; color: string; values: (number | null)[]; }

/** Width of a container, tracked live. SVG charts need real pixels — a viewBox that stretches
 *  would scale the type along with the plot and make every label a different size. */
export function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [w, setW] = useState(600);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setW(el.clientWidth || 600));
    ro.observe(el);
    setW(el.clientWidth || 600);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

interface Tip { x: number; y: number; title: string; rows: { label: string; value: string; color?: string }[]; }

function Tooltip({ tip }: { tip: Tip | null }) {
  if (!tip) return null;
  // Flip before the right edge rather than letting the box hang off-screen; a tooltip you can
  // only half read is worse than none.
  const flip = tip.x > window.innerWidth - 190;
  return (
    <div className="charttip" style={{ left: tip.x + (flip ? -178 : 14), top: tip.y - 10 }}>
      <div className="t">{tip.title}</div>
      {tip.rows.map((r, i) => (
        <div className="r" key={i}>
          {r.color && <span className="sw" style={{ background: r.color }} />}
          <span>{r.label}</span><b>{r.value}</b>
        </div>
      ))}
    </div>
  );
}

export function Legend({ series, values }: { series: Series[]; values?: (string | null)[] }) {
  if (series.length < 2) return null;      // one series is named by the title; a box would be noise
  return (
    <div className="legend">
      {series.map((s, i) => (
        <span className="k" key={s.key}>
          <span className="sw" style={{ background: s.color }} />
          {s.label}{values?.[i] ? <b>{values[i]}</b> : null}
        </span>
      ))}
    </div>
  );
}

/** A chart panel with a title, an optional right-hand slot, and a table twin behind a toggle.
 *  The toggle is not decoration — it is the WCAG-clean equivalent of the plot, and the reason a
 *  colour-encoded chart is allowed to exist here at all. */
export function ChartCard(
  { title, note, right, children, table, height }:
  { title: string; note?: ReactNode; right?: ReactNode; children: ReactNode;
    table?: ReactNode; height?: number }) {
  const [asTable, setAsTable] = useState(false);
  return (
    <div className="panel">
      <div className="row panel-head" style={{ gap: 8 }}>
        <b className="h2">{title}</b>
        {note && <span className="sub">{note}</span>}
        <div className="spacer" />
        {right}
        {table && (
          <button className="btn ghost small" aria-pressed={asTable}
            title="Show the same numbers as a table"
            onClick={() => setAsTable(t => !t)}>{asTable ? "▤ chart" : "▤ table"}</button>
        )}
      </div>
      {asTable && table
        ? <div className="scroll-y" style={{ maxHeight: (height ?? 200) + 60 }}>{table}</div>
        : children}
    </div>
  );
}

export function ChartEmpty({ children }: { children: ReactNode }) {
  return <div className="chart-empty">{children}</div>;
}

/* ---------------------------------------------------------------- time series */
export function TimeSeries(
  { days, series, height = 190, area = false, unit = "", yMax, yMin = 0, valueFmt }:
  { days: string[]; series: Series[]; height?: number; area?: boolean; unit?: string;
    yMax?: number; yMin?: number; valueFmt?: (n: number) => string }) {
  const [ref, w] = useWidth<HTMLDivElement>();
  const [tip, setTip] = useState<Tip | null>(null);
  const [hover, setHover] = useState(-1);
  const pad = { l: 42, r: 12, t: 10, b: 22 };
  const iw = Math.max(60, w - pad.l - pad.r), ih = height - pad.t - pad.b;
  const all = series.flatMap(s => s.values).filter((v): v is number => v != null);
  const hi = yMax ?? (all.length ? Math.max(...all) : 1);
  const lo = yMin;
  const top = hi === lo ? lo + 1 : hi + (hi - lo) * 0.08;
  const x = (i: number) => pad.l + (days.length < 2 ? iw / 2 : (i / (days.length - 1)) * iw);
  const y = (v: number) => pad.t + ih - ((v - lo) / (top - lo)) * ih;
  const fmt = valueFmt ?? ((n: number) => fmtNum(n) + unit);

  // Break the path wherever the series has no sample. See the header: a hole is not a zero.
  const pathOf = (s: Series) => {
    let d = "", pen = false;
    s.values.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
      pen = true;
    });
    return d.trim();
  };
  const areaOf = (s: Series) => {
    const segs: string[] = [];
    let cur: string[] = [];
    s.values.forEach((v, i) => {
      if (v == null) { if (cur.length > 1) segs.push(cur.join(" ")); cur = []; return; }
      cur.push(`${cur.length ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`);
    });
    if (cur.length > 1) segs.push(cur.join(" "));
    return segs.map(seg => {
      const pts = seg.split(" ");
      const first = pts[0].slice(1).split(",")[0];
      const last = pts[pts.length - 1].slice(1).split(",")[0];
      return `${seg} L${last},${(pad.t + ih).toFixed(1)} L${first},${(pad.t + ih).toFixed(1)} Z`;
    }).join(" ");
  };

  const ticks = 4;
  const gridY = Array.from({ length: ticks + 1 }, (_, i) => lo + ((top - lo) * i) / ticks);
  const step = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor(iw / 74))));

  function onMove(e: React.MouseEvent) {
    const box = (e.currentTarget as SVGRectElement).getBoundingClientRect();
    const rel = e.clientX - box.left;
    const i = Math.max(0, Math.min(days.length - 1,
      Math.round((rel / Math.max(1, box.width)) * (days.length - 1))));
    setHover(i);
    setTip({
      x: e.clientX, y: e.clientY, title: fmtDay(days[i]),
      rows: series.map(s => ({
        label: s.label, color: s.color,
        value: s.values[i] == null ? "no sample" : fmt(s.values[i] as number),
      })),
    });
  }

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <svg className="chart" width={w} height={height} role="img"
        aria-label={`${series.map(s => s.label).join(", ")} over ${days.length} days`}>
        <g className="grid">
          {gridY.map((v, i) => (
            <line key={i} x1={pad.l} x2={pad.l + iw} y1={y(v)} y2={y(v)} />
          ))}
        </g>
        {gridY.map((v, i) => (
          <text key={i} x={pad.l - 7} y={y(v) + 3.5} textAnchor="end">{fmt(v)}</text>
        ))}
        {days.map((d, i) => (i % step === 0 || i === days.length - 1) && (
          <text key={d} x={x(i)} y={height - 6} textAnchor={i === 0 ? "start"
            : i === days.length - 1 ? "end" : "middle"}>{fmtDay(d)}</text>
        ))}
        {area && series.map(s => (
          <path key={s.key + "-a"} d={areaOf(s)} fill={s.color} opacity={0.14} />
        ))}
        {series.map(s => (
          <path key={s.key} d={pathOf(s)} fill="none" stroke={s.color} strokeWidth={2}
            strokeLinejoin="round" strokeLinecap="round" />
        ))}
        {hover >= 0 && (
          <>
            <line className="cursor" x1={x(hover)} x2={x(hover)} y1={pad.t} y2={pad.t + ih} />
            {series.map(s => s.values[hover] != null && (
              // 2px surface ring rather than a stroke of its own colour: the marker has to read
              // as separate from the line it sits on without adding a second hue.
              <circle key={s.key} cx={x(hover)} cy={y(s.values[hover] as number)} r={4}
                fill={s.color} stroke="var(--panel)" strokeWidth={2} />
            ))}
          </>
        )}
        <rect className="hit" x={pad.l} y={pad.t} width={iw} height={ih}
          onMouseMove={onMove} onMouseLeave={() => { setTip(null); setHover(-1); }} />
      </svg>
      <Tooltip tip={tip} />
    </div>
  );
}

/* ---------------------------------------------------------------- stacked day bars */
export function DayBars(
  { days, series, height = 190, unit = "" }:
  { days: string[]; series: Series[]; height?: number; unit?: string }) {
  const [ref, w] = useWidth<HTMLDivElement>();
  const [tip, setTip] = useState<Tip | null>(null);
  const [hover, setHover] = useState(-1);
  const pad = { l: 42, r: 12, t: 10, b: 22 };
  const iw = Math.max(60, w - pad.l - pad.r), ih = height - pad.t - pad.b;
  const totals = days.map((_, i) => series.reduce((a, s) => a + (s.values[i] || 0), 0));
  const hi = Math.max(1, ...totals);
  const top = hi + hi * 0.1;
  const bw = Math.max(2, Math.min(26, (iw / Math.max(1, days.length)) - 3));
  const x = (i: number) => pad.l + (iw / Math.max(1, days.length)) * (i + 0.5) - bw / 2;
  const h = (v: number) => (v / top) * ih;
  const ticks = 4;
  const gridY = Array.from({ length: ticks + 1 }, (_, i) => (top * i) / ticks);
  const step = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor(iw / 74))));

  function show(i: number, e: React.MouseEvent) {
    setHover(i);
    setTip({
      x: e.clientX, y: e.clientY, title: fmtDay(days[i]),
      rows: [...series.map(s => ({ label: s.label, color: s.color,
                                   value: fmtNum(s.values[i] || 0) + unit })),
             { label: "total", value: fmtNum(totals[i]) + unit }],
    });
  }

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <svg className="chart" width={w} height={height} role="img"
        aria-label={`${series.map(s => s.label).join(", ")} per day`}>
        <g className="grid">
          {gridY.map((v, i) => <line key={i} x1={pad.l} x2={pad.l + iw} y1={pad.t + ih - h(v)}
                                     y2={pad.t + ih - h(v)} />)}
        </g>
        {gridY.map((v, i) => (
          <text key={i} x={pad.l - 7} y={pad.t + ih - h(v) + 3.5} textAnchor="end">
            {fmtNum(Math.round(v))}</text>
        ))}
        {days.map((d, i) => (i % step === 0 || i === days.length - 1) && (
          <text key={d} x={x(i) + bw / 2} y={height - 6} textAnchor="middle">{fmtDay(d)}</text>
        ))}
        {days.map((_, i) => {
          let acc = 0;
          return (
            <g key={i} opacity={hover < 0 || hover === i ? 1 : 0.55}
              onMouseMove={e => show(i, e)} onMouseLeave={() => { setTip(null); setHover(-1); }}>
              {/* a hit target the width of the slot, so a 2px bar is still hoverable */}
              <rect className="hit" x={pad.l + (iw / Math.max(1, days.length)) * i} y={pad.t}
                width={iw / Math.max(1, days.length)} height={ih} />
              {series.map(s => {
                const v = s.values[i] || 0;
                if (v <= 0) return null;
                const y = pad.t + ih - h(acc + v);
                acc += v;
                return (
                  // A 2px surface-coloured gap between segments instead of a border: a stroke
                  // around every mark reads as a grid of boxes at this size.
                  <rect key={s.key} x={x(i)} y={y} width={bw}
                    height={Math.max(1, h(v) - 2)} rx={2} fill={s.color} />
                );
              })}
            </g>
          );
        })}
      </svg>
      <Tooltip tip={tip} />
    </div>
  );
}

/* ---------------------------------------------------------------- sparkline */
export function Sparkline(
  { values, color = "var(--series-1)", height = 26 }:
  { values: (number | null)[]; color?: string; height?: number }) {
  const [ref, w] = useWidth<HTMLDivElement>();
  const nums = values.filter((v): v is number => v != null);
  if (nums.length < 2) return <div ref={ref} style={{ height }} />;
  const hi = Math.max(...nums), lo = Math.min(...nums);
  const span = hi === lo ? 1 : hi - lo;
  const x = (i: number) => (i / (values.length - 1)) * (w - 2) + 1;
  const y = (v: number) => height - 2 - ((v - lo) / span) * (height - 4);
  let d = "", pen = false;
  values.forEach((v, i) => {
    if (v == null) { pen = false; return; }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
    pen = true;
  });
  const lastIdx = values.map((v, i) => (v == null ? -1 : i)).filter(i => i >= 0).pop() ?? 0;
  return (
    <div ref={ref} style={{ height }}>
      <svg className="chart" width={w} height={height} aria-hidden="true">
        <path d={d.trim()} fill="none" stroke={color} strokeWidth={1.75}
          strokeLinejoin="round" strokeLinecap="round" />
        <circle cx={x(lastIdx)} cy={y(values[lastIdx] as number)} r={2.5} fill={color} />
      </svg>
    </div>
  );
}

/* ---------------------------------------------------------------- composition bar
   The library's make-up: complete, and the three ways a file can be short, and unreadable. This
   is STATUS, not identity, so it uses the reserved status palette — and because warning and
   serious sit close together on purpose, every segment is labelled with its count in the legend
   below. Hue never carries the meaning alone here. */
export interface Slice { key: string; label: string; n: number; cls: string; }

export function StackBar({ slices, total }: { slices: Slice[]; total: number }) {
  const [tip, setTip] = useState<Tip | null>(null);
  if (total <= 0) return <div className="covbar" />;
  return (
    <>
      <div className="covbar" role="img"
        aria-label={slices.map(s => `${s.label} ${s.n}`).join(", ")}>
        {slices.filter(s => s.n > 0).map(s => (
          <span key={s.key} className={"seg " + s.cls}
            style={{ width: `${(s.n / total) * 100}%` }}
            onMouseMove={e => setTip({
              x: e.clientX, y: e.clientY, title: s.label,
              rows: [{ label: "files", value: fmtNum(s.n) },
                     { label: "of library", value: ((s.n / total) * 100).toFixed(1) + "%" }],
            })}
            onMouseLeave={() => setTip(null)} />
        ))}
      </div>
      <Tooltip tip={tip} />
    </>
  );
}

/** Escape hatch used by pages that need the tooltip outside a chart (the queue's disk meter). */
export function useEscape(onEsc: () => void) {
  useEffect(() => {
    const on = (e: KeyboardEvent) => { if (e.key === "Escape") onEsc(); };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [onEsc]);
}
