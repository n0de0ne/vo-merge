export interface Movie {
  tmdb_id: number; imdb_id: string | null; title: string; original_title: string;
  year: number; original_lang: string; french_path: string | null; quality: string | null;
  status: string; candidate_title: string | null; candidate_score: number | null;
  candidate_seeders: number | null; dl_hash: string | null; en_file: string | null;
  merged_file: string | null; sync_delta: number | null; sync_offset_ms: number;
  error: string | null; updated: number; poster?: string | null; progress?: string | null;
  ai_status?: string | null; ai_verdict?: string | null;
}
export interface Status {
  enabled: boolean; grab_mode: string; counts: Record<string, number>; states: string[];
}
export interface Episode {
  id: string; series_id: number; series_title: string; season: number; episode: number;
  status: string; candidate_title: string | null; candidate_score: number | null;
  candidate_seeders: number | null; quality: string | null; poster: string | null;
  sync_delta: number | null; error: string | null; progress?: string | null;
  dl_hash?: string | null; series_type?: string | null;
  ai_status?: string | null; ai_verdict?: string | null;
}
export interface TvStatus { counts: Record<string, number>; }
export interface DL {
  progress: number; dlspeed: number; eta: number; state: string;
  seeds: number; size: number; downloaded: number;
}
export interface Candidate {
  score: number; seeders: number; size: number; title: string; indexer: string;
  multi: boolean; link: string; rid: string; tried: boolean; pack?: boolean; info_url?: string | null;
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
}
export interface DashRecent {
  kind: string; title: string; langs?: string | null; poster?: string | null; ts: number;
}
export interface Dash {
  enabled: boolean; grab_mode: string; scope_series: boolean;
  movies: Record<string, number>; episodes: Record<string, number>;
  active: DashActive[]; attention: DashAttention[]; recent: DashRecent[];
  merged_24h: number; merged_7d: number;
  inflight: number | null; inflight_cap: number;
  disk: { path: string; total: number; free: number } | null;
  next_runs: Record<string, number>; now: number;
}

async function j<T>(url: string, opts?: RequestInit): Promise<T> {
  const r = await fetch(url, {
    headers: { "Content-Type": "application/json" }, ...opts,
  });
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
  scan: () => j<{ found: number }>("/api/scan", { method: "POST" }),
  search: (id: number) => j<Movie>(`/api/movie/${id}/search`, { method: "POST" }),
  merge: (id: number) => j<Movie>(`/api/movie/${id}/merge`, { method: "POST" }),
  sync: (id: number, offset_ms: number) =>
    j<Movie>(`/api/movie/${id}/sync`, { method: "POST", body: JSON.stringify({ offset_ms }) }),
  retry: (id: number) => j<{ ok: boolean }>(`/api/movie/${id}/retry`, { method: "POST" }),
  ignore: (id: number) => j<{ ok: boolean }>(`/api/movie/${id}/ignore`, { method: "POST" }),
  unignore: (id: number) => j<{ ok: boolean }>(`/api/movie/${id}/unignore`, { method: "POST" }),
  research: (id: number) => j<Movie>(`/api/movie/${id}/research`, { method: "POST" }),
  aiSend: (id: number) => j<{ ok: boolean; queued: boolean }>(`/api/movie/${id}/ai`, { method: "POST" }),
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
  tvScan: () => j<{ found: number }>("/api/tv/scan", { method: "POST" }),
  tvRetryErrors: () => j<{ ok: boolean; retried: number }>("/api/tv/retry_errors", { method: "POST" }),
  epRetry: (id: string) =>
    j<{ ok: boolean }>(`/api/episode/${encodeURIComponent(id)}/retry`, { method: "POST" }),
  epIgnore: (id: string) =>
    j<{ ok: boolean }>(`/api/episode/${encodeURIComponent(id)}/ignore`, { method: "POST" }),
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
