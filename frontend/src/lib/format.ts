/** Formatters shared by every page. Kept out of components so a table cell and a chart tooltip
 *  can never disagree about how a byte count or a duration reads. */

export const pad2 = (n: number) => String(n).padStart(2, "0");

export const fmtTime = (s: number) => {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return (h ? `${h}:` : "") + `${pad2(m).padStart(h ? 2 : 1, "0")}:${pad2(ss)}`;
};

export const fmtBytes = (b: number) => {
  if (!b || b <= 0) return "—";
  const gb = b / 1e9;
  if (gb >= 1) return gb.toFixed(2) + " GB";
  return (b / 1e6).toFixed(0) + " MB";
};

export const fmtSpeed = (b: number) =>
  !b || b <= 0 ? "" : b / 1e6 >= 1 ? (b / 1e6).toFixed(1) + " MB/s" : (b / 1e3).toFixed(0) + " kB/s";

export const fmtEta = (s: number) => (!s || s <= 0 || s >= 8640000 ? "" : "ETA " + fmtTime(s));

/** "3m ago" / "2d ago". `now` is the SERVER's clock, passed through from the response — a browser
 *  whose clock is a few minutes out would otherwise render "in 4 minutes" for something that has
 *  just happened. */
export function fmtAgo(ts?: number | null, now?: number) {
  if (!ts) return "—";
  const d = Math.max(0, (now ?? Date.now() / 1000) - ts);
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}

export function fmtIn(ts?: number | null, now?: number) {
  if (ts == null) return "—";
  const d = ts - (now ?? Date.now() / 1000);
  if (d <= 0) return "now";
  if (d < 60) return `${Math.round(d)}s`;
  if (d < 3600) return `${Math.round(d / 60)}m`;
  return `${(d / 3600).toFixed(1)}h`;
}

/** A signed millisecond offset, always with its sign — "+250 ms" and "250 ms" mean different
 *  things here and the difference is which way the audio moves. */
export const fmtMs = (ms?: number | null) =>
  ms == null ? "—" : `${ms >= 0 ? "+" : ""}${Math.round(ms)} ms`;

export const fmtNum = (n?: number | null) => (n == null ? "—" : n.toLocaleString());

export const fmtPct = (n?: number | null, dp = 1) => (n == null ? "—" : `${n.toFixed(dp)}%`);

/** "Mon 14" for an axis tick; the year is never the interesting part on a 30-day window. */
export const fmtDay = (day: string) => {
  const d = new Date(day + "T00:00:00Z");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
};

/** Episode label from a record. */
export const fmtSE = (season?: number | null, episode?: number | null) =>
  `S${pad2(season ?? 0)}E${pad2(episode ?? 0)}`;

/** 1 -> "next up", 2 -> "2nd in line". Ordinals so a queue position reads as a place, not a count. */
export const queueLabel = (pos?: number | null) => {
  if (!pos) return "queued for merge";
  if (pos === 1) return "next up to merge";
  const s = ["th", "st", "nd", "rd"][(pos % 100 - 20) % 10]
    || ["th", "st", "nd", "rd"][pos % 100] || "th";
  return `queued for merge · ${pos}${s} in line`;
};

/** The identity a bulk action uses: "movie:603" / "episode:12:1:4". */
export const recKey = (kind: string, key: string | number) => `${kind}:${key}`;
