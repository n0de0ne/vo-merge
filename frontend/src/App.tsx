import { useEffect, useState } from "react";
import { api, setApiKey, type Status } from "./api";
import { useErrorSink, usePoll, useStored } from "./lib/poll";
import { go, useRoute } from "./lib/router";
import Overview from "./pages/Overview";
import Films from "./pages/Films";
import Series from "./pages/Series";
import Library from "./pages/Library";
import Activity from "./pages/Activity";
import Problems from "./pages/Problems";
import Settings from "./pages/Settings";
import System from "./pages/System";

/* ======================================================================
   The shell: a persistent left rail, a thin top bar carrying global state,
   and a hash-routed page.

   The rail exists for the reason the *arrs have one: this app has a fixed
   set of destinations and you move between them constantly (see a failure
   → check the queue → look at the file → change a setting). A row of tabs
   made every destination equal, had nowhere to put a count, and — because
   the tab lived in component state — meant no page could be linked to and
   a reload always dumped you back on the Overview.
   ====================================================================== */

interface NavDef { id: string; label: string; icon: string; group: string; }

const NAV: NavDef[] = [
  { id: "", label: "Overview", icon: "▦", group: "Library" },
  { id: "films", label: "Films", icon: "🎬", group: "Library" },
  { id: "series", label: "TV Shows", icon: "📺", group: "Library" },
  { id: "anime", label: "Anime", icon: "🎌", group: "Library" },
  { id: "library", label: "All files", icon: "🗂", group: "Library" },
  { id: "activity", label: "Queue", icon: "⏱", group: "Activity" },
  { id: "activity/history", label: "History", icon: "🕘", group: "Activity" },
  { id: "problems", label: "Problems", icon: "⚠️", group: "Manage" },
  { id: "settings", label: "Settings", icon: "⚙️", group: "Manage" },
  { id: "system", label: "System", icon: "🩺", group: "Manage" },
];

const TITLES: Record<string, string> = {
  "": "Overview", films: "Films", series: "TV Shows", anime: "Anime",
  library: "All files", activity: "Queue", "activity/history": "History",
  problems: "Problems", settings: "Settings", system: "System",
};

/** Global brake, reachable from every page. Pausing stops NEW searches, grabs and merges;
 *  anything already merging finishes — killing mkvmerge mid-write would leave a corrupt library
 *  file — so the bar says how many are still in flight. */
function PauseControl({ st, onDone }: { st: Status | null; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  if (!st) return null;
  const toggle = async () => {
    setBusy(true);
    try { await api.pause(!st.paused); onDone(); } finally { setBusy(false); }
  };
  return (
    <>
      {st.paused
        ? <span className="badge paused">⏸ paused
            {!!st.merging_now && <> · {st.merging_now} merge(s) finishing</>}</span>
        : st.hold === "scanning" && <span className="badge">⏳ scan running — grabs held</span>}
      <button className="btn sec" disabled={busy} onClick={toggle}
        title={st.paused
          ? "Resume searches, grabs and merges"
          : "Stop starting new searches, grabs and merges. Work already running finishes; "
            + "scans keep going."}>
        {st.paused ? "▶ Resume" : "⏸ Pause"}
      </button>
    </>
  );
}

/** Health, in the top bar, because a dependency being down explains every empty list below it.
 *  `deps_down` is the watchdog's memory of DURATION, which the per-cycle logs could never say. */
function Health({ st }: { st: Status | null }) {
  const down = Object.keys(st?.deps_down || {});
  const lowDisk = st?.disk?.low;
  if (!st || (!down.length && !lowDisk)) return null;
  const bits = [
    ...down.map(d => `${d} unreachable for ${Math.round((st.deps_down![d] || 0) / 60)}m`),
    ...(lowDisk ? [`disk below the floor${st.disk?.free_gb != null
      ? ` (${st.disk.free_gb.toFixed(0)} GB free)` : ""} — merges held`] : []),
  ];
  return (
    <button className="badge bad" onClick={() => go("system")} title={bits.join("\n")}>
      ⚠ {bits.length === 1 ? bits[0] : `${bits.length} problems`}
    </button>
  );
}

/** Asks for the API key when the server has one set. Shown instead of the app, since nothing can
 *  load without it. */
function ApiKeyGate({ onDone }: { onDone: () => void }) {
  const [key, setKey] = useState("");
  const [checking, setChecking] = useState(false);
  const [bad, setBad] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setChecking(true); setBad(false);
    setApiKey(key.trim());
    try { await api.status(); onDone(); }
    catch { setApiKey(""); setBad(true); }
    finally { setChecking(false); }
  }
  return (
    <div className="page" style={{ maxWidth: 460, margin: "60px auto" }}>
      <div className="panel">
        <div className="section-title" style={{ marginTop: 0 }}>API key required</div>
        <p className="muted" style={{ marginTop: 0 }}>
          This server has <code>api_key</code> set. Enter it to continue — it is kept in this
          browser only.
        </p>
        <form onSubmit={submit} style={{ display: "flex", gap: 8 }}>
          <input type="password" autoFocus value={key} placeholder="API key"
            onChange={e => setKey(e.target.value)} style={{ flex: 1 }} />
          <button className="btn" disabled={checking || !key.trim()}>
            {checking ? "checking…" : "Unlock"}</button>
        </form>
        {bad && <div className="err" style={{ marginTop: 10 }}>That key was rejected.</div>}
      </div>
    </div>
  );
}

export default function App() {
  const route = useRoute();
  const [err, setErr] = useErrorSink();
  const [locked, setLocked] = useState(false);
  const [st, setSt] = useState<Status | null>(null);
  const [problems, setProblems] = useState(0);
  const [collapsed, setCollapsed] = useStored("vo.rail", false);
  const [drawer, setDrawer] = useState(false);

  usePoll(() => api.status().then(setSt).catch(() => {}), 5000);
  // The Problems badge is the one number worth carrying on every page: it is the only count that
  // means "you have work", as opposed to "the machine has work".
  usePoll(() => api.problems().then(p => setProblems(p.total)).catch(() => {}), 30000);

  // A 401 clears the stored key and reports "__auth__", so a rotated key prompts again instead
  // of leaving every panel silently empty.
  useEffect(() => { if (err === "__auth__") { setLocked(true); setErr(null); } }, [err, setErr]);
  useEffect(() => { setDrawer(false); }, [route.hash]);

  if (locked) return <ApiKeyGate onDone={() => { setLocked(false); setErr(null); }} />;

  const path = route.path.join("/");
  const active = NAV.slice().sort((a, b) => b.id.length - a.id.length)
    .find(n => n.id === path || (n.id && path.startsWith(n.id + "/")))?.id ?? "";
  const groups = [...new Set(NAV.map(n => n.group))];

  const page = () => {
    const head = route.path[0] ?? "";
    switch (head) {
      case "": return <Overview />;
      case "films": return <Films />;
      case "series": return <Series anime={false} key="series" />;
      case "anime": return <Series anime key="anime" />;
      case "library": return <Library />;
      case "activity": return <Activity tab={route.path[1] === "history" ? "history" : "queue"} />;
      case "problems": return <Problems code={route.path[1]} />;
      case "settings": return <Settings section={route.path[1]} />;
      case "system": return <System />;
      default:
        return <div className="panel">
          <b>Nothing here.</b>
          <div className="muted" style={{ marginTop: 6 }}>
            <code>#/{path}</code> is not a page.{" "}
            <a href="#/" onClick={() => go("")}>Back to the overview</a>.</div>
        </div>;
    }
  };

  return (
    <div className={"shell" + (collapsed ? " collapsed" : "")}>
      <nav className={"rail" + (drawer ? " open" : "")} aria-label="Sections">
        <div className="rail-brand">
          <span className="logo">🎬</span><b>VO Merger</b>
        </div>
        <div className="rail-nav">
          {groups.map(g => (
            <div key={g}>
              <div className="navgroup">{g}</div>
              {NAV.filter(n => n.group === g).map(n => {
                const badge = n.id === "problems" && problems > 0 ? problems : null;
                return (
                  <a key={n.id} className={"navitem" + (active === n.id ? " active" : "")}
                    href={"#/" + n.id} title={collapsed ? n.label : undefined}
                    aria-current={active === n.id ? "page" : undefined}>
                    <span className="ico" aria-hidden="true">{n.icon}</span>
                    <span className="lbl">{n.label}</span>
                    {badge != null && <span className="n bad">{badge}</span>}
                  </a>
                );
              })}
            </div>
          ))}
        </div>
        <div className="rail-foot">
          <button className="btn ghost small" style={{ width: "100%" }}
            onClick={() => setCollapsed(!collapsed)}
            title={collapsed ? "Expand the sidebar" : "Collapse the sidebar"}>
            {collapsed ? "»" : "« collapse"}
          </button>
        </div>
      </nav>

      <div className="main">
        <header className="topbar">
          <button className="btn ghost small railbtn"
            onClick={() => setDrawer(d => !d)} aria-label="Menu">☰</button>
          <h1>{TITLES[active] ?? "VO Merger"}</h1>
          <div className="spacer" />
          <Health st={st} />
          <PauseControl st={st} onDone={() => api.status().then(setSt).catch(() => {})} />
        </header>

        <div className="page">
          {err && (
            <div className="errbar" role="alert">
              <span>{err}</span>
              <button className="btn small" onClick={() => setErr(null)}>dismiss</button>
            </div>
          )}
          {st && !st.enabled && (
            <div className="panel warnbar">
              ⏸ The pipeline is <b style={{ margin: "0 4px" }}>disabled</b> — nothing will be
              searched, grabbed or merged.
              <button className="btn sec" style={{ marginLeft: 10 }}
                onClick={() => go("settings/general")}>Settings</button>
            </div>
          )}
          {page()}
        </div>
      </div>
    </div>
  );
}
