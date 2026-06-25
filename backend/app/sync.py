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


def detect(base, donor, base_ai, donor_ai, dur, cfg, tag=""):
    """Returns (offset_ms|None, confidence, method). offset>0 delays the donor track.

    A real constant offset reproduces in EVERY analysis window; a spurious peak does not.
    So we require cross-window CONSENSUS (>=2 windows agreeing within 150ms) before trusting
    an offset. Without consensus we report inconclusive (None) rather than guess — the caller
    then tries another release instead of applying a wrong shift."""
    import statistics
    vmin = cfg.get("sync_video_min_conf", 0.4)
    amin = cfg.get("auto_sync_min_conf", 0.2)
    wins = _windows(dur, n=cfg.get("sync_windows", 4), length=cfg.get("sync_window_dur", 480))
    vres = []
    for (s, d) in wins:
        try:
            m, c = detect_offset_video_ms(
                base, donor, start=int(s), dur=int(d),
                threads=cfg.get("sync_ffmpeg_threads", 4),
                hwaccel=cfg.get("sync_hwaccel", "vaapi"),
                device=cfg.get("sync_hwaccel_device", "/dev/dri/renderD128"))
        except Exception as e:
            core.log(f"sync{tag}: video window {int(s)}s error: {e}"); m, c = None, 0.0
        if m is not None:
            vres.append((m, c))
    # cross-window consensus
    consensus = None
    for m0, _ in sorted(vres, key=lambda x: -x[1]):
        agree = [(m, c) for m, c in vres if abs(m - m0) <= 150]
        if len(agree) >= 2:
            consensus = (statistics.median(m for m, _ in agree), max(c for _, c in agree), len(agree))
            break
    # audio at a central window (cross-check / fallback)
    s, d = wins[len(wins) // 2]
    am, ac = None, 0.0
    try:
        am, ac = detect_offset_ms(base, base_ai, donor, donor_ai, start=int(s), dur=int(d))
    except Exception as e:
        core.log(f"sync{tag}: audio error: {e}")
    if consensus:
        off, conf, n = consensus
        core.log(f"sync{tag}: video consensus {off:+.0f}ms across {n} windows (conf {conf:.2f})")
        return off, conf, f"video x{n}"
    # no consensus: trust a single window only if it's strong AND audio independently confirms it
    if vres:
        m, c = max(vres, key=lambda x: x[1])
        if c >= vmin and am is not None and abs(m - am) <= 150:
            return m, max(c, ac), "video+audio"
    # audio-only is least reliable — require a solid peak
    if am is not None and ac >= max(amin, 0.35):
        return am, ac, "audio"
    return None, (consensus[1] if consensus else max((c for _, c in vres), default=0.0)), None
