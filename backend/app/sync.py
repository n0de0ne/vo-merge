"""Resilient A/V offset detection: multiple analysis windows, video scene-cut matching
primary, audio music/SFX cross-correlation as cross-check + fallback. Centralizes the
logic used by both the movie and episode merges."""
from . import core
from .offdet_video import detect_offset_video_ms
from .offdet import detect_offset_ms


def fps_close(a, b, tol=0.03):
    """True if framerates are close enough that a constant offset still works.
    23.976 vs 24.0 (0.1%) -> ok; 25 vs 23.976 (4%) -> not (PAL speedup, would drift)."""
    if not a or not b:
        return True
    return abs(a - b) / max(a, b) <= tol


def _windows(dur, n=3, length=480):
    """n analysis windows spread across the middle of the content, clamped to runtime."""
    dur = dur or 0
    if dur < 240:                       # very short -> one best-effort window
        return [(max(0, dur * 0.1), max(60, dur * 0.6))] if dur else [(120, length)]
    out, step = [], (dur * 0.8) / (n + 1)
    for i in range(1, n + 1):
        s = max(30, min(dur * 0.1 + step * i - length / 2, dur - length - 5))
        out.append((s, min(length, dur - s - 2)))
    # de-dupe near-identical windows
    uniq = []
    for w in out:
        if not uniq or abs(w[0] - uniq[-1][0]) > 30:
            uniq.append(w)
    return uniq


def audio_consensus(path, ref_ai, shift_ai, dur, cfg, tag=""):
    """Offset between two audio tracks IN THE SAME FILE (for re-syncing a finished merge,
    where there's only one video). Multi-window consensus. Returns (offset_ms|None, conf)."""
    import statistics
    wins = _windows(dur, n=cfg.get("sync_windows", 4), length=cfg.get("sync_window_dur", 480))
    res = []
    for (s, d) in wins:
        try:
            m, c = detect_offset_ms(path, ref_ai, path, shift_ai, start=int(s), dur=int(d))
            if m is not None:
                res.append((m, c))
        except Exception:
            pass
    for m0, _ in sorted(res, key=lambda x: -x[1]):
        agree = [(m, c) for m, c in res if abs(m - m0) <= 150]
        if len(agree) >= 2:
            return statistics.median(m for m, _ in agree), max(c for _, c in agree)
    if res:
        m, c = max(res, key=lambda x: x[1])
        if c >= max(cfg.get("auto_sync_min_conf", 0.2), 0.35):
            return m, c
    return None, 0.0


def _linfit(xs, ys):
    n = len(xs); sx = sum(xs); sy = sum(ys); sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in zip(xs, ys))
    den = n * sxx - sx * sx
    if den == 0:
        return 0.0, sy / n, 0.0
    b = (n * sxy - sx * sy) / den; a = (sy - b * sx) / n
    mean = sy / n; ss_tot = sum((y - mean) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return b, a, r2


def verify_hint(base, donor, dur, hint, cfg, tag=""):
    """Confirm a pack-mate's already-known offset with ONE window (≈5x faster than full
    detect). Returns (offset, conf, method, drift) if it agrees, else None to force full
    detect. Only for constant offsets (drift hints aren't quick-verifiable)."""
    ho, hd = hint
    if hd:
        return None
    s = (dur or 1200) * 0.45                       # one central window
    try:
        m, c = detect_offset_video_ms(
            base, donor, start=int(s), dur=int(cfg.get("sync_window_dur", 480)),
            threads=cfg.get("sync_ffmpeg_threads", 4),
            hwaccel=cfg.get("sync_hwaccel", "vaapi"),
            device=cfg.get("sync_hwaccel_device", "/dev/dri/renderD128"))
    except Exception:
        return None
    if m is not None and c >= 0.5 and abs(m - ho) <= 150:
        core.log(f"sync{tag}: pack offset {ho:+.0f}ms confirmed (1 window, conf {c:.2f})")
        return int(round(ho)), c, "pack-verify", None
    core.log(f"sync{tag}: pack offset {ho:+.0f}ms NOT confirmed (got {m}/{c:.2f}) -> full detect")
    return None


def detect(base, donor, base_ai, donor_ai, dur, cfg, tag="", on_progress=None, hint=None):
    if hint is not None:                           # try the fast pack-mate path first
        r = verify_hint(base, donor, dur, hint, cfg, tag)
        if r:
            return r
    """Returns (offset_ms|None, confidence, method, drift_ratio|None).

    Measures the video offset at several points across the movie:
      - a MAJORITY agree on one value      -> constant offset (drift=None)
      - they fall on a straight LINE        -> linear drift (framerate mismatch); returns
                                               the base offset + an o1/o2 stretch ratio
      - they're INCONSISTENT (different cut)-> reject (offset=None) so the caller tries
                                               another release / a MULTI instead of guessing
    """
    import statistics
    amin = cfg.get("auto_sync_min_conf", 0.2)
    wins = _windows(dur, n=max(5, cfg.get("sync_windows", 5)), length=cfg.get("sync_window_dur", 480))
    n = len(wins)
    vres = []                                          # (center_time_s, offset_ms, conf)
    for i, (s, d) in enumerate(wins):
        if on_progress:
            on_progress(f"sync: scanning window {i+1}/{n} (@{int(s//60)}min)")
        core.log(f"sync{tag}: window {i+1}/{n} @ {int(s)}s ({int(d)}s)")
        try:
            m, c = detect_offset_video_ms(
                base, donor, start=int(s), dur=int(d),
                threads=cfg.get("sync_ffmpeg_threads", 4),
                hwaccel=cfg.get("sync_hwaccel", "vaapi"),
                device=cfg.get("sync_hwaccel_device", "/dev/dri/renderD128"))
        except Exception as e:
            core.log(f"sync{tag}: video window {int(s)}s error: {e}"); m, c = None, 0.0
        if m is not None and c >= 0.3:
            vres.append((s + d / 2.0, m, c))
            core.log(f"sync{tag}: window {i+1}/{n} -> {int(m):+d}ms (conf {c:.2f})")
    if on_progress:
        on_progress("sync: computing offset")
    if len(vres) >= 2:
        offs = [o for _, o, _ in vres]
        # largest cluster agreeing within 150ms
        best = []
        for o0 in offs:
            cl = [(t, o, c) for t, o, c in vres if abs(o - o0) <= 150]
            if len(cl) > len(best):
                best = cl
        if len(best) >= max(2, (len(vres) + 1) // 2) and len(best) >= len(vres) * 0.6:
            off = statistics.median(o for _, o, _ in best)
            core.log(f"sync{tag}: constant {off:+.0f}ms ({len(best)}/{len(vres)} windows)")
            return int(round(off)), max(c for _, _, c in best), f"video x{len(best)}", None
        if len(vres) >= 3:                             # linear drift?
            ts = [t for t, _, _ in vres]
            b, a, r2 = _linfit(ts, offs)
            if r2 >= 0.93 and abs(b * dur) >= 300:     # meaningful, well-fit drift
                k = 1.0 + b / 1000.0                    # audio runs b ms fast per s -> stretch
                core.log(f"sync{tag}: LINEAR DRIFT {b*dur:+.0f}ms over movie, base {a:+.0f}ms, ratio {k:.6f} (R²={r2:.2f})")
                return int(round(a)), r2, "video-drift", k
        core.log(f"sync{tag}: inconsistent offsets {[int(o) for o in offs]} -> reject (different cut?)")
        return None, max((c for _, _, c in vres), default=0.0), None, None
    # audio fallback at a central window
    s, d = wins[len(wins) // 2]
    try:
        am, ac = detect_offset_ms(base, base_ai, donor, donor_ai, start=int(s), dur=int(d))
    except Exception:
        am, ac = None, 0.0
    if am is not None and ac >= max(amin, 0.35):
        return int(round(am)), ac, "audio", None
    return None, ac, None, None
