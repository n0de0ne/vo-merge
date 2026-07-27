import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, Movie, Status, Episode, Candidate, DL, Dash, RescanState, Coverage, CoverageLib,
  LibItem, LibPage, RepairPlan, RepairState, AiHealth } from "./api";

const fmtTime = (s: number) => {
  s = Math.max(0, Math.floor(s)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return (h ? `${h}:` : "") + `${String(m).padStart(h ? 2 : 1, "0")}:${String(ss).padStart(2, "0")}`;
};

const fmtBytes = (b: number) => {
  if (!b || b <= 0) return "—";
  const gb = b / 1e9;
  if (gb >= 1) return gb.toFixed(2) + " GB";
  return (b / 1e6).toFixed(0) + " MB";
};

const fmtSpeed = (b: number) => (!b || b <= 0 ? "" : b / 1e6 >= 1 ? (b / 1e6).toFixed(1) + " MB/s" : (b / 1e3).toFixed(0) + " kB/s");
const fmtEta = (s: number) => (!s || s <= 0 || s >= 8640000 ? "" : "ETA " + fmtTime(s));

const pad2 = (n: number) => String(n).padStart(2, "0");

// Poll `fn` every `ms`, but ONLY while the tab is visible. Browsers throttle background-tab
// timers, so a tab left open would silently go stale (the "won't update without a reload"
// complaint). We pause while hidden and fire an immediate refresh the moment the tab is
// focused again. `deps` re-arms the loop (e.g. when a filter changes).
function usePoll(fn: () => void, ms: number, deps: any[] = []) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    let alive = true;
    const run = () => { if (alive && !document.hidden) saved.current(); };
    run();
    const id = setInterval(run, ms);
    const onVis = () => { if (!document.hidden) run(); };   // instant refresh on return
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("focus", onVis);
    return () => {
      alive = false; clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
      window.removeEventListener("focus", onVis);
    };
    /* eslint-disable-next-line */
  }, [ms, ...deps]);
}

// tiny "this view auto-refreshes" indicator
function LiveDot() {
  return <span className="livedot" title="Auto-refreshing while this tab is visible">live</span>;
}

// small secondary pill for the AI-review status of an item (orthogonal to the pipeline status)
const AI_LABEL: Record<string, string> = {
  pending: "🤖 AI working", resolved: "🤖 AI resolved",
  failed: "🤖 AI couldn't fix", needs_human: "🤖 needs you",
};
function AiPill({ s }: { s?: string | null }) {
  if (!s) return null;
  return <span className={`pill ai_${s}`}>{AI_LABEL[s] ?? s}</span>;
}

// human label for a qB torrent state (distinguishes queued from genuinely stalled)
const DL_LABEL: Record<string, string> = {
  metaDL: "fetching metadata", queuedDL: "queued", stalledDL: "stalled",
  checkingDL: "checking", pausedDL: "paused", allocating: "allocating", moving: "moving",
};
// Live download progress bar for an item still in qB. `dl` comes from /api/downloads.
function DownloadBar({ dl }: { dl?: DL }) {
  if (!dl) return null;
  const pct = Math.round((dl.progress || 0) * 100);
  const st = dl.state || "";
  const active = (dl.dlspeed || 0) > 0;
  let label = active ? fmtSpeed(dl.dlspeed) : pct >= 100 ? "done" : (DL_LABEL[st] ?? "waiting");
  const meta = [label, active ? fmtEta(dl.eta) : "", `${dl.seeds || 0} seeds`].filter(Boolean).join(" · ");
  const cls = active ? "live" : st === "stalledDL" ? "stalled" : st === "queuedDL" ? "queued" : "";
  return (
    <div className="dlbar" title={`${st} · ${pct}% of ${fmtBytes(dl.size)}`}>
      <div className="dlbar-track"><div className={"dlbar-fill " + cls} style={{ width: pct + "%" }} /></div>
      <div className="dlbar-meta">{pct}% · {meta}</div>
    </div>
  );
}

// Per-row action menu: a native <details> dropdown so a row never sprawls into many button
// lines. Children are the action buttons; clicking any one closes the menu.
function RowMenu({ children, label = "Actions" }: { children: ReactNode; label?: string }) {
  const ref = useRef<HTMLDetailsElement>(null);
  return (
    <details className="rowmenu" ref={ref}>
      <summary className="btn sec">{label} ▾</summary>
      <div className="menu-pop" onClick={() => ref.current?.removeAttribute("open")}>{children}</div>
    </details>
  );
}

// poster thumbnail at the start of a row; neutral box if missing
function Poster({ src, alt }: { src?: string | null; alt: string }) {
  return src
    ? <img className="poster" src={src} alt={alt} loading="lazy" />
    : <div className="poster placeholder" aria-label="no poster">🎞</div>;
}

// ---------------- Sync Editor (stable video + Web Audio live offset + waveform) ----------------
function SyncEditor({ movie, onClose }: { movie: Movie; onClose: () => void }) {
  const [d, setD] = useState<{ video: string; audio: string; start: number; fps: number; duration: number; movie_dur: number } | null>(null);
  // start at 0: the preview already reflects the CURRENT file; Apply adds this delta on top
  const [offset, setOffset] = useState(0);
  const [previewT, setPreviewT] = useState(-1);     // movie position of the preview window (-1 = auto)
  const [peaks, setPeaks] = useState<number[] | null>(null);
  const [vt, setVt] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("loading preview…");
  const v = useRef<HTMLVideoElement>(null);
  const cv = useRef<HTMLCanvasElement>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const bufRef = useRef<AudioBuffer | null>(null);
  const srcRef = useRef<AudioBufferSourceNode | null>(null);
  const offRef = useRef(offset);
  offRef.current = offset;
  const fps = d?.fps || 23.976, dur = d?.duration || 20;

  async function load(t = previewT) {
    setMsg("loading preview…"); setPeaks(null);
    try { srcRef.current?.stop(); ctxRef.current?.close(); } catch {}
    try {
      const dd = await api.preview(movie.tmdb_id, "eng", t); setD(dd); setMsg("decoding audio…");
      const buf = await (await fetch(dd.audio)).arrayBuffer();
      const ac = new (window.AudioContext || (window as any).webkitAudioContext)();
      const ab = await ac.decodeAudioData(buf);
      ctxRef.current = ac; bufRef.current = ab;
      const ch = ab.getChannelData(0), W = 1200, block = Math.max(1, Math.floor(ch.length / W)), pk: number[] = [];
      for (let i = 0; i < W; i++) { let m = 0; for (let j = 0; j < block; j++) { const a = Math.abs(ch[i * block + j] || 0); if (a > m) m = a; } pk.push(m); }
      setPeaks(pk); setMsg("");
    } catch (e: any) { setMsg("preview failed: " + e.message); }
  }
  useEffect(() => {
    load();
    return () => { try { srcRef.current?.stop(); ctxRef.current?.close(); } catch {} };
    /* eslint-disable-next-line */
  }, []);
  // jump the preview window anywhere in the movie (debounced re-generate)
  useEffect(() => {
    if (previewT < 0) return;
    const h = setTimeout(() => load(previewT), 450);
    return () => clearTimeout(h);
    /* eslint-disable-next-line */
  }, [previewT]);

  // (re)start the Web Audio source aligned to the video at the current offset (no reload)
  function startAudio() {
    const ac = ctxRef.current, ab = bufRef.current, vv = v.current;
    if (!ac || !ab || !vv) return;
    try { srcRef.current?.stop(); } catch {}
    ac.resume();
    const s = ac.createBufferSource(); s.buffer = ab; s.connect(ac.destination); s.loop = false;
    const pos = vv.currentTime - offRef.current / 1000;     // audio position for this frame
    if (pos >= 0 && pos < ab.duration) s.start(0, pos); else s.start(0, 0);
    srcRef.current = s;
  }
  function stopAudio() { try { srcRef.current?.stop(); } catch {} srcRef.current = null; }

  // video drives everything; audio follows. Re-align on play/seek/loop and offset change.
  useEffect(() => {
    const vv = v.current; if (!vv) return;
    const onPlay = () => { setPlaying(true); startAudio(); };
    const onPause = () => { setPlaying(false); stopAudio(); };
    const onSeek = () => { if (!vv.paused) startAudio(); };
    vv.addEventListener("play", onPlay); vv.addEventListener("pause", onPause); vv.addEventListener("seeked", onSeek);
    return () => { vv.removeEventListener("play", onPlay); vv.removeEventListener("pause", onPause); vv.removeEventListener("seeked", onSeek); };
  }, [d]);
  useEffect(() => { if (playing) startAudio(); /* eslint-disable-next-line */ }, [offset]); // live offset, no reload

  useEffect(() => {                                  // track time + correct audio drift / loop
    let raf = 0; const tick = () => {
      const vv = v.current; if (vv) { setVt(vv.currentTime); }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick); return () => cancelAnimationFrame(raf);
  }, []);

  useEffect(() => {                                  // waveform shifted by offset + playhead
    const c = cv.current; if (!c || !peaks) return;
    const ctx = c.getContext("2d")!, W = c.width, H = c.height;
    ctx.fillStyle = "#0c0f14"; ctx.fillRect(0, 0, W, H);
    const dx = (offset / 1000 / dur) * W; ctx.fillStyle = "#4f9cf9";
    for (let i = 0; i < peaks.length; i++) { const x = (i / peaks.length) * W + dx, h = peaks[i] * H * 0.95; ctx.fillRect(x, (H - h) / 2, 1, h); }
    const px = (vt / dur) * W;
    ctx.strokeStyle = "#ff5252"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke();
  }, [peaks, offset, vt, dur]);

  const frame = Math.round(1000 / fps);
  const step = (df: number) => { const vv = v.current; if (vv) { vv.pause(); vv.currentTime = Math.max(0, vv.currentTime + df / 1000); } };
  async function apply() {
    setBusy(true); stopAudio();
    try {
      await api.applyOffset(movie.tmdb_id, offset, "eng");
      setOffset(0); await load();           // reload the now-modified file so you hear the result
      setMsg("applied ✓ — preview updated; press play to confirm");
    } catch (e: any) { setMsg("apply failed: " + e.message); }
    finally { setBusy(false); }
  }

  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()} style={{ width: "min(900px,94vw)" }}>
        <div className="row"><b>Tune sync — {movie.title}</b><div className="spacer" />
          <button className="btn sec" onClick={onClose}>✕</button></div>
        <p className="muted">Press play — the English audio plays through the slider <b>live</b> (the video never reloads).
          For precision: step to the <b>moment of impact</b> and slide until the audio <b>spike</b> sits under the red
          playhead. Sound too early → spike is <b>left</b> of the line → push toward <b>+</b>.</p>
        {msg && <div className="muted">{msg}</div>}
        {d && <>
          <video ref={v} src={d.video} muted loop playsInline controls
            style={{ width: "100%", maxHeight: "46vh", borderRadius: 8, background: "#000" }} />
          <div className="row" style={{ margin: "8px 0 2px" }}>
            <span className="muted" style={{ width: 46 }}>scene:</span>
            <input type="range" min={0} max={d.movie_dur || 0} step={1}
              value={previewT < 0 ? d.start : previewT}
              onChange={e => setPreviewT(Number(e.target.value))} style={{ flex: 1 }} />
            <span className="muted" style={{ width: 120, textAlign: "right" }}>
              {fmtTime(previewT < 0 ? d.start : previewT)} / {fmtTime(d.movie_dur)}</span>
          </div>
          <canvas ref={cv} width={1200} height={110}
            style={{ width: "100%", height: 110, borderRadius: 8, marginTop: 10, border: "1px solid var(--border)" }} />
          <div className="row" style={{ margin: "6px 0 12px" }}>
            <span className="muted">video:</span>
            <button className="btn sec" onClick={() => step(-frame)}>◀ frame</button>
            <button className="btn sec" onClick={() => step(frame)}>frame ▶</button>
            <span className="muted">@ {(d.start + vt).toFixed(2)}s</span>
          </div>
          <div className="row" style={{ marginBottom: 10 }}>
            <button className="btn sec" onClick={() => setOffset(o => o - frame)}>−1 frame</button>
            <button className="btn sec" onClick={() => setOffset(o => o - 5)}>−5ms</button>
            <input type="range" min={-1500} max={1500} step={1} value={offset}
              onChange={e => setOffset(Number(e.target.value))} style={{ flex: 1 }} />
            <button className="btn sec" onClick={() => setOffset(o => o + 5)}>+5ms</button>
            <button className="btn sec" onClick={() => setOffset(o => o + frame)}>+1 frame</button>
          </div>
          <div className="row">
            <b style={{ width: 110 }}>{offset >= 0 ? "+" : ""}{offset} ms</b>
            <span className="muted">{offset >= 0 ? "audio later" : "audio earlier"} · 1 frame ≈ {frame}ms</span>
            <div className="spacer" />
            <button className="btn" disabled={busy} onClick={apply}>Apply {offset >= 0 ? "+" : ""}{offset}ms</button>
          </div>
        </>}
      </div>
    </div>
  );
}

const STATES = ["pending","searching","no_release","grabbed","downloading",
  "ready","merging","merged","review","sync_fail","error","ignored"];

// `ready` means "download finished, waiting its turn to merge" — say that, don't say "ready"
const STATE_LABEL: Record<string, string> = { ready: "queued" };

function Pill({ s }: { s: string }) {
  return <span className={`pill ${s}`}>{STATE_LABEL[s] ?? s.replace("_", " ")}</span>;
}

// ordinal for the merge-queue position: 1 -> "next up", 2 -> "2nd in line", …
const queueLabel = (pos?: number | null) => {
  if (!pos) return "queued for merge";
  if (pos === 1) return "next up to merge";
  const s = ["th", "st", "nd", "rd"][(pos % 100 - 20) % 10] || ["th", "st", "nd", "rd"][pos % 100] || "th";
  return `queued for merge · ${pos}${s} in line`;
};

function QueuedLine({ pos }: { pos?: number | null }) {
  return <div className="sub queued">⏳ {queueLabel(pos)}</div>;
}

// a rate stretch was applied (PAL 25fps vs 23.976 etc) — worth showing, it's not a plain offset
function DriftBadge({ d }: { d?: number | null }) {
  if (!d || Math.abs(d - 1) < 1e-6) return null;
  const pct = (d - 1) * 100;
  return <span className="drift" title={`audio time-stretched ${pct.toFixed(2)}% to match the video's rate`}>
    ⏩ rate ×{d.toFixed(4)} ({pct > 0 ? "+" : ""}{pct.toFixed(2)}%)</span>;
}

// What the library file actually contains, read off the file itself (not from Radarr/Sonarr
// metadata), plus what it's still missing. `needs` is "", "audio", "subs" or "audio+subs".
function Tracks({ a, s, na, ns }:
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

// Re-read every library file with mkvmerge and re-decide the gaps. Covers films, series and
// anime in one pass regardless of the enabled scopes, and takes minutes on a big library — so
// it reports live progress rather than looking hung.
function RescanButton({ scope, label, primary }:
  { scope: "all" | "films" | "anime" | "series"; label: string; primary?: boolean }) {
  const [st, setSt] = useState<RescanState | null>(null);
  const [busy, setBusy] = useState(false);
  usePoll(() => api.rescanState().then(setSt).catch(() => {}), st?.running ? 3000 : 30000, [st?.running]);

  async function go() {
    setBusy(true);
    try { await api.rescan(scope, true); await api.rescanState().then(setSt); }
    finally { setBusy(false); }
  }
  // one scan runs at a time (SCAN_LOCK), so a pass started from another tab disables this one
  const mine = st?.scope === scope;
  const running = !!st?.running;
  const done = mine && st && !running && st.finished > 0;
  const found = scope === "films" ? st?.films
    : scope === "all" ? (st?.films ?? 0) + (st?.episodes ?? 0) : st?.episodes;
  const dropped = (st?.pruned ?? 0) + (st?.pruned_records ?? 0);
  return (
    <>
      <button className={primary ? "btn" : "btn sec"} disabled={busy || running} onClick={go}
        title={`Re-read every ${label} file with mkvmerge and re-decide what it is missing. `
               + "Every file is read again even if it looks unchanged, and files that have been "
               + "deleted from the library are dropped. Takes a few minutes on a big library."}>
        {running && mine ? "Re-reading…" : `Re-read ${label}`}
      </button>
      {running && <span className="muted">
        {mine ? `${st!.phase}…` : `busy: ${st!.scope} scan running`}</span>}
      {done && !st.error &&
        <span className="muted">last: {found ?? "?"} gap(s) · {st.probes.cached} file(s) read
          {dropped > 0 && <> · {dropped} deleted entr{dropped === 1 ? "y" : "ies"} removed</>}
          {st.probes.unreadable > 0 && <span className="bad"> · {st.probes.unreadable} unreadable</span>}</span>}
      {mine && st?.error && <span className="bad">rescan failed: {st.error}</span>}
    </>
  );
}

// How much of the library actually meets its language targets. Sourced from the probe table —
// the only complete inventory, since scan() records a movie/episode only when it HAS a gap.
const LANG_NAME: Record<string, string> = {
  fre: "French", eng: "English", jpn: "Japanese", spa: "Spanish", ger: "German",
  ita: "Italian", por: "Portuguese", rus: "Russian", kor: "Korean", zho: "Chinese",
};
const lang = (c: string) => LANG_NAME[c] ?? c.toUpperCase();

function LibBar({ l }: { l: CoverageLib }) {
  const n = Math.max(l.total, 1);
  const pct = (v: number) => (v / n) * 100;
  const seg = [
    { k: "complete", v: l.complete, cls: "ok", label: "meets target" },
    { k: "subs", v: l.missing_subs, cls: "warn", label: "missing subtitles" },
    { k: "audio", v: l.missing_audio, cls: "bad", label: "missing audio" },
    { k: "both", v: l.missing_both, cls: "bad2", label: "missing audio + subtitles" },
    { k: "unread", v: l.unreadable, cls: "unk", label: "unreadable" },
  ].filter(s => s.v > 0);
  return (
    <div className="covlib">
      <div className="covhead">
        <b>{l.name}</b>
        <span className="muted">{l.total.toLocaleString()} files · targets {l.targets.audio.join("/")} audio</span>
        <div className="spacer" />
        <span className="covpct">{Math.round(pct(l.complete))}%</span>
      </div>
      <div className="covbar" role="img"
        aria-label={`${Math.round(pct(l.complete))}% of ${l.name} meets its language target`}>
        {seg.map(s => (
          <span key={s.k} className={`seg ${s.cls}`} style={{ width: `${pct(s.v)}%` }}
            title={`${s.label}: ${s.v.toLocaleString()} (${pct(s.v).toFixed(1)}%)`} />
        ))}
      </div>
      <div className="covlegend">
        {seg.map(s => (
          <span key={s.k}><i className={`dot ${s.cls}`} />{s.label} {s.v.toLocaleString()}</span>
        ))}
      </div>
      <div className="covlangs">
        {(["audio", "subs"] as const).map(which => (
          <div className="covlangrow" key={which}>
            <span className="covlangkind">{which === "audio" ? "🔊 audio" : "💬 subs"}</span>
            {l.targets[which].map(code => {
              const have = (which === "audio" ? l.audio : l.subs)[code] ?? 0;
              const p = pct(have);
              return (
                <span className="covlang" key={code} title={`${have.toLocaleString()} of ${l.total.toLocaleString()} files have ${lang(code)} ${which}`}>
                  <span className="covlangname">{lang(code)}</span>
                  <span className="covmini"><span className={p >= 95 ? "ok" : p >= 50 ? "warn" : "bad"}
                    style={{ width: `${p}%` }} /></span>
                  <span className="covlangpct">{Math.round(p)}%</span>
                </span>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

function CoveragePanel({ goto }: { goto?: (tab: string) => void }) {
  const [c, setC] = useState<Coverage | null>(null);
  usePoll(() => api.coverage().then(setC).catch(() => {}), 60000);
  if (!c) return null;
  if (!c.probed)
    return (
      <div className="panel">
        <div className="row" style={{ marginBottom: 6 }}><b>Language coverage</b></div>
        <div className="muted">Nothing probed yet — run “Re-read everything” on the Library tab.</div>
      </div>
    );
  const pct = Math.round((c.complete / Math.max(c.probed, 1)) * 100);
  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 10 }}>
        <b>Language coverage</b>
        <span className="muted">{c.complete.toLocaleString()} of {c.probed.toLocaleString()} probed
          files meet their target · {pct}%
          {c.unreadable > 0 && <> · <span className="bad">{c.unreadable.toLocaleString()} unreadable</span></>}</span>
        {goto && <><div className="spacer" />
          <button className="btn sec" onClick={() => goto("library")}>Browse files →</button></>}
      </div>
      {c.libraries.map(l => <LibBar key={l.name} l={l} />)}
    </div>
  );
}

// ---------------- Library browser: which files meet the target, and which don't ----------------
// /coverage answers "how much" as a number; this answers "which ones", the only form you can act
// on. Both read the same probe inventory through the same classifier, so a file counted as
// complete in the chart is in the Complete list here — they cannot disagree.
const PAGE = 200;

function LibRow({ i }: { i: LibItem }) {
  // "French, English audio · English subs" — grouped by kind, not one clause per language, so a
  // file short of three things still reads as one short phrase
  const miss = [
    i.missing_audio.length ? `${i.missing_audio.map(lang).join(", ")} audio` : "",
    i.missing_subs.length ? `${i.missing_subs.map(lang).join(", ")} subs` : "",
  ].filter(Boolean);
  return (
    <div className={`librow ${i.state}`}>
      <div className="libmain">
        <div className="libtitle">{i.title}</div>
        <div className="libfile" title={i.path}>{i.file}</div>
      </div>
      <div className="libtracks">
        {i.state === "unreadable"
          ? <span className="bad" title={i.err || ""}>⚠ {i.err || "probe failed"}</span>
          : <>
              <span title="audio languages read from the file">🔊 {i.audio.map(lang).join(", ") || <span className="muted">none tagged</span>}</span>
              <span title="subtitle languages read from the file">💬 {i.subs.map(lang).join(", ") || <span className="muted">none</span>}</span>
            </>}
      </div>
      <div className="libmiss">
        {i.state === "complete"
          ? <span className="ok-txt">✓ meets target</span>
          : miss.length > 0 ? <span className="needs">missing {miss.join(" · ")}</span> : null}
      </div>
    </div>
  );
}

// A file with no audio at all can never be repaired by grafting — there is nothing to sync
// against and nothing to keep — so the only fix is a fresh copy. Deleting through Radarr/Sonarr
// (rather than unlinking) is what makes them mark it missing and search again.
//
// This deletes the operator's media, so the UI never offers a one-click destructive action: it
// asks the backend what it WOULD do, shows that, and only then offers the run.
function RepairPanel({ n, onDone }: { n: number; onDone: () => void }) {
  const [plan, setPlan] = useState<RepairPlan | null>(null);
  const [st, setSt] = useState<RepairState | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  usePoll(() => { if (st?.running) api.repairState().then(setSt).catch(() => {}); },
    3000, [st?.running]);

  async function check() {
    setBusy(true); setErr("");
    try { setPlan(await api.repairPlan()); }
    catch (e: any) { setErr(e.message || "check failed"); }
    finally { setBusy(false); }
  }
  async function run() {
    const known = plan!.total - plan!.unknown;
    if (!confirm(`Delete ${known} file(s) with no audio track and ask Radarr/Sonarr to `
      + `download a replacement?\n\nEach file is re-probed first and skipped if it turns out `
      + `to be readable. This cannot be undone.`)) return;
    setBusy(true); setErr("");
    try {
      const r = await api.repairRun();
      if (!r.started) { setErr(r.note || "could not start"); return; }
      setSt(await api.repairState());
    } catch (e: any) { setErr(e.message || "repair failed"); }
    finally { setBusy(false); }
  }

  const running = !!st?.running;
  const finished = st && !running && st.finished > 0;
  return (
    <div className="panel repair">
      <div className="row">
        <b>🔇 {n.toLocaleString()} file(s) carry no audio at all</b>
        <span className="muted">mkvmerge and ffprobe both read them and found zero audio streams,
          so there is nothing to graft into — the only fix is a fresh copy.</span>
        <div className="spacer" />
        {!plan && <button className="btn sec" disabled={busy || running} onClick={check}>
          {busy ? "Checking…" : "Check what can be replaced"}</button>}
      </div>
      {err && <div className="bad" style={{ marginTop: 6 }}>{err}</div>}
      {plan && !running && !finished && <div className="repair-plan">
        <div>
          <b>{(plan.total - plan.unknown).toLocaleString()}</b> would be deleted and re-searched
          via Radarr/Sonarr
          {plan.unknown > 0 && <> · <b>{plan.unknown.toLocaleString()}</b> skipped — not in
            Radarr/Sonarr, so deleting them would just lose the title</>}
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          <button className="btn danger" disabled={busy || plan.total === plan.unknown}
            onClick={run}>Delete {(plan.total - plan.unknown).toLocaleString()} and re-search</button>
          <button className="btn sec" onClick={() => setPlan(null)}>Cancel</button>
        </div>
      </div>}
      {running && <div className="muted" style={{ marginTop: 6 }}>
        {st!.phase} · re-probed {st!.checked} of {st!.total} · deleted {st!.deleted}</div>}
      {finished && <div className="repair-plan">
        <div><b>{st!.deleted.toLocaleString()}</b> deleted and re-searched
          {st!.skipped.length > 0 && <> · <b>{st!.skipped.length.toLocaleString()}</b> skipped</>}</div>
        {st!.skipped.slice(0, 8).map(s => (
          <div className="muted small" key={s.path}>{s.path.split("/").pop()} — {s.reason}</div>))}
        {st!.skipped.length > 8 &&
          <div className="muted small">+{st!.skipped.length - 8} more</div>}
        {st!.error && <div className="bad">{st!.error}</div>}
        <div className="row" style={{ marginTop: 8 }}>
          <button className="btn sec" onClick={() => { setSt(null); setPlan(null); onDone(); }}>
            Done</button>
        </div>
      </div>}
    </div>
  );
}

function Library() {
  const [state, setState] = useState<"incomplete" | "complete" | "unreadable">("incomplete");
  const [lib, setLib] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(0);
  const [d, setD] = useState<LibPage | null>(null);
  const [err, setErr] = useState("");

  const load = () => api.library({ state, lib, q, limit: PAGE, offset: page * PAGE })
    .then(x => { setD(x); setErr(""); })
    .catch(e => setErr(e.message || "load failed"));
  usePoll(load, 30000, [state, lib, q, page]);
  // A filter change invalidates the page number — page 4 of a 2-page result is blank. Reset it in
  // the same update as the filter (not in an effect) so the poll re-arms once, with both new
  // values, instead of firing a throwaway request at the old offset first.
  const pick = <T,>(set: (v: T) => void) => (v: T) => { set(v); setPage(0); };

  const tabs: [typeof state, string, number][] = [
    ["incomplete", "Target not met", d?.counts.incomplete ?? 0],
    ["complete", "Complete", d?.counts.complete ?? 0],
    ["unreadable", "Unreadable", d?.counts.unreadable ?? 0],
  ];
  const shown = d?.items.length ?? 0;
  const pages = Math.ceil((d?.total ?? 0) / PAGE);
  return (
    <>
      <div className="panel">
        <div className="row" style={{ marginBottom: 8 }}>
          <b>Library</b>
          <span className="muted">every file the scanner has read, scored against its language target</span>
          <div className="spacer" />
          <RescanButton scope="all" label="everything" primary />
        </div>
        <div className="row libfilters">
          <div className="segbtns">
            {tabs.map(([k, label, n]) => (
              <button key={k} className={state === k ? "active" : ""} onClick={() => pick(setState)(k)}>
                {label} <span className="segn">{n.toLocaleString()}</span>
              </button>
            ))}
          </div>
          <select value={lib} onChange={e => pick(setLib)(e.target.value)}>
            <option value="">All libraries</option>
            {(d?.libraries ?? []).map(l =>
              <option key={l.name} value={l.name}>{l.name} ({l.total.toLocaleString()})</option>)}
          </select>
          <input placeholder="filter by title or path…" value={q}
            onChange={e => pick(setQ)(e.target.value)} style={{ minWidth: 220 }} />
        </div>
        {err && <div className="bad">{err}</div>}
        {/* "unreadable" is not one problem. Only "no audio track" is a broken file; the others
            say mkvmerge couldn't parse the container, which is about our tools, not the media. */}
        {state === "unreadable" && d && Object.keys(d.error_kinds).length > 0 &&
          <div className="errkinds">
            {Object.entries(d.error_kinds).map(([k, n]) => (
              <span key={k} className={k === "no audio track" ? "ek broken" : "ek"}>
                {k} <b>{n.toLocaleString()}</b></span>))}
          </div>}
        {d && d.total === 0 && !err &&
          <div className="muted" style={{ marginTop: 10 }}>
            {d.counts.complete + d.counts.incomplete + d.counts.unreadable === 0
              ? "Nothing read yet — hit “Re-read everything” to probe the library."
              : "Nothing here matches those filters."}
          </div>}
        {d && d.total > 0 && <>
          <div className="libhead">
            <span>title</span><span>on the file</span><span>gap</span>
          </div>
          <div className="liblist">{d.items.map(i => <LibRow key={i.path} i={i} />)}</div>
          <div className="row" style={{ marginTop: 10 }}>
            <span className="muted">
              showing {(page * PAGE + 1).toLocaleString()}–{(page * PAGE + shown).toLocaleString()}
              {" "}of {d.total.toLocaleString()}</span>
            <div className="spacer" />
            <button className="btn sec" disabled={page === 0} onClick={() => setPage(p => p - 1)}>← prev</button>
            <button className="btn sec" disabled={page + 1 >= pages} onClick={() => setPage(p => p + 1)}>next →</button>
          </div>
        </>}
      </div>
      {state === "unreadable" && (d?.repairable ?? 0) > 0 &&
        <RepairPanel n={d!.repairable} onDone={load} />}
      <CoveragePanel />
    </>
  );
}

// ---------------- Interactive release search modal ----------------
function ReleaseModal({ title, load, onGrab, onClose, onGrabbed }:
  { title: string; load: () => Promise<Candidate[]>; onGrab: (c: Candidate) => Promise<any>;
    onClose: () => void; onGrabbed: () => void }) {
  const [list, setList] = useState<Candidate[] | null>(null);
  const [err, setErr] = useState("");
  const [grabbing, setGrabbing] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    load().then(c => { if (alive) setList(c); }).catch(e => { if (alive) setErr(e.message || "search failed"); });
    return () => { alive = false; };
    /* eslint-disable-next-line */
  }, []);

  async function grab(c: Candidate) {
    setGrabbing(c.rid);
    try { await onGrab(c); onGrabbed(); onClose(); }
    catch (e: any) { setErr(e.message || "grab failed"); setGrabbing(null); }
  }

  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()} style={{ width: "min(860px,94vw)" }}>
        <div className="row"><b>Releases — {title}</b><div className="spacer" />
          <button className="btn sec" onClick={onClose}>✕</button></div>
        {err && <div className="sub bad" style={{ marginTop: 8 }}>{err}</div>}
        {!list && !err && <div className="muted" style={{ marginTop: 12 }}>searching indexers… (this can take a few seconds)</div>}
        {list && list.length === 0 && <div className="muted" style={{ marginTop: 12 }}>No releases found.</div>}
        {list && list.length > 0 &&
          <div className="rel-list">
            {list.map(c => (
              <div className={"rel-row" + (c.tried ? " tried" : "")} key={c.rid}>
                <div className="rel-main">
                  <div className="rel-title">
                    {c.info_url
                      ? <a href={c.info_url} target="_blank" rel="noreferrer" title="Open tracker page"
                           style={{ color: "#9ecbff", textDecoration: "none" }}>{c.title} ↗</a>
                      : c.title}
                    {c.pack && <span className="multi-badge" style={{ background: "#14432a", color: "#5ee9a0" }}>PACK</span>}
                    {c.multi && <span className="multi-badge">MULTI</span>}
                    {c.tried && <span className="tried-mark">tried</span>}
                  </div>
                  <div className="sub">{c.indexer} · {c.seeders}s · {fmtBytes(c.size)} · score {c.score}</div>
                </div>
                <button className="btn" disabled={grabbing === c.rid} onClick={() => grab(c)}>
                  {grabbing === c.rid ? "Grabbing…" : "Grab"}
                </button>
              </div>
            ))}
          </div>}
      </div>
    </div>
  );
}

// status-appropriate actions for a movie, folded into a dropdown so rows stay compact
function MovieActions({ m, busy, act, onRelease, onTune }:
  { m: Movie; busy: boolean; act: (fn: () => Promise<any>) => void;
    onRelease: (m: Movie) => void; onTune: (m: Movie) => void }) {
  const B = (label: string, fn: () => void) =>
    <button className="btn sec" disabled={busy} onClick={fn}>{label}</button>;
  return (
    <RowMenu>
      {["pending", "no_release", "error", "review", "sync_fail", "grabbed", "downloading"].includes(m.status) &&
        B("Interactive…", () => onRelease(m))}
      {["pending", "no_release", "error"].includes(m.status) &&
        B("Auto-search", () => act(() => api.search(m.tmdb_id)))}
      {["no_release", "error", "sync_fail", "downloading", "merged"].includes(m.status) &&
        B("Search again", () => act(() => api.research(m.tmdb_id)))}
      {["grabbed", "downloading", "no_release", "error", "sync_fail"].includes(m.status) &&
        B("Pick another", () => act(() => api.another(m.tmdb_id)))}
      {m.status === "merged" && B("Re-sync", () => act(() => api.sync(m.tmdb_id, 0)))}
      {m.status === "merged" && B("Tune sync", () => onTune(m))}
      {m.status === "sync_fail" && B("Re-try sync", () => act(() => api.sync(m.tmdb_id, 0)))}
      {m.status === "ignored" && B("Unignore", () => act(() => api.unignore(m.tmdb_id)))}
      {m.status !== "ignored" && m.status !== "merged" && B("Ignore", () => act(() => api.ignore(m.tmdb_id)))}
    </RowMenu>
  );
}

// grid (card) presentation of a movie
function MovieCard({ m, dl, busy, act, onRelease, onTune }:
  { m: Movie; dl?: DL; busy: boolean; act: (fn: () => Promise<any>) => void;
    onRelease: (m: Movie) => void; onTune: (m: Movie) => void }) {
  return (
    <div className="card">
      <Poster src={m.poster} alt={m.title} />
      <div className="card-body">
        <div className="card-title">{m.title} <span className="muted">({m.year})</span></div>
        <div className="sub">→ {m.original_title} · {m.original_lang}{m.quality ? " · " + m.quality : ""}</div>
        <div className="card-row"><Pill s={m.status} />
          {m.status === "sync_fail" && m.sync_delta != null && <span className="sub">Δ {m.sync_delta.toFixed(1)}s</span>}
          <div className="spacer" />
          <MovieActions m={m} busy={busy} act={act} onRelease={onRelease} onTune={onTune} />
        </div>
        {m.status === "downloading" && <DownloadBar dl={dl} />}
        {m.status === "ready" && <QueuedLine />}
        {m.status === "merging" && m.progress && <div className="sub" style={{ color: "#5ee9a0" }}>{m.progress}</div>}
        <Tracks a={m.audio_langs} s={m.sub_langs} na={m.need_audio} ns={m.need_subs} />
        {m.candidate_title && <div className="sub" style={{ marginTop: 4 }} title={m.candidate_title}>🎯 {m.candidate_title}</div>}
        <DriftBadge d={m.sync_drift} />
        {m.error && <div className="sub bad">{m.error}</div>}
      </div>
    </div>
  );
}

// ---------------- Overview (landing dashboard) ----------------
const fmtAgo = (ts: number, now: number) => {
  const d = Math.max(0, now - ts);
  if (d < 90) return "just now";
  if (d < 5400) return Math.round(d / 60) + " min ago";
  if (d < 129600) return Math.round(d / 3600) + " h ago";
  return Math.round(d / 86400) + " d ago";
};
const fmtIn = (ts: number, now: number) => {
  const d = ts - now;
  if (d <= 45) return "now";
  if (d < 5400) return "in " + Math.round(d / 60) + " min";
  return "in " + Math.round(d / 3600) + " h";
};

function Tile({ label, value, sub, tone, onClick }:
  { label: string; value: ReactNode; sub?: ReactNode; tone?: "good" | "warn" | "bad"; onClick?: () => void }) {
  return (
    <div className={"tile" + (tone ? " " + tone : "") + (onClick ? " click" : "")} onClick={onClick}>
      <div className="t-label">{label}</div>
      <div className="t-num">{value}</div>
      {sub && <div className="t-sub">{sub}</div>}
    </div>
  );
}

// Is the on-call AI actually fixing anything? The dispatcher runs on the host, outside this app,
// so the only evidence is whether it calls back. `resolved`/`failed` are verdicts it produced;
// `needs_human` is mostly the no-callback flip. A wall of needs_human with no callback ever means
// the dispatcher isn't running — which, without saying so, looks exactly like "it looked at
// everything and gave up".
function AiHealthPanel({ ai, now }: { ai?: AiHealth; now: number }) {
  if (!ai || !ai.enabled) return null;
  const seen = ai.pending + ai.resolved + ai.failed + ai.needs_human;
  if (seen + ai.never_sent === 0) return null;
  const verdicts = ai.resolved + ai.failed;      // outcomes it actually reported
  const rate = verdicts > 0 ? Math.round((ai.resolved / verdicts) * 100) : null;
  const silent = verdicts === 0 && (ai.needs_human > 0 || ai.pending > 0);
  return (
    <div className={`panel ${silent ? "warnbar-soft" : ""}`}>
      <div className="row" style={{ marginBottom: 6 }}>
        <b>🤖 On-call AI</b>
        <span className="muted">{ai.last_callback
          ? `last verdict ${fmtAgo(ai.last_callback, now)}`
          : "no verdict ever received"}</span>
      </div>
      <div className="aistats">
        {([["resolved", ai.resolved, "it fixed these"],
           ["failed", ai.failed, "it examined these and could not fix them"],
           ["needs_human", ai.needs_human, `flagged for you (includes the ${ai.stale_min}m no-callback flip)`],
           ["pending", ai.pending, "sent, still waiting for a verdict"],
           ["never_sent", ai.never_sent, "failed before AI review was on, or never ticketed"],
          ] as [string, number, string][]).filter(([, n]) => n > 0).map(([k, n, tip]) => (
          <span className={`aistat ${k}`} key={k} title={tip}>
            {n.toLocaleString()} <i>{k.replace("_", " ")}</i></span>))}
      </div>
      {silent
        ? <div className="sub bad" style={{ marginTop: 6 }}>
            Never called back. The dispatcher (the host cron running the Claude Code CLI on
            /config/ai-tickets) isn't reporting — so "needs you" here means it went quiet, not
            that it examined these and gave up.</div>
        : rate != null && <div className="sub muted" style={{ marginTop: 6 }}>
            {rate}% of the {verdicts.toLocaleString()} it reported on were resolved.</div>}
    </div>
  );
}

function Overview({ goto }: { goto: (tab: string) => void }) {
  const [d, setD] = useState<Dash | null>(null);
  const [dls, setDls] = useState<Record<string, DL>>({});
  const [logLines, setLogLines] = useState<string[]>([]);
  const [err, setErr] = useState("");

  usePoll(() => api.dashboard().then(x => { setD(x); setErr(""); })
    .catch(e => setErr(e.message || "dashboard unavailable")), 6000);
  usePoll(() => api.downloads().then(x => setDls(x.items || {})).catch(() => {}), 4000);
  usePoll(() => api.logs().then(x => setLogLines(x.lines.slice(-14))).catch(() => {}), 10000);

  if (!d) return <div className="panel muted">{err || "loading overview…"}</div>;

  const n = (c: Record<string, number>, ...ss: string[]) => ss.reduce((a, s) => a + (c[s] || 0), 0);
  const both = (...ss: string[]) => n(d.movies, ...ss) + n(d.episodes, ...ss);
  const merged = both("merged");
  const attention = both("review", "sync_fail", "error");
  const backlog = both("pending", "searching", "no_release");
  const downloading = d.active.filter(a => a.status === "downloading");
  const merging = d.active.filter(a => a.status === "merging");
  const queued = d.active.filter(a => a.status === "ready");
  const totalSpeed = Object.values(dls).reduce((a, x) => a + (x.dlspeed || 0), 0);
  const diskPct = d.disk ? Math.round((1 - d.disk.free / d.disk.total) * 100) : null;
  const dlOf = (a: { dl_hash?: string | null }) => dls[(a.dl_hash || "").toLowerCase()];

  return (
    <>
      {!d.enabled &&
        <div className="panel warnbar">⏸ Pipeline is <b>disabled</b> — nothing will be searched, grabbed or merged.
          <button className="btn sec" style={{ marginLeft: 10 }} onClick={() => goto("settings")}>Settings</button></div>}

      <div className="tilegrid">
        {/* `merged` is the terminal state for three outcomes; the sub-line counts only the work
            vo-merge actually did, so a library re-read can't read as thousands of merges. */}
        <Tile label="Correct now" value={merged} tone="good"
          sub={<>{d.merged_24h} merged in 24 h · {d.merged_7d} in 7 d</>} />
        <Tile label="Downloading" value={<>{d.inflight ?? downloading.length}<span className="t-cap"> / {d.inflight_cap}</span></>}
          sub={totalSpeed > 0 ? "↓ " + fmtSpeed(totalSpeed) : d.inflight == null ? "qB unreachable" : "slots in use"}
          tone={d.inflight == null ? "warn" : undefined} />
        <Tile label="Merging" value={<>{merging.length}<span className="t-cap"> / {d.merge_cap}</span></>}
          sub={merging.length ? merging[0].title
            : queued.length ? `${queued.length} queued` : "idle"} />
        <Tile label="Attention" value={attention} tone={attention ? "bad" : undefined}
          onClick={() => goto("review")}
          sub={<>{both("review")} review · {both("sync_fail")} sync · {both("error")} error</>} />
        <Tile label="Backlog" value={backlog}
          sub={<>{both("pending")} pending · {both("no_release")} no release</>} />
        {d.disk &&
          <Tile label="Donor disk" value={fmtBytes(d.disk.free)} tone={diskPct! >= 90 ? "bad" : diskPct! >= 80 ? "warn" : undefined}
            sub={<>free · {diskPct}% used</>} />}
      </div>

      <CoveragePanel goto={goto} />

      <div className="dash-cols">
        <div className="panel">
          <div className="row" style={{ marginBottom: 8 }}><b>Active now</b>
            <span className="muted">{d.active.length} item{d.active.length === 1 ? "" : "s"}</span>
            <div className="spacer" /><LiveDot /></div>
          {d.active.length === 0 && <div className="muted">Nothing in flight.</div>}
          {d.active.map(a => (
            <div className="dashrow" key={a.key}>
              <Poster src={a.poster} alt={a.title} />
              <div className="dashrow-main">
                <div className="dashrow-title">{a.title}
                  {a.count > 1 && <span className="muted"> · {a.count} eps</span>}</div>
                {a.sub && <div className="sub" title={a.sub}>{a.sub}</div>}
                {a.status === "downloading" && <DownloadBar dl={dlOf(a)} />}
                {a.status === "ready" && <QueuedLine pos={a.queue_pos} />}
                {a.status === "merging" && a.progress &&
                  <div className="sub" style={{ color: "#5ee9a0" }}>{a.progress}</div>}
              </div>
              <Pill s={a.status} />
            </div>
          ))}
        </div>

        <div>
          <div className="panel">
            {/* Only what the on-call AI could not resolve, plus `review` (a human decision by
                definition). Everything else that failed is still with the AI and is counted,
                not listed — otherwise this is a list of things already being worked on. */}
            <div className="row" style={{ marginBottom: 8 }}><b>Needs attention</b>
              {(d.ai_working ?? 0) > 0 &&
                <span className="muted" title="failed records the AI is still working on — they appear here only if it can't fix them">
                  {d.ai_working} with the AI</span>}
              {attention > 0 && <button className="btn sec" style={{ marginLeft: "auto", padding: "3px 10px", fontSize: 12 }}
                onClick={() => goto("review")}>Open review →</button>}</div>
            {d.attention.length === 0 && <div className="muted">
              {(d.ai_working ?? 0) > 0
                ? `Nothing for you — ${d.ai_working} failure(s) are with the AI.`
                : attention > 0
                  ? `${attention} failure(s), none flagged for you yet.`
                  : "All clear 🎉"}</div>}
            {d.attention.map(a => (
              // Title on ONE truncated line with the pills pinned beside it, then the message
              // below at full width. The old shape put the pills in a right-hand COLUMN, which
              // stole ~110px from the text and stacked them vertically as the panel narrowed.
              <div className="attn" key={a.key}>
                <div className="attn-head">
                  <span className="attn-title" title={a.title}>{a.title}</span>
                  {(a.count ?? 1) > 1 && <span className="cnt">{a.count}</span>}
                  <Pill s={a.status} />
                  <AiPill s={a.ai_status} />
                </div>
                {a.error && <div className="sub bad clamp2" title={a.error}>{a.error}</div>}
                {a.sync_delta != null && !a.error && <div className="sub">Δ {a.sync_delta.toFixed(1)}s</div>}
                {a.ai_verdict && <div className="sub clamp2" title={a.ai_verdict}>🤖 {a.ai_verdict}</div>}
              </div>
            ))}
          </div>

          <AiHealthPanel ai={d.ai} now={d.now} />

          <div className="panel">
            <div className="row" style={{ marginBottom: 8 }}><b>Recently merged</b>
              {d.merged_kinds &&
                <span className="muted" title="'already correct' files were never touched by vo-merge — a scan found they met their target and closed the record out">
                  {d.merged_kinds.grafted.toLocaleString()} grafted
                  {d.merged_kinds.replaced > 0 && <> · {d.merged_kinds.replaced.toLocaleString()} replaced</>}
                  {d.merged_kinds.already > 0 && <> · {d.merged_kinds.already.toLocaleString()} already correct</>}
                </span>}</div>
            {d.recent.length === 0 && <div className="muted">No merges yet.</div>}
            {d.recent.map((r, i) => (
              <div className="dashrow" key={i}>
                <Poster src={r.poster} alt={r.title} />
                <div className="dashrow-main">
                  <div className="dashrow-title">{r.title}</div>
                  <div className="sub addrow">
                    {r.langs && <span className="lang-badge">+{r.langs} audio</span>}
                    {r.subs && <span className="lang-badge subs">+{r.subs} subs</span>}
                    {r.how === "replaced" && <span className="lang-badge repl">used the release</span>}
                    {!r.langs && !r.subs && r.how !== "replaced" && <span className="muted">merged</span>}
                  </div>
                </div>
                <span className="muted" style={{ fontSize: 12, whiteSpace: "nowrap" }}>{fmtAgo(r.ts, d.now)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="row" style={{ marginBottom: 8 }}><b>Activity</b>
          <button className="btn sec" style={{ marginLeft: "auto", padding: "3px 10px", fontSize: 12 }}
            onClick={() => goto("logs")}>Full log →</button></div>
        <pre className="logs mini">{logLines.join("")}</pre>
        <div className="chips" style={{ marginTop: 10 }}>
          <span className="chip">grab <b>{d.grab_mode}</b></span>
          {d.next_runs.search != null && <span className="chip">next search <b>{fmtIn(d.next_runs.search, d.now)}</b></span>}
          {d.next_runs.finish != null && <span className="chip">next merge check <b>{fmtIn(d.next_runs.finish, d.now)}</b></span>}
          {d.next_runs.stall != null && <span className="chip">next stall sweep <b>{fmtIn(d.next_runs.stall, d.now)}</b></span>}
        </div>
      </div>
    </>
  );
}

// ---------------- Films ----------------
function Films() {
  const [status, setStatus] = useState<Status | null>(null);
  const [movies, setMovies] = useState<Movie[]>([]);
  const [filter, setFilter] = useState("");
  const [q, setQ] = useState("");
  const [view, setView] = useState<"grid" | "list">(() => (localStorage.getItem("vo_view") as any) || "grid");
  const [dls, setDls] = useState<Record<string, DL>>({});
  const [busy, setBusy] = useState(false);
  const [tune, setTune] = useState<Movie | null>(null);
  const [release, setRelease] = useState<Movie | null>(null);
  const [searchMsg, setSearchMsg] = useState("");

  async function refresh() {
    setStatus(await api.status());
    setMovies(await api.movies(filter || undefined));
  }
  usePoll(refresh, 8000, [filter]);
  // poll live download progress more often than the full list
  usePoll(() => api.downloads().then(d => setDls(d.items || {})).catch(() => {}), 4000);
  const setViewP = (v: "grid" | "list") => { setView(v); localStorage.setItem("vo_view", v); };

  const shown = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return movies;
    return movies.filter(m =>
      (m.title || "").toLowerCase().includes(s) || (m.original_title || "").toLowerCase().includes(s));
  }, [movies, q]);
  const dlOf = (m: Movie) => dls[(m.dl_hash || "").toLowerCase()];

  async function act(fn: () => Promise<any>) { setBusy(true); try { await fn(); } finally { setBusy(false); refresh(); } }

  async function searchAll() {
    setBusy(true); setSearchMsg("starting…");
    try {
      const r = await api.searchAll();
      setSearchMsg(r.started
        ? `searching ${r.pending ?? 0} pending · ${r.slots ?? "?"} free slot(s)`
        : (r.note || "already running"));
    } catch (e: any) { setSearchMsg("failed: " + (e.message || "")); }
    finally { setBusy(false); refresh(); }
  }

  return (
    <>
      <div className="panel">
        <div className="row">
          <div><b>Pipeline</b> {status?.enabled
            ? <span className="ok">enabled</span> : <span className="muted">disabled</span>}
            {status && <span className="muted"> · grab: {status.grab_mode}</span>}</div>
          <div className="spacer" />
          {searchMsg && <span className="muted">{searchMsg}</span>}
          <button className="btn sec" disabled={busy} onClick={searchAll}
            title="Search every pending title now instead of waiting for the timer (grabs up to the free download slots)">
            🔍 Search pending</button>
          <button className="btn" disabled={busy} onClick={() => act(api.scan)}>Scan now</button>
          <RescanButton scope="films" label="films" />
        </div>
        <div className="chips" style={{ marginTop: 14 }}>
          {STATES.map(s => (
            <span className="chip" key={s}>{s.replace("_"," ")} <b>{status?.counts[s] ?? 0}</b></span>
          ))}
        </div>
      </div>

      <div className="panel">
        <div className="row toolbar" style={{ marginBottom: 12 }}>
          <select value={filter} onChange={e => setFilter(e.target.value)}>
            <option value="">all states</option>
            {STATES.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
          <input className="search" placeholder="Search title…" value={q} onChange={e => setQ(e.target.value)} />
          <span className="muted">{shown.length}{shown.length !== movies.length ? `/${movies.length}` : ""} movies</span>
          <div className="spacer" />
          <div className="viewtoggle">
            <button className={view === "grid" ? "active" : ""} onClick={() => setViewP("grid")} title="Grid view">▦ Grid</button>
            <button className={view === "list" ? "active" : ""} onClick={() => setViewP("list")} title="List view">☰ List</button>
          </div>
        </div>

        {view === "grid"
          ? <div className="cardgrid">
              {shown.map(m => <MovieCard key={m.tmdb_id} m={m} dl={dlOf(m)} busy={busy}
                act={act} onRelease={setRelease} onTune={setTune} />)}
            </div>
          : <table>
              <thead><tr>
                <th>Title</th><th>Status</th><th>Candidate</th><th>Quality</th><th>Actions</th>
              </tr></thead>
              <tbody>
                {shown.map(m => (
                  <tr key={m.tmdb_id}>
                    <td><div className="titlecell">
                      <Poster src={m.poster} alt={m.title} />
                      <div>{m.title}<div className="sub">→ {m.original_title} ({m.year}) · {m.original_lang}</div>
                        {m.error && <div className="sub bad">{m.error}</div>}</div>
                    </div></td>
                    <td style={{ minWidth: 150 }}><Pill s={m.status} />{m.sync_delta != null && m.status === "sync_fail" &&
                      <div className="sub">Δ {m.sync_delta.toFixed(1)}s</div>}
                      {m.status === "downloading" && <DownloadBar dl={dlOf(m)} />}
                      {m.status === "ready" && <QueuedLine />}
                      {m.status === "merging" && m.progress &&
                      <div className="sub" style={{ color: "#5ee9a0" }}>{m.progress}</div>}</td>
                    <td>{m.candidate_title
                      ? <>{m.candidate_title}<div className="sub">score {m.candidate_score} · {m.candidate_seeders}s</div></>
                      : <span className="muted">—</span>}</td>
                    <td className="muted">{m.quality || "—"}
                      <Tracks a={m.audio_langs} s={m.sub_langs} na={m.need_audio} ns={m.need_subs} /></td>
                    <td><MovieActions m={m} busy={busy} act={act} onRelease={setRelease} onTune={setTune} /></td>
                  </tr>
                ))}
              </tbody>
            </table>}
        {shown.length === 0 && <div className="muted" style={{ padding: 8 }}>No movies match.</div>}
      </div>
      {tune && <SyncEditor movie={tune} onClose={() => { setTune(null); refresh(); }} />}
      {release && <ReleaseModal title={release.title}
        load={() => api.candidates(release.tmdb_id)}
        onGrab={c => api.grab(release.tmdb_id, c.link, c.rid, c.title)}
        onClose={() => setRelease(null)} onGrabbed={refresh} />}
    </>
  );
}

// ---------------- Series (Sonarr / TV) ----------------
const TV_STATES = ["pending","searching","no_release","grabbed","downloading",
  "ready","merging","merged","sync_fail","error","ignored"];

function Series({ anime }: { anime: boolean }) {
  const [eps, setEps] = useState<Episode[]>([]);
  const [filter, setFilter] = useState("");
  const [q, setQ] = useState("");
  const [view, setView] = useState<"grid" | "list">(() => (localStorage.getItem("vo_tv_view") as any) || "list");
  const setViewP = (v: "grid" | "list") => { setView(v); localStorage.setItem("vo_tv_view", v); };
  const [dls, setDls] = useState<Record<string, DL>>({});
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [relSeason, setRelSeason] = useState<{ seriesId: number; season: number; title: string } | null>(null);
  const [relEp, setRelEp] = useState<Episode | null>(null);
  const [searchMsg, setSearchMsg] = useState("");
  const toggle = (t: string) => setOpen(o => { const n = new Set(o); n.has(t) ? n.delete(t) : n.add(t); return n; });
  const dlOf = (e: Episode) => dls[(e.dl_hash || "").toLowerCase()];

  // only this tab's kind (anime vs standard TV)
  const kindEps = useMemo(() => eps.filter(e => ((e.series_type || "") === "anime") === anime), [eps, anime]);
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const e of kindEps) c[e.status] = (c[e.status] || 0) + 1;
    return c;
  }, [kindEps]);
  // group episodes: show -> season -> episodes (+ per-show status tallies)
  const shows = useMemo(() => {
    const m = new Map<string, Episode[]>();
    for (const e of kindEps) { let a = m.get(e.series_title); if (!a) { a = []; m.set(e.series_title, a); } a.push(e); }
    return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([title, list]) => {
      const byStatus: Record<string, number> = {};
      for (const e of list) byStatus[e.status] = (byStatus[e.status] || 0) + 1;
      const sm = new Map<number, Episode[]>();
      for (const e of list) { let a = sm.get(e.season); if (!a) { a = []; sm.set(e.season, a); } a.push(e); }
      const seasons = [...sm.entries()].sort((a, b) => a[0] - b[0]);
      for (const [, seps] of seasons) seps.sort((a, b) => a.episode - b.episode);
      return { title, poster: list[0]?.poster, eps: list, byStatus, seasons };
    });
  }, [kindEps]);
  const shownShows = useMemo(() => {
    const s = q.trim().toLowerCase();
    return s ? shows.filter(sh => sh.title.toLowerCase().includes(s)) : shows;
  }, [shows, q]);
  const allOpen = shownShows.length > 0 && open.size >= shownShows.length;

  async function refresh() {
    setEps(await api.tvEpisodes(filter || undefined));
  }
  usePoll(refresh, 8000, [filter]);
  usePoll(() => api.downloads().then(d => setDls(d.items || {})).catch(() => {}), 4000);

  async function act(fn: () => Promise<any>) { setBusy(true); try { await fn(); } finally { setBusy(false); refresh(); } }

  async function searchAll() {
    setBusy(true); setSearchMsg("starting…");
    try {
      const r = await api.searchAll();
      setSearchMsg(r.started
        ? `searching ${r.pending ?? 0} pending · ${r.slots ?? "?"} free slot(s)`
        : (r.note || "already running"));
    } catch (e: any) { setSearchMsg("failed: " + (e.message || "")); }
    finally { setBusy(false); refresh(); }
  }

  // seasons + episodes detail, shared by the list rows and the grid cards
  const renderSeasons = (sh: typeof shows[number]) => sh.seasons.map(([season, seps]) => (
    <div className="seasonblock" key={season}>
      <div className="seasonhead">Season {season} <span className="muted">· {seps.length}</span>
        <button className="btn sec" style={{ marginLeft: 8, padding: "2px 8px", fontSize: 11 }}
          onClick={() => setRelSeason({ seriesId: seps[0].series_id, season, title: sh.title })}>
          Search pack…</button></div>
      <table><tbody>
        {seps.map(e => (
          <tr key={e.id}>
            <td style={{ width: 70 }}>S{pad2(e.season)}E{pad2(e.episode)}
              {e.aired && <div className="sub" title="the numbering releases use for this episode">
                aired {e.aired}</div>}</td>
            <td style={{ minWidth: 140 }}><Pill s={e.status} />
              {e.ai_status && <AiPill s={e.ai_status} />}
              {e.status === "downloading" && <DownloadBar dl={dlOf(e)} />}
              {e.status === "ready" && <QueuedLine />}
              {e.status === "merging" && e.progress &&
              <div className="sub" style={{ color: "#5ee9a0" }}>{e.progress}</div>}
              <Tracks a={e.audio_langs} s={e.sub_langs} na={e.need_audio} ns={e.need_subs} />
              {e.error && <div className="sub bad">{e.error}</div>}
              {e.ai_verdict && <div className="sub" title={e.ai_verdict}>🤖 {e.ai_verdict}</div>}</td>
            <td>{e.candidate_title
              ? <>{e.candidate_title}<div className="sub">score {e.candidate_score} · {e.candidate_seeders}s</div></>
              : <span className="muted">—</span>}</td>
            <td className="muted" style={{ width: 90 }}>{e.quality || "—"}</td>
            <td><div className="row">
              {!["ignored", "merged"].includes(e.status) &&
                <button className="btn sec" disabled={busy} onClick={() => setRelEp(e)}>Interactive…</button>}
              {["pending", "no_release", "error", "sync_fail"].includes(e.status) &&
                <button className="btn sec" disabled={busy} onClick={() => act(() => api.epRetry(e.id))}>Retry</button>}
              {!["ignored", "merged"].includes(e.status) &&
                <button className="btn sec" disabled={busy} onClick={() => act(() => api.epIgnore(e.id))}>Ignore</button>}
            </div></td>
          </tr>
        ))}
      </tbody></table>
    </div>
  ));

  const statusPills = (sh: typeof shows[number]) => Object.entries(sh.byStatus).map(([s, n]) =>
    <span className={`pill ${s}`} key={s}>{n} {s.replace("_", " ")}</span>);

  // list row (accordion)
  const renderShow = (sh: typeof shows[number]) => {
    const isOpen = open.has(sh.title);
    return (
      <div className="showgroup" key={sh.title}>
        <div className="showhead" onClick={() => toggle(sh.title)}>
          <span className="caret">{isOpen ? "▾" : "▸"}</span>
          <Poster src={sh.poster} alt={sh.title} />
          <b>{sh.title}</b><span className="muted">{sh.eps.length} ep</span>
          <div className="spacer" />
          <span className="chips">{statusPills(sh)}</span>
        </div>
        {isOpen && renderSeasons(sh)}
      </div>
    );
  };

  // grid poster card; expands full-width when opened
  const renderShowCard = (sh: typeof shows[number]) => {
    const isOpen = open.has(sh.title);
    return (
      <div className={"showcard" + (isOpen ? " open" : "")} key={sh.title}>
        <div className="showcard-head" onClick={() => toggle(sh.title)}>
          <Poster src={sh.poster} alt={sh.title} />
          <div className="showcard-meta">
            <div className="showcard-title">{sh.title}</div>
            <div className="sub">{sh.eps.length} ep</div>
            <div className="chips">{statusPills(sh)}</div>
          </div>
        </div>
        {isOpen && <div className="showcard-detail">{renderSeasons(sh)}</div>}
      </div>
    );
  };

  return (
    <>
      <div className="panel">
        <div className="row">
          <div><b>{anime ? "🎌 Anime pipeline" : "📺 TV Shows pipeline"}</b> <span className="muted">episode VO merges</span></div>
          <div className="spacer" />
          {searchMsg && <span className="muted">{searchMsg}</span>}
          <button className="btn sec" disabled={busy} onClick={searchAll}
            title="Search every pending episode now instead of waiting for the timer">
            🔍 Search pending</button>
          <button className="btn" disabled={busy} onClick={() => act(api.tvScan)}>
            Scan {anime ? "anime" : "series"}</button>
          <RescanButton scope={anime ? "anime" : "series"} label={anime ? "anime" : "TV shows"} />
        </div>
        <div className="chips" style={{ marginTop: 14 }}>
          {TV_STATES.map(s => (
            <span className="chip" key={s}>{s.replace("_"," ")} <b>{counts[s] ?? 0}</b></span>
          ))}
        </div>
      </div>

      <div className="panel">
        <div className="row toolbar" style={{ marginBottom: 10 }}>
          <select value={filter} onChange={e => setFilter(e.target.value)}>
            <option value="">all states</option>
            {TV_STATES.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
          <input className="search" placeholder={anime ? "Search anime…" : "Search show…"} value={q} onChange={e => setQ(e.target.value)} />
          <span className="muted">{shownShows.length} shows · {kindEps.length} episodes</span>
          <div className="spacer" />
          {(counts.error ?? 0) > 0 &&
            <button className="btn sec" disabled={busy} onClick={() => act(api.tvRetryErrors)}
              title="Blocklist the failed release, drop its donor, and re-search">
              ↻ Retry {counts.error} errors</button>}
          <button className="btn sec" onClick={() => setOpen(allOpen ? new Set() : new Set(shownShows.map(s => s.title)))}>
            {allOpen ? "Collapse all" : "Expand all"}</button>
          <div className="viewtoggle">
            <button className={view === "grid" ? "active" : ""} onClick={() => setViewP("grid")} title="Grid view">▦ Grid</button>
            <button className={view === "list" ? "active" : ""} onClick={() => setViewP("list")} title="List view">☰ List</button>
          </div>
        </div>
        {view === "grid"
          ? <div className="showcardgrid">{shownShows.map(renderShowCard)}</div>
          : shownShows.map(renderShow)}
        {shownShows.length === 0 && <div className="muted">{anime ? "No anime match." : "No shows match."}</div>}
      </div>
      {relSeason && <ReleaseModal title={`${relSeason.title} S${pad2(relSeason.season)}`}
        load={() => api.seasonCandidates(relSeason.seriesId, relSeason.season)}
        onGrab={c => api.seasonGrab(relSeason.seriesId, relSeason.season, c.link, c.rid, c.title)}
        onClose={() => setRelSeason(null)} onGrabbed={refresh} />}
      {relEp && <ReleaseModal title={`${relEp.series_title} S${pad2(relEp.season)}E${pad2(relEp.episode)}`}
        load={() => api.episodeCandidates(relEp.id)}
        onGrab={c => api.episodeGrab(relEp.id, c.link, c.rid, c.title)}
        onClose={() => setRelEp(null)} onGrabbed={refresh} />}
    </>
  );
}

// ---------------- Review (needs attention) ----------------
// The single place a human resolves problems: movies in review/sync_fail/error AND TV episodes
// in sync_fail/error, each showing what the on-call AI made of it. Items the AI couldn't fix
// (failed / needs_human) float to the top — that highlighted band is the manual-review queue.
const aiUnfixed = (s?: string | null) => s === "failed" || s === "needs_human";

function Review() {
  const [movies, setMovies] = useState<Movie[]>([]);
  const [eps, setEps] = useState<Episode[]>([]);
  const [busy, setBusy] = useState(false);
  const [tune, setTune] = useState<Movie | null>(null);
  const [release, setRelease] = useState<Movie | null>(null);
  const [relEp, setRelEp] = useState<Episode | null>(null);
  const [ai, setAi] = useState<Record<string, string>>({});

  async function refresh() {
    const [rv, sf, er, allEps] = await Promise.all([
      api.movies("review"), api.movies("sync_fail"), api.movies("error"), api.tvEpisodes(),
    ]);
    setMovies([...rv, ...sf, ...er]);
    setEps(allEps.filter(e =>
      ["error", "sync_fail"].includes(e.status) || aiUnfixed(e.ai_status)));
  }
  usePoll(refresh, 8000);

  async function act(fn: () => Promise<any>) { setBusy(true); try { await fn(); } finally { setBusy(false); refresh(); } }
  async function sendAI(k: string, call: () => Promise<{ queued: boolean }>) {
    setAi(s => ({ ...s, [k]: "…" }));
    try { const r = await call(); setAi(s => ({ ...s, [k]: r.queued ? "queued ✓" : "failed" })); }
    catch { setAi(s => ({ ...s, [k]: "failed" })); }
    refresh();
  }

  type Row = { kind: "movie"; m: Movie } | { kind: "episode"; e: Episode };
  const rows: Row[] = [
    ...movies.map(m => ({ kind: "movie" as const, m })),
    ...eps.map(e => ({ kind: "episode" as const, e })),
  ];
  const aiOf = (r: Row) => r.kind === "movie" ? r.m.ai_status : r.e.ai_status;
  rows.sort((a, b) => (aiUnfixed(aiOf(b)) ? 1 : 0) - (aiUnfixed(aiOf(a)) ? 1 : 0));
  const needHuman = rows.filter(r => aiUnfixed(aiOf(r))).length;
  // 'review' means a human must decide — bulk retry only covers the two failure states
  const retryable = rows.filter(r =>
    ["error", "sync_fail"].includes(r.kind === "movie" ? r.m.status : r.e.status)).length;

  const aiCell = (status?: string | null, verdict?: string | null, k?: string) =>
    status ? <><AiPill s={status} />{verdict && <div className="sub" title={verdict}>{verdict}</div>}</>
           : <span className="muted">not sent</span>;

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 10 }}>
        <b>Needs review</b>
        <span className="muted">{rows.length} item{rows.length === 1 ? "" : "s"}
          {needHuman > 0 && <> · <span className="bad">{needHuman} the AI couldn’t fix</span></>}</span>
        <div className="spacer" />
        {retryable > 0 &&
          <button className="btn sec" disabled={busy}
            title="Blocklist each failed release, drop its donor, and re-search — films and episodes"
            onClick={() => act(api.retryAllErrors)}>↻ Retry {retryable} failed</button>}
        <LiveDot />
      </div>
      {rows.length === 0
        ? <div className="muted">Nothing needs review 🎉</div>
        : <table>
            <thead><tr><th>Title</th><th>Reason</th><th>AI review</th><th>Actions</th></tr></thead>
            <tbody>
              {rows.map(r => {
                if (r.kind === "movie") {
                  const m = r.m, k = `m${m.tmdb_id}`;
                  return (
                    <tr key={k} className={aiUnfixed(m.ai_status) ? "needshuman" : ""}>
                      <td><div className="titlecell">
                        <Poster src={m.poster} alt={m.title} />
                        <div>{m.title}<div className="sub">{m.original_title} ({m.year})</div>
                          <Pill s={m.status} /></div>
                      </div></td>
                      <td>{m.error ? <span className="bad">{m.error}</span> : <span className="muted">—</span>}
                        {m.sync_delta != null && <div className="sub">Δ {m.sync_delta.toFixed(2)}s</div>}
                        <DriftBadge d={m.sync_drift} /></td>
                      <td>{aiCell(m.ai_status, m.ai_verdict)}</td>
                      <td><div className="row">
                        <button className="btn sec" disabled={busy} onClick={() => setTune(m)}>Tune sync</button>
                        <button className="btn sec" disabled={busy} onClick={() => setRelease(m)}>Search…</button>
                        <button className="btn sec" disabled={busy} onClick={() => act(() => api.another(m.tmdb_id))}>Pick another</button>
                        <button className="btn sec" disabled={busy} onClick={() => act(() => api.ignore(m.tmdb_id))}>Ignore</button>
                        <button className="btn sec" disabled={busy || ai[k] === "…" || ai[k] === "queued ✓"}
                          title="File a ticket for the on-call AI agent — it inspects this item and resyncs, picks another release, or reports back"
                          onClick={() => sendAI(k, () => api.aiSend(m.tmdb_id))}>🤖 {ai[k] ?? "Send to AI"}</button>
                      </div></td>
                    </tr>
                  );
                }
                const e = r.e, k = `e${e.id}`;
                return (
                  <tr key={k} className={aiUnfixed(e.ai_status) ? "needshuman" : ""}>
                    <td><div className="titlecell">
                      <Poster src={e.poster} alt={e.series_title} />
                      <div>{e.series_title} <span className="muted">S{pad2(e.season)}E{pad2(e.episode)}</span>
                        <div className="sub">{e.aired ? `episode · released as ${e.aired}` : "episode"}</div>
                        <Pill s={e.status} /></div>
                    </div></td>
                    <td>{e.error ? <span className="bad">{e.error}</span> : <span className="muted">—</span>}
                      {e.sync_delta != null && <div className="sub">Δ {e.sync_delta.toFixed(2)}s</div>}
                      <DriftBadge d={e.sync_drift} /></td>
                    <td>{aiCell(e.ai_status, e.ai_verdict)}</td>
                    <td><div className="row">
                      <button className="btn sec" disabled={busy} onClick={() => setRelEp(e)}>Search…</button>
                      <button className="btn sec" disabled={busy} onClick={() => act(() => api.epRetry(e.id))}>Retry</button>
                      <button className="btn sec" disabled={busy} onClick={() => act(() => api.epIgnore(e.id))}>Ignore</button>
                      <button className="btn sec" disabled={busy || ai[k] === "…" || ai[k] === "queued ✓"}
                        title="File a ticket for the on-call AI agent"
                        onClick={() => sendAI(k, () => api.epAiSend(e.id))}>🤖 {ai[k] ?? "Send to AI"}</button>
                    </div></td>
                  </tr>
                );
              })}
            </tbody>
          </table>}
      {tune && <SyncEditor movie={tune} onClose={() => { setTune(null); refresh(); }} />}
      {release && <ReleaseModal title={release.title}
        load={() => api.candidates(release.tmdb_id)}
        onGrab={c => api.grab(release.tmdb_id, c.link, c.rid, c.title)}
        onClose={() => setRelease(null)} onGrabbed={refresh} />}
      {relEp && <ReleaseModal title={`${relEp.series_title} S${pad2(relEp.season)}E${pad2(relEp.episode)}`}
        load={() => api.episodeCandidates(relEp.id)}
        onGrab={c => api.episodeGrab(relEp.id, c.link, c.rid, c.title)}
        onClose={() => setRelEp(null)} onGrabbed={refresh} />}
    </div>
  );
}

// ---------------- Settings ----------------
const SECRET_BOOLS = ["prowlarr_key", "radarr_key", "sonarr_key", "plex_token"];
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
    for (const key of ["en_indexer_ids", "multi_indexer_ids"])
      if (key in data && typeof data[key] === "string")
        data[key] = data[key].split(",").map((x: string) => parseInt(x.trim(), 10)).filter((n: number) => !isNaN(n));
    for (const key of ["series_pilot", "anime_dirs"])
      if (key in data && typeof data[key] === "string")
        data[key] = data[key].split(",").map((x: string) => x.trim()).filter(Boolean);
    await api.saveSettings(data); setSaved(true); setChanged({});
    api.settings().then(setCfg);
  }
  async function test(which: string) {
    setTests({ ...tests, [which]: "…" });
    const r = await api.test(which);
    setTests({ ...tests, [which]: r.ok ? "ok" : (r.error || "failed") });
  }

  // lang_profiles is nested ({movie:{audio:[],subs:[]}, …}); edit it as comma-separated text
  // and keep the whole object in `changed` so save() sends it in one piece.
  const prof = (kind: string, which: "audio" | "subs") => {
    const v = (val("lang_profiles") || {})[kind]?.[which];
    return Array.isArray(v) ? v.join(", ") : (v ?? "");
  };
  const setProf = (kind: string, which: "audio" | "subs", text: string) => {
    const all = JSON.parse(JSON.stringify(val("lang_profiles") || {}));
    all[kind] = { ...(all[kind] || {}), [which]: text.split(",").map(x => x.trim()).filter(Boolean) };
    set("lang_profiles", all);
  };

  // the *arrs must reach this app by IP, so show the URL the browser is already using
  const origin = typeof window !== "undefined" ? window.location.origin : "http://<vo-merge>";
  const tok = val("webhook_token") ? `?token=${encodeURIComponent(val("webhook_token"))}` : "";

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
        <label>Sonarr URL</label>{Text("sonarr_url")}{TestBtn("sonarr")}
        <label>Sonarr API key</label>{Secret("sonarr_key")}<span />
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
        <label>MULTI indexer IDs</label>
        <input type="text" value={Array.isArray(val("multi_indexer_ids")) ? val("multi_indexer_ids").join(", ") : (val("multi_indexer_ids") ?? "")}
          onChange={e => set("multi_indexer_ids", e.target.value)} /><span className="muted">extra (e.g. FR trackers) for MULTI</span>
        <label>Films</label>{Check("scope_films")}<span />
        <label>Exclude French-origin</label>{Check("exclude_french_origin")}<span />
      </div>

      <div className="section-title">Series (Sonarr)</div>
      <div className="form-grid">
        <label>Enable Series/Anime</label>{Check("scope_series")}<span className="muted">runs the episode pipeline</span>
        <label>Sonarr vo-gap tag</label>{Text("sonarr_vo_gap_tag")}<span />
        <label>Pilot series</label>
        <input type="text" value={Array.isArray(val("series_pilot")) ? val("series_pilot").join(", ") : (val("series_pilot") ?? "")}
          onChange={e => set("series_pilot", e.target.value)} /><span className="muted">comma-sep; empty = all tagged</span>
        <label>qB TV category</label>{Text("qb_tv_category")}<span />
        <label>Season-pack threshold</label>{Text("tv_pack_threshold", "number")}<span className="muted">≥ N gap eps → grab a pack</span>
      </div>

      <div className="section-title">Gap detection &amp; subtitles</div>
      <div className="form-grid">
        <label>How gaps are found</label>
        <select value={val("scan_mode") ?? "files"} onChange={e => set("scan_mode", e.target.value)}>
          <option value="files">Read the files (accurate)</option>
          <option value="tag">Trust Radarr/Sonarr metadata (legacy)</option>
        </select>
        <span className="muted">"Read the files" probes every library file with mkvmerge, so a
          stale tag or an un-analysed file can't hide a gap</span>
        <label>Scan all films</label>{Check("scan_all_movies")}
        <span className="muted">files mode: consider every Radarr film, not just vo-gap tagged ones</span>
        <label>Add subtitles</label>{Check("want_subs")}
        <span className="muted">take the donor's subtitles while grafting its audio</span>
        <label>Max sub tracks</label>{Text("max_sub_tracks", "number")}
        <span className="muted">per language (packs ship 6+: full, forced, SDH, signs…)</span>
        <label>Chase subs alone</label>{Check("subs_only_gap")}
        <span className="muted">download a release for a file that already has every target
          audio language but is missing a target subtitle — off by default, this adds a lot of
          downloads</span>
      </div>

      <div className="section-title">Instant pickup (webhooks)</div>
      <div className="muted" style={{ margin: "-4px 0 10px" }}>
        Without these, a newly imported file waits up to one search interval
        ({val("search_interval_min") ?? 60} min) for the sweep. Add a <b>Connect → Webhook</b> in
        Radarr and Sonarr, method POST, triggered <b>On Import</b> and <b>On Upgrade</b>, pointing at:
        <div className="hookurl">{origin}/api/hook/radarr{tok}</div>
        <div className="hookurl">{origin}/api/hook/sonarr{tok}</div>
        Their <b>Test</b> button works. Only the changed file is probed, so a hook costs one
        mkvmerge call rather than a library sweep.
      </div>
      <div className="form-grid">
        <label>Webhook token</label>{Text("webhook_token")}
        <span className="muted">optional; when set, the URLs above must carry
          <code>?token=…</code>. Leave empty for no check (LAN-only, like the rest of the API)</span>
      </div>

      <div className="section-title">Language targets</div>
      <div className="muted" style={{ margin: "-4px 0 10px" }}>
        The end state each kind of title should reach. A file missing any of these is a gap, and
        the missing languages are what gets grafted off the donor. Comma-separated 3-letter codes.
      </div>
      <div className="form-grid">
        {(["movie", "series", "anime"] as const).flatMap(kind => [
          <label key={`${kind}-a`}>{kind === "movie" ? "Films" : kind === "series" ? "TV shows" : "Anime"} — audio</label>,
          <input key={`${kind}-ai`} type="text" value={prof(kind, "audio")}
            onChange={e => setProf(kind, "audio", e.target.value)} />,
          <span key={`${kind}-as`} className="muted">
            {kind === "anime" ? "keeps the Japanese VO alongside FR+EN" : "e.g. fre, eng"}</span>,
          <label key={`${kind}-s`}>&nbsp;&nbsp;&nbsp;&nbsp;— subtitles</label>,
          <input key={`${kind}-si`} type="text" value={prof(kind, "subs")}
            onChange={e => setProf(kind, "subs", e.target.value)} />,
          <span key={`${kind}-ss`} className="muted" />,
        ])}
        <label>Anime folders</label>
        <input type="text" value={Array.isArray(val("anime_dirs")) ? val("anime_dirs").join(", ") : (val("anime_dirs") ?? "")}
          onChange={e => set("anime_dirs", e.target.value)} />
        <span className="muted">top-level library folders that mean anime; Sonarr's own anime
          flag and a Japanese original language also select that profile</span>
      </div>

      <div className="section-title">Queues &amp; limits</div>
      <div className="form-grid">
        <label>Download slots</label>{Text("max_inflight_downloads", "number")}
        <span className="muted">how many downloads run at once (a season pack counts as one)</span>
        <label>Simultaneous merges</label>{Text("max_parallel_merges", "number")}
        <span className="muted">merges share the CPU/iGPU — 1 is safest, raise only with headroom</span>
        <label>Searches per run</label>{Text("max_search_per_run", "number")}
        <span className="muted">cap on new searches/grabs per cycle</span>
        <label>Queue check (min)</label>{Text("promote_interval_min", "number")}
        <span className="muted">how often finished downloads join the merge queue</span>
        <label>Stall timeout (min)</label>{Text("stall_timeout_min", "number")}
        <span className="muted">idle+seedless this long → drop &amp; try another release</span>
        <label>Metadata timeout (min)</label>{Text("meta_timeout_min", "number")}
        <span className="muted">dead magnet ("fetching metadata", no seeds) → dropped this fast</span>
        <label>Max download age (min)</label>{Text("dl_max_age_min", "number")}
        <span className="muted">absolute cap; even a slow trickle is dropped past this</span>
        <label>Stall check (min)</label>{Text("stall_check_interval_min", "number")}<span />
        <label>Max release retries</label>{Text("max_sync_retries", "number")}
        <span className="muted">different releases tried before giving up</span>
        <label>AI reply timeout (min)</label>{Text("ai_stale_min", "number")}
        <span className="muted">no AI verdict in this long → flag for manual review</span>
        <label>AI escalation</label>{Check("ai_tickets")}
        <span className="muted">auto-send failures to the on-call AI</span>
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
  useEffect(() => { refresh(); /* always load once on mount */ /* eslint-disable-next-line */ }, []);
  usePoll(() => { if (auto) refresh().catch(() => {}); }, 5000, [auto]);
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
// Global brake, in the header so it's reachable from every tab. Pausing stops NEW searches,
// grabs and merges; anything already merging finishes (killing mkvmerge mid-write would leave a
// corrupt library file), so the header says how many are still in flight.
function PauseControl() {
  const [st, setSt] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);
  usePoll(() => api.status().then(setSt).catch(() => {}), 5000);
  if (!st) return null;
  const toggle = async () => {
    setBusy(true);
    try { await api.pause(!st.paused); await api.status().then(setSt); }
    finally { setBusy(false); }
  };
  return (
    <>
      {st.paused
        ? <span className="badge paused">⏸ paused
            {!!st.merging_now && <> · {st.merging_now} merge(s) finishing</>}</span>
        : st.hold === "scanning" &&
            <span className="badge">⏳ scan running — grabs held</span>}
      <button className="btn sec" disabled={busy} onClick={toggle}
        title={st.paused
          ? "Resume searches, grabs and merges"
          : "Stop starting new searches, grabs and merges. Work already running finishes; scans keep going."}>
        {st.paused ? "▶ Resume" : "⏸ Pause"}
      </button>
    </>
  );
}

export default function App() {
  const [tab, setTab] = useState("overview");
  const tabs: [string, string][] = [
    ["overview", "Overview"],
    ["films", "Films"], ["anime", "🎌 Anime"], ["series", "📺 TV Shows"],
    ["library", "Library"], ["review", "Review"],
    ["settings", "Settings"], ["logs", "Logs"]];
  return (
    <div className="app">
      <header className="top">
        <h1>🎬 VO Merger</h1>
        <span className="badge">multi-language library builder</span>
        <div className="spacer" />
        <PauseControl />
      </header>
      <nav>
        {tabs.map(([k, label]) =>
          <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>{label}</button>)}
      </nav>
      {tab === "overview" && <Overview goto={setTab} />}
      {tab === "films" && <Films />}
      {tab === "anime" && <Series anime key="anime" />}
      {tab === "series" && <Series anime={false} key="series" />}
      {tab === "library" && <Library />}
      {tab === "review" && <Review />}
      {tab === "settings" && <Settings />}
      {tab === "logs" && <Logs />}
    </div>
  );
}
