export interface Movie {
  tmdb_id: number; imdb_id: string | null; title: string; original_title: string;
  year: number; original_lang: string; french_path: string | null; quality: string | null;
  status: string; candidate_title: string | null; candidate_score: number | null;
  candidate_seeders: number | null; dl_hash: string | null; en_file: string | null;
  merged_file: string | null; sync_delta: number | null; sync_offset_ms: number;
  error: string | null; updated: number; poster?: string | null; progress?: string | null;
  ai_status?: string | null; ai_verdict?: string | null; sync_drift?: number | null;
  priority?: number | null;   // >0 = jumps the search sweep and the merge queue
  // read off the FILE by mkvmerge, not from Radarr/Sonarr metadata
  audio_langs?: string | null; sub_langs?: string | null; needs?: string | null;
  need_audio?: string | null; need_subs?: string | null; added_subs?: string | null;
}
export interface Status {
  enabled: boolean; grab_mode: string; counts: Record<string, number>; states: string[];
  paused?: boolean;
  hold?: string | null;      // "paused" | "scanning" — why no new work is starting
  merging_now?: number;      // merges still in flight (a pause lets these finish)
}
export interface Episode {
  id: string; series_id: number; series_title: string; season: number; episode: number;
  status: string; candidate_title: string | null; candidate_score: number | null;
  candidate_seeders: number | null; quality: string | null; poster: string | null;
  sync_delta: number | null; error: string | null; progress?: string | null;
  dl_hash?: string | null; series_type?: string | null;
  ai_status?: string | null; ai_verdict?: string | null; sync_drift?: number | null;
  priority?: number | null;   // see Movie
  aired?: string | null;   // "S04E15" when releases number this episode differently
  audio_langs?: string | null; sub_langs?: string | null; needs?: string | null;
  need_audio?: string | null; need_subs?: string | null; added_subs?: string | null;
}
export interface TvStatus { counts: Record<string, number>; }
// What a scan/rescan POST returns: it starts a background pass rather than doing the work in
// the request, so there is no count to report yet — poll rescanState() for progress.
export interface RescanStart {
  ok: boolean; started: boolean; note?: string; state?: RescanState;
}
export interface RescanState {
  running: boolean; scope: string; phase: string; started: number; finished: number;
  films: number | null; episodes: number | null; error: string | null;
  probes: { cached: number; unreadable: number };
  // files that vanished from the library since the last pass and were dropped from the DB
  pruned?: number | null; pruned_records?: number | null;
  full?: boolean | null;      // true = the cache was dropped first; false = progressive
  read?: number;              // files actually re-read this pass (live)
  reused?: number;            // files served from the probe cache (live)
  // why *arr-known files did not become inventory rows, so a wrong file count is explainable
  skips?: { films?: Record<string, number>; episodes?: Record<string, number> };
}
export interface LibItem {
  path: string; rel: string; lib: string; kind: string;
  title: string; file: string; state: "complete" | "incomplete" | "unreadable";
  audio: string[]; subs: string[]; missing_audio: string[]; missing_subs: string[];
  targets: { audio: string[]; subs: string[] };
  dur: number | null; probed: number | null; err: string | null;
}
export interface LibPage {
  items: LibItem[]; total: number; offset: number;
  counts: { complete: number; incomplete: number; unreadable: number };
  // unreadable broken down by WHY — "no audio track" is a broken file, the rest are our tools
  error_kinds: Record<string, number>;
  repairable: number;    // files carrying no audio at all: deletable + re-searchable
  libraries: { name: string; total: number }[];
}
export interface RecheckState {
  running: boolean; scope: string; started: number; finished: number;
  checked: number; total: number; reopened: number; complete: number;
  unreadable: number; gone: number; error: string | null;
}
export interface RepairPlan {
  dry_run: true; total: number; unknown: number;
  candidates: { path: string; title: string; kind: string; known: boolean }[];
}
// What the on-call AI reported back. Its own query because a callback leaves the pipeline status
// alone, so a record it fixed has usually moved on and nothing selecting by status can find it.
export interface AiLogItem {
  kind: string; key: string; title: string; sub: string; status: string;
  ai_status: string; ai_verdict: string | null; ai_at: number;
  error: string | null; poster: string | null;
}
export interface AiLog {
  items: AiLogItem[]; counts: Record<string, number>; now: number;
}
export interface RepairState {
  running: boolean; started: number; finished: number; phase: string;
  checked: number; total: number; deleted: number; searched: number;
  skipped: { path: string; reason: string }[];
  done: { path: string; title: string; kind: string }[];
  error: string | null;
}
export interface CoverageLib {
  name: string; kind: string; total: number; unreadable: number;
  complete: number; missing_audio: number; missing_subs: number; missing_both: number;
  audio: Record<string, number>; subs: Record<string, number>;
  // how many files actually TARGET each language — not every file in a library wants the same
  // set, since the anime profile's original-audio slot resolves per title (Blue Lock wants jpn,
  // Arcane doesn't). Scoring a language against the library total would understate it.
  audio_of?: Record<string, number>; subs_of?: Record<string, number>;
  targets: { audio: string[]; subs: string[] };
}
// When the library reaches a target %, at the rate it is actually going. `eta_days` is null
// when there is no rate to project from, or when the remaining files are blocked rather than
// merely pending — `reason` says which, so the panel never shows a date it can't stand behind.
export interface Forecast {
  target: number; total: number; complete: number; unreadable: number;
  pct: number | null; needed: number;
  rate: Record<string, number>; rate_used: number;
  blocked: { no_release: number; ignored: number; unreadable: number };
  eta_days: number | null; eta_ts?: number; reason: string; now: number;
  libraries: { name: string; total: number; complete: number; pct: number }[];
}
export interface Coverage {
  libraries: CoverageLib[]; total: number; complete: number; unreadable: number; probed: number;
}
export interface DL {
  progress: number; dlspeed: number; eta: number; state: string;
  seeds: number; size: number; downloaded: number;
}
export interface Candidate {
  score: number; seeders: number; size: number; title: string; indexer: string;
  multi: boolean; link: string; rid: string; tried: boolean; pack?: boolean; info_url?: string | null;
  complete?: boolean;      // covers the whole show (a complete-series / batch release)
}
export interface DashActive {
  kind: string; key: string; title: string; sub?: string | null; status: string;
  progress?: string | null; dl_hash?: string | null; poster?: string | null; count: number;
  queue_pos?: number | null;   // place in the merge queue while status is 'ready'
}
export interface DashAttention {
  kind: string; key: string; title: string; status: string; error?: string | null;
  sync_delta?: number | null; poster?: string | null; ts: number;
  ai_status?: string | null; ai_verdict?: string | null;
  count?: number;            // episodes sharing this status+error (a failed pack groups into one)
}
export interface DashRecent {
  kind: string; title: string; langs?: string | null; poster?: string | null; ts: number;
  subs?: string | null;    // subtitle languages grafted in
  how?: string;            // "grafted" (tracks added) | "replaced" (download became the file)
}
// One page of the full merge history. Rows are DashRecent, so the dashboard panel and the full
// list render through the same component and can never drift apart.
export interface MergedLog {
  items: DashRecent[]; total: number; offset: number; limit: number; now: number;
}
// How the on-call AI dispatcher is actually doing. `resolved`/`failed` are verdicts it produced
// itself; `needs_human` is mostly the no-callback flip, so a wall of it with last_callback null
// means the dispatcher never ran — which otherwise looks identical to "it examined and gave up".
// The dispatcher runs on the host, outside this container, so the only evidence we have is
// whether it CONSUMES what we write (tickets should not sit in the directory) and whether it
// CALLS BACK. `waiting`/`oldest_age` describe the ticket directory.
export interface AiAgent {
  enabled: boolean; dir: string; waiting: number; oldest_age: number | null;
}
export interface AiHealth {
  enabled: boolean; stale_min: number; last_callback: number | null;
  pending: number; resolved: number; failed: number; needs_human: number; never_sent: number;
  agent?: AiAgent;
}
export interface Dash {
  enabled: boolean; grab_mode: string; scope_series: boolean;
  movies: Record<string, number>; episodes: Record<string, number>;
  active: DashActive[]; attention: DashAttention[]; recent: DashRecent[];
  ai_working?: number;    // failures still with the AI (counted, not listed in attention)
  ai?: AiHealth;
  merged_24h: number; merged_7d: number;
  // the 'merged' population by outcome: grafted / replaced = work we did, already = the file
  // was correct on its own and the scan simply closed the record out
  merged_kinds?: { grafted: number; replaced: number; already: number };
  inflight: number | null; inflight_cap: number; merge_cap: number;
  disk: { path: string; total: number; free: number } | null;
  next_runs: Record<string, number>; now: number;
}

// When the server has an api_key set, every request needs it. It is kept in localStorage so a
// reload doesn't log you out; a 401 clears it and prompts again, so a rotated key can't leave the
// UI permanently wedged against a stale one.
const KEY_STORAGE = "vo-merge.apiKey";

export function getApiKey(): string {
  try { return localStorage.getItem(KEY_STORAGE) || ""; } catch { return ""; }
}

export function setApiKey(k: string) {
  try { k ? localStorage.setItem(KEY_STORAGE, k) : localStorage.removeItem(KEY_STORAGE); } catch { /* private mode */ }
}

/** Append the API key to a URL loaded by the browser directly (a <video src> or a bare fetch
 *  can't carry the X-API-Key header). The guard accepts ?apikey= for exactly this. */
export function withKey(url: string): string {
  const k = getApiKey();
  if (!k) return url;
  return url + (url.includes("?") ? "&" : "?") + "apikey=" + encodeURIComponent(k);
}

export class Unauthorized extends Error {
  constructor() { super("API key required"); this.name = "Unauthorized"; }
}

async function j<T>(url: string, opts?: RequestInit): Promise<T> {
  const key = getApiKey();
  const r = await fetch(url, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      ...(key ? { "X-API-Key": key } : {}),
      ...(opts?.headers || {}),
    },
  });
  if (r.status === 401) { setApiKey(""); throw new Unauthorized(); }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export const api = {
  status: () => j<Status>("/api/status"),
  dashboard: () => j<Dash>("/api/dashboard"),
  movies: (status?: string) =>
    j<Movie[]>("/api/movies" + (status ? `?status=${encodeURIComponent(status)}` : "")),
  settings: () => j<Record<string, any>>("/api/settings"),
  saveSettings: (data: Record<string, any>) =>
    j<{ ok: boolean }>("/api/settings", { method: "POST", body: JSON.stringify({ data }) }),
  test: (which: string) =>
    j<{ ok: boolean; error?: string }>(`/api/test/${which}`, { method: "POST" }),
  // Now a background scan (see main.do_scan) — same shape as rescan, not { found }.
  scan: () => j<RescanStart>("/api/scan", { method: "POST" }),
  pause: (on: boolean) =>
    j<{ ok: boolean; paused: boolean; in_flight: string[] }>(
      "/api/pause", { method: "POST", body: JSON.stringify({ on }) }),
  searchAll: () => j<{ ok: boolean; started: boolean; pending?: number; slots?: number | null; note?: string }>(
    "/api/search_all", { method: "POST" }),
  search: (id: number) => j<Movie>(`/api/movie/${id}/search`, { method: "POST" }),
  // Queues rather than merges inline (see pipeline.enqueue_merge). `note` says when nothing will
  // drain the queue — the pipeline being disabled or paused.
  merge: (id: number) =>
    j<{ movie: Movie; queued: boolean; note: string }>(`/api/movie/${id}/merge`, { method: "POST" }),
  sync: (id: number, offset_ms: number) =>
    j<Movie>(`/api/movie/${id}/sync`, { method: "POST", body: JSON.stringify({ offset_ms }) }),
  retry: (id: number) => j<{ ok: boolean }>(`/api/movie/${id}/retry`, { method: "POST" }),
  ignore: (id: number) => j<{ ok: boolean }>(`/api/movie/${id}/ignore`, { method: "POST" }),
  unignore: (id: number) => j<{ ok: boolean }>(`/api/movie/${id}/unignore`, { method: "POST" }),
  research: (id: number) => j<Movie>(`/api/movie/${id}/research`, { method: "POST" }),
  // Move a title to the front of the search sweep AND the merge queue (level 0 = normal).
  priority: (id: number, level = 1) =>
    j<{ ok: boolean; priority: number }>(`/api/movie/${id}/priority`,
      { method: "POST", body: JSON.stringify({ level }) }),
  epPriority: (id: string, level = 1) =>
    j<{ ok: boolean; priority: number }>(`/api/episode/${encodeURIComponent(id)}/priority`,
      { method: "POST", body: JSON.stringify({ level }) }),
  seriesPriority: (seriesId: number, level = 1) =>
    j<{ ok: boolean; priority: number; episodes: number }>(`/api/tv/${seriesId}/priority`,
      { method: "POST", body: JSON.stringify({ level }) }),
  aiSend: (id: number) => j<{ ok: boolean; queued: boolean }>(`/api/movie/${id}/ai`, { method: "POST" }),
  aiLog: (outcome: "resolved" | "failed" | "needs_human" | "all" = "resolved", limit = 50) =>
    j<AiLog>(`/api/ai_log?outcome=${outcome}&limit=${limit}`),
  // Re-read ONE title's files (probe cache bypassed) and act on what is now missing — the unit
  // an operator works in after replacing a show's or a film's files by hand.
  rescanSeries: (seriesId: number, search = true) =>
    j<{ ok: boolean; started: boolean; scope?: string; note?: string }>(
      `/api/tv/${seriesId}/rescan?search=${search}`, { method: "POST" }),
  rescanMovie: (tmdbId: number, search = true) =>
    j<{ ok: boolean; outcome: string; searched: boolean; movie: Movie }>(
      `/api/movie/${tmdbId}/rescan?search=${search}`, { method: "POST" }),
  // the full Recently-merged history behind the dashboard panel's top ten
  mergedLog: (limit = 50, offset = 0, q = "") =>
    j<MergedLog>(`/api/merged?limit=${limit}&offset=${offset}`
                 + (q ? `&q=${encodeURIComponent(q)}` : "")),
  epAiSend: (id: string) =>
    j<{ ok: boolean; queued: boolean }>(`/api/episode/${encodeURIComponent(id)}/ai`, { method: "POST" }),
  another: (id: number) => j<Movie>(`/api/movie/${id}/another`, { method: "POST" }),
  logs: () => j<{ lines: string[] }>("/api/logs"),
  downloads: () => j<{ items: Record<string, DL>; error?: string }>("/api/downloads"),
  preview: (id: number, lang = "eng", t = -1) =>
    j<{ video: string; audio: string; start: number; fps: number; duration: number; movie_dur: number }>(
      `/api/movie/${id}/preview?lang=${lang}&t=${t}`),
  applyOffset: (id: number, offset_ms: number, lang = "eng") =>
    j<Movie>(`/api/movie/${id}/apply_offset`,
      { method: "POST", body: JSON.stringify({ offset_ms, lang }) }),

  // ---- TV / Series ----
  tvStatus: () => j<TvStatus>("/api/tv/status"),
  tvEpisodes: (status?: string) =>
    j<Episode[]>("/api/tv/episodes" + (status ? `?status=${encodeURIComponent(status)}` : "")),
  tvScan: () => j<RescanStart>("/api/tv/scan", { method: "POST" }),
  rescan: (scope: "all" | "films" | "anime" | "series" | "tv" = "all", forget = false) =>
    j<RescanStart>(`/api/rescan?scope=${scope}&forget=${forget}`, { method: "POST" }),
  rescanState: () => j<RescanState>("/api/rescan"),
  coverage: () => j<Coverage>("/api/coverage"),
  forecast: (target = 90) => j<Forecast>(`/api/forecast?target=${target}`),
  // re-probe everything in a settled state (merged / no_release) and re-open what's below target
  recheck: (scope: "all" | "films" | "tv" | "anime" | "series" = "all") =>
    j<{ ok: boolean; started: boolean; note?: string }>(
      `/api/recheck?scope=${scope}`, { method: "POST" }),
  recheckState: () => j<RecheckState>("/api/recheck"),
  library: (o: { state?: string; lib?: string; q?: string; limit?: number; offset?: number } = {}) =>
    j<LibPage>("/api/library?" + new URLSearchParams({
      state: o.state ?? "incomplete", lib: o.lib ?? "", q: o.q ?? "",
      limit: String(o.limit ?? 200), offset: String(o.offset ?? 0),
    })),
  // delete files that carry no audio at all and let Radarr/Sonarr fetch a replacement
  repairPlan: (paths?: string[]) =>
    j<RepairPlan>("/api/library/repair",
      { method: "POST", body: JSON.stringify({ dry_run: true, paths: paths ?? null }) }),
  repairRun: (paths?: string[]) =>
    j<{ ok: boolean; started: boolean; total?: number; note?: string }>("/api/library/repair",
      { method: "POST", body: JSON.stringify({ dry_run: false, paths: paths ?? null }) }),
  repairState: () => j<RepairState>("/api/library/repair"),
  tvRetryErrors: () => j<{ ok: boolean; retried: number }>("/api/tv/retry_errors", { method: "POST" }),
  retryAllErrors: () =>
    j<{ ok: boolean; movies: number; episodes: number }>("/api/retry_errors", { method: "POST" }),
  epRetry: (id: string) =>
    j<{ ok: boolean }>(`/api/episode/${encodeURIComponent(id)}/retry`, { method: "POST" }),
  epIgnore: (id: string) =>
    j<{ ok: boolean }>(`/api/episode/${encodeURIComponent(id)}/ignore`, { method: "POST" }),
  // whole-show releases: complete-series batches and multi-season packs. A per-season query can
  // never surface these — an indexer asked for "Title S01" doesn't return "(Complete Series)".
  seriesCandidates: (seriesId: number) =>
    j<Candidate[]>(`/api/tv/${seriesId}/candidates`),
  seriesGrab: (seriesId: number, link: string, rid: string, title: string) =>
    j<{ ok: boolean; episodes: number }>(`/api/tv/${seriesId}/grab`,
      { method: "POST", body: JSON.stringify({ link, rid, title }) }),
  seasonCandidates: (seriesId: number, season: number) =>
    j<Candidate[]>(`/api/tv/${seriesId}/${season}/candidates`),
  seasonGrab: (seriesId: number, season: number, link: string, rid: string, title: string) =>
    j<{ ok: boolean; episodes: number }>(`/api/tv/${seriesId}/${season}/grab`,
      { method: "POST", body: JSON.stringify({ link, rid, title }) }),
  episodeCandidates: (epId: string) =>
    j<Candidate[]>(`/api/episode/${encodeURIComponent(epId)}/candidates`),
  episodeGrab: (epId: string, link: string, rid: string, title: string) =>
    j<{ ok: boolean; episodes: number }>(`/api/episode/${encodeURIComponent(epId)}/grab`,
      { method: "POST", body: JSON.stringify({ link, rid, title }) }),

  // ---- Interactive release search ----
  candidates: (id: number) => j<Candidate[]>(`/api/movie/${id}/candidates`),
  grab: (id: number, link: string, rid: string, title: string) =>
    j<Movie>(`/api/movie/${id}/grab`,
      { method: "POST", body: JSON.stringify({ link, rid, title }) }),
};
