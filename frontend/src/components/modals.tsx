import { useEffect, useRef, useState } from "react";
import { api, withKey, type Candidate, type Movie } from "../api";
import { fmtBytes, fmtTime } from "../lib/format";
import { Modal } from "./ui";

/* Two modals lifted out of the old single-file App unchanged in behaviour: the interactive
   release list, and the sync tuner (a stable video element with Web Audio playing the
   candidate offset live, so you hear the change without a reload). Both are intricate and
   both work; the revamp moves them, it does not rewrite them. */

export function ReleaseModal({ title, load, onGrab, onClose, onGrabbed }:
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
          <div className="rel-list scroll-y tall">
            {list.map(c => (
              <div className={"rel-row" + (c.tried ? " tried" : "")} key={c.rid}>
                <div className="rel-main">
                  <div className="rel-title">
                    {c.info_url
                      ? <a href={c.info_url} target="_blank" rel="noreferrer" title="Open tracker page"
                           style={{ color: "#9ecbff", textDecoration: "none" }}>{c.title} ↗</a>
                      : c.title}
                    {/* COMPLETE outranks PACK: it claims every episode of the show in one grab,
                        which is a different promise from "one season". */}
                    {c.complete
                      ? <span className="multi-badge" style={{ background: "#2a2440", color: "#c9a6ff", borderColor: "#4a3d70" }}>COMPLETE</span>
                      : c.pack && <span className="multi-badge" style={{ background: "#14432a", color: "#5ee9a0" }}>PACK</span>}
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

// ---------------- Sync Editor (stable video + Web Audio live offset + waveform) ----------------
export function SyncEditor({ movie, onClose }: { movie: Movie; onClose: () => void }) {
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
      const buf = await (await fetch(withKey(dd.audio))).arrayBuffer();
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
          <video ref={v} src={withKey(d.video)} muted loop playsInline controls
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

/* ---------------------------------------------------------------- lip-sync reader
   The one measurement in this app that does not compare two files to each other. It correlates
   mouth movement in the picture against the speech envelope of an audio track, so it can answer
   when the pair has already been declared a different cut — and it works on a library file with
   no donor at all, which nothing else here can do.

   The panel deliberately shows every WINDOW, not just the verdict. "3 of 6 windows agreed" and
   "6 of 6 agreed" are both a number; only the first tells you to treat it with care. */
export function LipSyncModal({ kind, id, title, onClose, onApplied }:
  { kind: string; id: string; title: string; onClose: () => void; onApplied?: () => void }) {
  const [r, setR] = useState<any>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(true);
  const [applying, setApplying] = useState(false);

  async function run(apply = false) {
    apply ? setApplying(true) : setBusy(true);
    setErr("");
    try {
      const out = await api.lipsync(kind, id, { apply });
      setR(out);
      if (apply) onApplied?.();
    } catch (e: any) { setErr(e.message || "lip-sync failed"); }
    finally { setBusy(false); setApplying(false); }
  }
  useEffect(() => { run(false); /* eslint-disable-next-line */ }, []);

  const tracks: any[] = r?.tracks ?? [];
  const pairwise = r && r.compared && r.compared !== "library file against its own picture";

  const windows = (w: any[], label: string) => (
    <div className="lipwins">
      <div className="sub" style={{ marginBottom: 2 }}>{label}</div>
      {w.map((x, i) => (
        <div className={"lipwin" + (x.used ? "" : " skip")} key={i}
          title={x.why || (x.used ? "used in the consensus" : "")}>
          <span style={{ width: 58 }}>{fmtTime(x.start)}</span>
          <span className="bar"><span style={{ width: Math.min(100, x.conf * 100) + "%" }} /></span>
          <span style={{ width: 74, textAlign: "right" }}>
            {x.offset_ms == null ? "—" : `${x.offset_ms > 0 ? "+" : ""}${x.offset_ms} ms`}</span>
          <span style={{ width: 42, textAlign: "right" }}>{x.conf.toFixed(2)}</span>
        </div>
      ))}
    </div>
  );

  return (
    <Modal title={`Read the lips — ${title}`} onClose={onClose} wide>
      <p className="muted" style={{ marginTop: 0 }}>
        Correlates mouth movement in the picture against the speech in each audio track. This is
        the only <b>absolute</b> reading here — everything else compares two files, so both can be
        wrong together. On original-language audio it is good to about a frame; on a dub it is
        accurate to roughly ±150&nbsp;ms, because dubbing matches <i>when</i> people speak rather
        than how their lips move.
      </p>
      {busy && <div className="muted">reading… this decodes several short windows and takes a
        minute or two.</div>}
      {err && <div className="err">{err}</div>}

      {r && pairwise && (
        <div className="panel" style={{ background: "var(--panel2)" }}>
          <div className="row">
            <div>
              <div className="t-label">Library vs donor</div>
              <div className="t-num">
                {r.offset_ms == null ? "no reading"
                  : `${r.offset_ms > 0 ? "+" : ""}${r.offset_ms} ms`}</div>
              <div className="t-sub">
                {r.offset_ms == null
                  ? "one of the two files could not be read — too little on-screen dialogue, or a "
                    + "genuinely different edit"
                  : `confidence ${(r.confidence ?? 0).toFixed(2)}`}</div>
            </div>
            <div className="spacer" />
            {r.offset_ms != null && (
              <button className="btn" disabled={applying} onClick={() => run(true)}>
                {applying ? "applying…" : `Apply ${r.offset_ms > 0 ? "+" : ""}${r.offset_ms} ms and merge`}
              </button>
            )}
          </div>
          {r.note && <div className="sub" style={{ marginTop: 8 }}>{r.note}</div>}
        </div>
      )}

      {r && !pairwise && tracks.length > 0 && (
        <>
          <div className="sub" style={{ margin: "10px 0 6px" }}>
            No donor on disk, so this reads the library file's own tracks. A track that is out on
            its own is a bad graft; every track out by the same amount is a bad source file.
          </div>
          {tracks.map((t, i) => (
            <div className="panel" key={i} style={{ background: "var(--panel2)" }}>
              <div className="row">
                <b>{t.track?.lang || "und"}
                  {t.track?.name ? <span className="muted"> · {t.track.name}</span> : null}</b>
                <div className="spacer" />
                <b className={t.offset_ms == null ? "muted"
                  : Math.abs(t.offset_ms) > 200 ? "bad" : "ok"}>
                  {t.offset_ms == null ? "no reading"
                    : `${t.offset_ms > 0 ? "+" : ""}${t.offset_ms} ms`}</b>
                <span className="sub">{t.agreed}/{t.tested} windows · {t.method}</span>
              </div>
              {t.note && <div className="sub" style={{ marginTop: 4 }}>{t.note}</div>}
              {t.windows?.length > 0 && windows(t.windows, "per window (start · confidence · offset)")}
            </div>
          ))}
        </>
      )}

      {r && !busy && (
        <div className="row" style={{ marginTop: 10 }}>
          <button className="btn sec" onClick={() => run(false)}>Read again</button>
          <span className="sub">Each read decodes fresh windows, so a second run is an
            independent measurement rather than a cached one.</span>
        </div>
      )}
    </Modal>
  );
}
