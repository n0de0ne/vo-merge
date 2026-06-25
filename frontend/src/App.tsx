import { useEffect, useState } from "react";
import { api, Movie, Status } from "./api";

const STATES = ["pending","searching","no_release","grabbed","downloading",
  "ready","merging","merged","sync_fail","error","ignored"];

function Pill({ s }: { s: string }) {
  return <span className={`pill ${s}`}>{s.replace("_", " ")}</span>;
}

// ---------------- Dashboard ----------------
function Dashboard() {
  const [status, setStatus] = useState<Status | null>(null);
  const [movies, setMovies] = useState<Movie[]>([]);
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    setStatus(await api.status());
    setMovies(await api.movies(filter || undefined));
  }
  useEffect(() => { refresh(); const t = setInterval(refresh, 8000); return () => clearInterval(t); }, [filter]);

  async function act(fn: () => Promise<any>) { setBusy(true); try { await fn(); } finally { setBusy(false); refresh(); } }

  return (
    <>
      <div className="panel">
        <div className="row">
          <div><b>Pipeline</b> {status?.enabled
            ? <span className="ok">enabled</span> : <span className="muted">disabled</span>}
            {status && <span className="muted"> · grab: {status.grab_mode}</span>}</div>
          <div className="spacer" />
          <button className="btn" disabled={busy} onClick={() => act(api.scan)}>Scan now</button>
        </div>
        <div className="chips" style={{ marginTop: 14 }}>
          {STATES.map(s => (
            <span className="chip" key={s}>{s.replace("_"," ")} <b>{status?.counts[s] ?? 0}</b></span>
          ))}
        </div>
      </div>

      <div className="panel">
        <div className="row" style={{ marginBottom: 10 }}>
          <select value={filter} onChange={e => setFilter(e.target.value)}>
            <option value="">all states</option>
            {STATES.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
          <span className="muted">{movies.length} movies</span>
        </div>
        <table>
          <thead><tr>
            <th>Title</th><th>Status</th><th>Candidate</th><th>Quality</th><th>Actions</th>
          </tr></thead>
          <tbody>
            {movies.map(m => (
              <tr key={m.tmdb_id}>
                <td>{m.title}<div className="sub">→ {m.original_title} ({m.year}) · {m.original_lang}</div>
                  {m.error && <div className="sub bad">{m.error}</div>}</td>
                <td><Pill s={m.status} />{m.sync_delta != null && m.status === "sync_fail" &&
                  <div className="sub">Δ {m.sync_delta.toFixed(1)}s</div>}</td>
                <td>{m.candidate_title
                  ? <>{m.candidate_title}<div className="sub">score {m.candidate_score} · {m.candidate_seeders}s</div></>
                  : <span className="muted">—</span>}</td>
                <td className="muted">{m.quality || "—"}</td>
                <td><div className="row">
                  {["pending","no_release","error"].includes(m.status) &&
                    <button className="btn sec" disabled={busy} onClick={() => act(() => api.search(m.tmdb_id))}>Search</button>}
                  {["no_release","error"].includes(m.status) &&
                    <button className="btn sec" disabled={busy} onClick={() => act(() => api.retry(m.tmdb_id))}>Retry</button>}
                  {m.status === "merged" &&
                    <button className="btn sec" disabled={busy} onClick={() => act(() => api.sync(m.tmdb_id, 0))}>Re-sync</button>}
                  {m.status === "sync_fail" &&
                    <button className="btn sec" disabled={busy} onClick={() => act(() => api.sync(m.tmdb_id, 0))}>Re-try sync</button>}
                  {m.status !== "ignored" && m.status !== "merged" &&
                    <button className="btn sec" disabled={busy} onClick={() => act(() => api.ignore(m.tmdb_id))}>Ignore</button>}
                </div></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

// ---------------- Sync failures ----------------
function SyncFailures() {
  const [movies, setMovies] = useState<Movie[]>([]);
  const [offset, setOffset] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState(false);
  async function refresh() { setMovies(await api.movies("sync_fail")); }
  useEffect(() => { refresh(); }, []);
  async function run(id: number, ms: number) {
    setBusy(true);
    try { await api.sync(id, ms); } finally { setBusy(false); refresh(); }
  }
  return (
    <div className="panel">
      <p className="muted">Releases the matcher couldn't align (framerate differs, or a likely different
        cut). <b>Auto-match</b> re-runs the video scene-cut matcher (it can rescue runtime-delta cases);
        or enter a manual ms offset (+ delays the added track, − advances it) and apply.</p>
      <table>
        <thead><tr><th>Title</th><th>Δ / reason</th><th>Offset (ms)</th><th></th></tr></thead>
        <tbody>
          {movies.map(m => (
            <tr key={m.tmdb_id}>
              <td>{m.title}<div className="sub">{m.original_title} ({m.year})</div></td>
              <td>{m.sync_delta != null ? "Δ " + m.sync_delta.toFixed(2) + "s" : "—"}
                {m.error && <div className="sub bad">{m.error}</div>}</td>
              <td><input className="sync" type="number" value={offset[m.tmdb_id] ?? ""}
                placeholder="0" onChange={e => setOffset({ ...offset, [m.tmdb_id]: e.target.value })} /></td>
              <td><div className="row">
                <button className="btn" disabled={busy} onClick={() => run(m.tmdb_id, 0)}>Auto-match</button>
                <button className="btn sec" disabled={busy} onClick={() => run(m.tmdb_id, parseInt(offset[m.tmdb_id] || "0", 10) || 0)}>Apply offset</button>
              </div></td>
            </tr>
          ))}
          {movies.length === 0 && <tr><td colSpan={4} className="muted">No sync failures 🎉</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

// ---------------- Settings ----------------
const SECRET_BOOLS = ["prowlarr_key", "radarr_key", "plex_token"];
function Settings() {
  const [cfg, setCfg] = useState<Record<string, any> | null>(null);
  const [changed, setChanged] = useState<Record<string, any>>({});
  const [tests, setTests] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);
  useEffect(() => { api.settings().then(setCfg); }, []);
  if (!cfg) return <div className="panel">loading…</div>;

  const val = (k: string) => (k in changed ? changed[k] : cfg[k]);
  const set = (k: string, v: any) => { setChanged({ ...changed, [k]: v }); setSaved(false); };

  async function save() {
    const data: Record<string, any> = {};
    for (const k of Object.keys(changed)) {
      const v = changed[k];
      if (k === "qb_pass" && v === "********") continue;
      if (SECRET_BOOLS.includes(k) && typeof v === "boolean") continue;
      data[k] = v;
    }
    if ("en_indexer_ids" in data && typeof data.en_indexer_ids === "string")
      data.en_indexer_ids = data.en_indexer_ids.split(",").map((x: string) => parseInt(x.trim(), 10)).filter((n: number) => !isNaN(n));
    await api.saveSettings(data); setSaved(true); setChanged({});
    api.settings().then(setCfg);
  }
  async function test(which: string) {
    setTests({ ...tests, [which]: "…" });
    const r = await api.test(which);
    setTests({ ...tests, [which]: r.ok ? "ok" : (r.error || "failed") });
  }

  const Text = (k: string, type = "text") =>
    <input type={type} value={val(k) ?? ""} onChange={e => set(k, type === "number" ? Number(e.target.value) : e.target.value)} />;
  const Secret = (k: string) =>
    <input type="password" placeholder={cfg[k] ? "•••••• (saved)" : "not set"}
      value={k in changed ? changed[k] : ""} onChange={e => set(k, e.target.value)} />;
  const Check = (k: string) =>
    <input type="checkbox" checked={!!val(k)} onChange={e => set(k, e.target.checked)} />;
  const TestBtn = (k: string) =>
    <button className="btn sec" onClick={() => test(k)}>Test {tests[k] &&
      <span className={tests[k] === "ok" ? "ok" : "bad"}> {tests[k]}</span>}</button>;

  return (
    <div className="panel">
      <div className="section-title">Integrations</div>
      <div className="form-grid">
        <label>Prowlarr URL</label>{Text("prowlarr_url")}{TestBtn("prowlarr")}
        <label>Prowlarr API key</label>{Secret("prowlarr_key")}<span />
        <label>Radarr URL</label>{Text("radarr_url")}{TestBtn("radarr")}
        <label>Radarr API key</label>{Secret("radarr_key")}<span />
        <label>qBittorrent URL</label>{Text("qb_url")}{TestBtn("qb")}
        <label>qB username</label>{Text("qb_user")}<span />
        <label>qB password</label><input type="password" placeholder={cfg.qb_pass ? "•••••• (saved)" : "not set"}
          value={"qb_pass" in changed ? changed.qb_pass : ""} onChange={e => set("qb_pass", e.target.value)} /><span />
        <label>Plex URL</label>{Text("plex_url")}{TestBtn("plex")}
        <label>Plex token</label>{Secret("plex_token")}<span />
      </div>

      <div className="section-title">Pipeline</div>
      <div className="form-grid">
        <label>English indexer IDs</label>
        <input type="text" value={Array.isArray(val("en_indexer_ids")) ? val("en_indexer_ids").join(", ") : val("en_indexer_ids")}
          onChange={e => set("en_indexer_ids", e.target.value)} /><span />
        <label>vo-gap tag</label>{Text("vo_gap_tag")}<span />
        <label>qB category</label>{Text("qb_category")}<span />
        <label>Score threshold</label>{Text("score_threshold", "number")}<span />
        <label>Min seeders</label>{Text("min_seeders", "number")}<span />
        <label>Grab mode</label>
        <select value={val("grab_mode")} onChange={e => set("grab_mode", e.target.value)}>
          <option value="auto">auto</option><option value="approval">approval</option>
        </select><span />
        <label>Sync tolerance (s)</label>{Text("sync_tolerance_s", "number")}<span />
        <label>Search interval (min)</label>{Text("search_interval_min", "number")}<span />
        <label>Finish interval (min)</label>{Text("finish_interval_min", "number")}<span />
        <label>Films</label>{Check("scope_films")}<span />
        <label>Series/Anime</label>{Check("scope_series")}<span />
        <label>Exclude French-origin</label>{Check("exclude_french_origin")}<span />
      </div>

      <div className="section-title">Master</div>
      <div className="form-grid">
        <label>Pipeline enabled</label>{Check("enabled")}<span />
      </div>

      <div className="row" style={{ marginTop: 18 }}>
        <button className="btn" onClick={save}>Save</button>
        {saved && <span className="ok">saved ✓</span>}
      </div>
    </div>
  );
}

// ---------------- Logs ----------------
function Logs() {
  const [lines, setLines] = useState<string[]>([]);
  const [auto, setAuto] = useState(true);
  async function refresh() { setLines((await api.logs()).lines); }
  useEffect(() => { refresh(); if (!auto) return; const t = setInterval(refresh, 5000); return () => clearInterval(t); }, [auto]);
  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 10 }}>
        <button className="btn sec" onClick={refresh}>Refresh</button>
        <label className="muted"><input type="checkbox" checked={auto} onChange={e => setAuto(e.target.checked)} /> auto</label>
      </div>
      <pre className="logs">{lines.join("")}</pre>
    </div>
  );
}

// ---------------- App ----------------
export default function App() {
  const [tab, setTab] = useState("dashboard");
  const tabs: [string, string][] = [
    ["dashboard", "Dashboard"], ["sync", "Sync failures"], ["settings", "Settings"], ["logs", "Logs"]];
  return (
    <div className="app">
      <header className="top">
        <h1>🎬 VO Merger</h1>
        <span className="badge">multi-language library builder</span>
      </header>
      <nav>
        {tabs.map(([k, label]) =>
          <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>{label}</button>)}
      </nav>
      {tab === "dashboard" && <Dashboard />}
      {tab === "sync" && <SyncFailures />}
      {tab === "settings" && <Settings />}
      {tab === "logs" && <Logs />}
    </div>
  );
}
