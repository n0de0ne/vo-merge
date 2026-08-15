"""Audio/video lip-sync detection: correlate mouth movement in the PICTURE against the speech in
an AUDIO TRACK.

Every other measurement in this app is RELATIVE. `offdet_video` correlates the donor's scene cuts
against the base's; `offdet` correlates the donor's audio envelope against the base's;
`qc_grafted_audio` correlates the grafted track against the base's own track inside the output.
All three are satisfied by two files that agree with each other and are both wrong — a donor
carrying a 40s leader and a library file carrying the same leader align perfectly and are both
off — and none of them can say anything at all about a file with only ONE audio track.

This module asks a different question: is this audio in sync with what is on screen? The picture
is the ground truth, so the answer is absolute. Two things follow that nothing else here can do:

- a library file's own sync can be measured, with no donor at all;
- a "different cut, no offset aligns them" verdict becomes a number, because each file is
  measured against ITSELF instead of against the other.

## What it actually measures, and its honest limits

The visual signal is *mouth-region motion*; the audio signal is the *speech envelope* (band-limited
to 300–3400 Hz, the band that carries speech). Both are ACTIVITY signals, so what is really being
matched is "someone is speaking now" against "a mouth is moving now".

That distinction is the whole accuracy story, and it must not be oversold:

- **On original-language audio** the phonemes line up with the lips, the correlation is sharp and
  the offset is good to roughly a frame or two (±40–80 ms).
- **On a DUB it still works, but coarser.** Dubbing replaces the phonemes and cannot match them to
  the lips — but it preserves *when each character speaks*, because it is cut to the picture. So
  the activity envelopes still correlate, at roughly ±100–200 ms. That is far below what a human
  notices as "out of sync" (~120 ms audio-late), which is exactly the range this is for. It is NOT
  a lip-sync quality judgement — it cannot tell you a dub is badly matched, only that it is
  displaced in time.
- **It fails, and says so, on**: films with almost no on-screen dialogue, heavy narration over
  cutaways, animation whose mouths are drawn on 2s or barely move, and windows landing on action
  or music. That is what the multi-window consensus is for — windows that don't resolve are
  discarded, and a verdict needs `lipsync_min_windows` of them to agree.

## How the mouth is found without a face detector

A face detector is the obvious approach and would be one more dependency (see `_cv2_face`, used
automatically when opencv is importable). The dependency-free path exploits the thing that makes a
talking mouth unique in a frame: it modulates at the SYLLABLE rate, 2–8 Hz. So instead of looking
for a face, we look for pixels whose motion energy lives in that band:

  decode small grey frames  →  per-pixel |temporal difference|  →  per-pixel FFT
  →  keep pixels whose 2–8 Hz band holds a high share of their total motion energy
  →  those pixels' summed motion IS the visual speech signal

Global motion (a pan, a camera shake, a cut) moves every pixel at once, so the per-frame median is
subtracted before the band-pass — otherwise a handheld shot is the strongest "speaker" in frame.
"""
import subprocess

import numpy as np

from . import core, media

# Speech modulates the mouth at the syllable rate. This band is what separates a talking face from
# a flickering fire, a rippling lake or a panning camera — all of which have plenty of motion
# energy, just not here.
SYLLABLE_LO, SYLLABLE_HI = 2.0, 8.0

# Windows whose visual or audio signal is too weak to mean anything. Below these there is nothing
# to correlate and a confident-looking number would be noise.
MIN_VISUAL_STD = 1e-4
MIN_SPEECH_FRAC = 0.05           # fraction of the window that must look like speech


def available(cfg=None):
    """Whether lip-sync can run at all. ffmpeg is the only hard requirement; opencv is a bonus."""
    return bool((cfg or {}).get("lipsync_enabled", True))


def _cv2():
    """opencv, if it happens to be installed. Deliberately NOT a dependency — see
    requirements.txt. Everything here works without it; a cascade only sharpens the region search.

    Note the API risk this guard absorbs: OpenCV 5 removed `CascadeClassifier` and the bundled
    cascade data outright, so on a 5.x install `_cv2_face` raises and returns None rather than
    working. That is the whole reason this is optional and lazily imported."""
    try:
        import cv2
        return cv2 if hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data") else None
    except Exception:
        return None


# --------------------------------------------------------------------------- signal extraction
def _frames(path, start, dur, fps, w=128, h=72, threads=2, timeout=None):
    """A window of the video as a (T, h, w) float array of luma, decoded small and cheap.

    Deliberately software-decoded at this size: the frames are tiny, the filter chain is a scale
    and a format conversion, and the VAAPI path's win (keeping 4K surfaces off the CPU) is
    irrelevant when we are asking for 128x72 at 12fps. Avoiding hwaccel here also avoids the
    class of failure that made `scene_cuts` need a software fallback in the first place."""
    cmd = ["nice", "-n", "19", "ffmpeg", "-v", "error", "-threads", str(threads),
           "-ss", str(start), "-t", str(dur), "-i", path,
           "-vf", f"fps={fps},scale={w}:{h}", "-pix_fmt", "gray",
           "-an", "-sn", "-f", "rawvideo", "-"]
    try:
        p = core.run_proc(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return np.zeros((0, h, w))
    n = len(p.stdout) // (w * h)
    if n < 8:
        return np.zeros((0, h, w))
    a = np.frombuffer(p.stdout[:n * w * h], dtype=np.uint8).astype(np.float64)
    return a.reshape(n, h, w) / 255.0


def _speech_env(path, ai, start, dur, fps, timeout=None):
    """The audio track's speech envelope, resampled to the video frame rate.

    `highpass`/`lowpass` restrict this to the band speech actually occupies, so a loud score or an
    explosion contributes far less than a voice does. Without it the envelope is dominated by the
    effects bed — which is precisely the signal `offdet` uses to match two files to each OTHER,
    and precisely the wrong signal for matching audio to a mouth."""
    sr = 16000
    cmd = ["nice", "-n", "19", "ffmpeg", "-v", "error", "-threads", "2",
           "-ss", str(start), "-t", str(dur), "-i", path, "-map", f"0:a:{ai}",
           "-af", "highpass=f=300,lowpass=f=3400", "-ac", "1", "-ar", str(sr),
           "-f", "f32le", "-"]
    try:
        p = core.run_proc(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return np.array([])
    x = np.frombuffer(p.stdout, dtype=np.float32).astype(np.float64)
    if x.size < sr:
        return np.array([])
    hop = int(round(sr / fps))
    n = x.size // hop
    if n < 8:
        return np.array([])
    frames = x[:n * hop].reshape(n, hop)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    # Log compression: speech is enormously peaky, and a linear envelope lets one shout outweigh a
    # whole conversation. What we want to correlate is the PATTERN of speaking, not its loudness.
    return np.log1p(rms / (np.median(rms) + 1e-9))


def _cv2_face(frames, cv2):
    """A (y0, y1, x0, x1) box around the mouth of the most consistently detected face, or None.

    Only the lower half of the face box is kept — eyes and brows move plenty and none of it is
    speech. Detection runs on a handful of frames, not all of them: a face that appears in a few
    sampled frames is where the dialogue is, and running a cascade over 300 frames is the one way
    to make this path slower than the dependency-free one."""
    try:
        casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if casc.empty():
            return None
        h, w = frames.shape[1], frames.shape[2]
        # The cascade wants a reasonable pixel count; our frames are 128x72, so upscale to detect.
        best, scale = None, 4
        for idx in np.linspace(0, len(frames) - 1, 12).astype(int):
            img = (frames[idx] * 255).astype(np.uint8)
            big = cv2.resize(img, (w * scale, h * scale), interpolation=cv2.INTER_LINEAR)
            faces = casc.detectMultiScale(big, 1.1, 4, minSize=(40, 40))
            for (fx, fy, fw, fh) in faces:
                if best is None or fw * fh > best[2] * best[3]:
                    best = (fx, fy, fw, fh)
        if best is None:
            return None
        fx, fy, fw, fh = (v / scale for v in best)
        y0 = int(max(0, fy + fh * 0.55))          # lower half of the face = the mouth
        y1 = int(min(h, fy + fh * 1.05))
        x0 = int(max(0, fx + fw * 0.15))
        x1 = int(min(w, fx + fw * 0.85))
        if y1 - y0 < 2 or x1 - x0 < 2:
            return None
        return y0, y1, x0, x1
    except Exception:
        return None


def _visual_signal(frames, fps):
    """Turn a window of frames into one "a mouth is moving" signal per frame.

    Returns (signal, found_face). See the module docstring for why the band-pass selection works:
    a talking mouth is the thing in frame whose motion energy sits at the syllable rate."""
    if len(frames) < 8:
        return np.array([]), False
    d = np.abs(np.diff(frames, axis=0))                    # (T-1, h, w) motion volume
    # A pan, a shake or a cut moves everything at once. Removing the per-frame median makes the
    # signal about what moves DIFFERENTLY from the frame as a whole.
    d = d - np.median(d, axis=(1, 2), keepdims=True)
    d = np.clip(d, 0, None)

    cv2 = _cv2()
    box = _cv2_face(frames, cv2) if cv2 is not None else None
    if box:
        y0, y1, x0, x1 = box
        region = d[:, y0:y1, x0:x1]
        sig = region.reshape(len(region), -1).mean(axis=1)
        return sig - sig.mean(), True

    # No face detector: find the pixels whose motion is rhythmic at speech rate.
    t = len(d)
    flat = d.reshape(t, -1)
    flat = flat - flat.mean(axis=0, keepdims=True)
    spec = np.abs(np.fft.rfft(flat, axis=0)) ** 2
    freqs = np.fft.rfftfreq(t, d=1.0 / fps)
    band = (freqs >= SYLLABLE_LO) & (freqs <= SYLLABLE_HI)
    if not band.any():
        sig = flat.mean(axis=1)
        return sig - sig.mean(), False
    total = spec[1:].sum(axis=0) + 1e-12                   # skip DC
    share = spec[band].sum(axis=0) / total
    energy = flat.var(axis=0)
    # Both factors matter: a pixel with a high syllable-band SHARE but no energy is noise, and a
    # high-energy pixel with a flat spectrum is a moving object, not a mouth.
    score = share * np.sqrt(energy)
    k = max(12, score.size // 200)                         # ~0.5% of pixels: a mouth-sized blob
    keep = np.argpartition(score, -k)[-k:]
    sig = flat[:, keep].mean(axis=1)
    return sig - sig.mean(), False


def _xcorr(vis, env, fps, max_lag_s):
    """Slide the visual signal over a LONGER speech envelope and report the best alignment.

    The two inputs are deliberately different lengths. `vis` covers one window of picture; `env`
    covers that window plus `max_lag_s` of audio on each side, so the search range is set by how
    much extra AUDIO we decoded rather than by how much video we did. That asymmetry is the whole
    trick: audio decode is nearly free, video decode is not, so a ±60s search costs one extra
    ffmpeg audio pass instead of five times the frames.

    Normalised per lag against the LOCAL audio norm (via a cumulative sum), not against the whole
    envelope — otherwise a window containing one loud passage scores every lag near it highly and
    the peak lands on volume rather than on alignment.

    **Sign convention, used everywhere downstream:** the returned value is the CORRECTION — what
    `mkvmerge --sync` should add to the audio's timestamps. Negative means the audio currently
    plays late and must be pulled earlier. Returns (correction_ms, confidence)."""
    n = len(vis)
    lagf = int(round(max_lag_s * fps))
    if n < 16 or len(env) < n + 2 * lagf:
        return None, 0.0
    v = vis - vis.mean()
    nv = float(np.linalg.norm(v))
    if nv <= 0:
        return None, 0.0
    # Sliding dot product of v over env, for every start position 0 … 2*lagf.
    corr = np.correlate(env, v, mode="valid")            # length len(env) - n + 1
    # Local mean/norm of each aligned segment of env, from cumulative sums — O(len(env)).
    c1 = np.concatenate(([0.0], np.cumsum(env)))
    c2 = np.concatenate(([0.0], np.cumsum(env * env)))
    starts = np.arange(len(corr))
    s1 = c1[starts + n] - c1[starts]
    s2 = c2[starts + n] - c2[starts]
    # v is mean-removed, so subtracting the segment mean from the dot product is exact.
    num = corr - (s1 * v.mean())                          # v.mean() is ~0; kept for clarity
    denom = np.sqrt(np.maximum(s2 - s1 * s1 / n, 1e-12)) * nv
    # The envelope is decoded with a little slack past the pad (whole seconds, and the visual
    # signal is one frame shorter than its window because it is a difference). Trim to exactly the
    # lags we claim to search, or a peak just outside the stated range would be reported as if it
    # were inside it.
    score = (num / denom)[:2 * lagf + 1]
    j = int(np.argmax(score))
    conf = float(score[j])
    # Segment j starts `lagf - j` frames of audio EARLIER than the picture window, i.e. the audio
    # that belongs at picture time t currently sits at t + (j/fps - max_lag_s).
    displacement_s = j / fps - max_lag_s
    return -displacement_s * 1000.0, max(0.0, conf)


# --------------------------------------------------------------------------- the public call
def measure(path, ai=0, dur=None, cfg=None, tag="", on_progress=None,
            audio_path=None, max_lag_s=None):
    """Measure how far an audio track is displaced from the picture, in ms.

    The returned `offset_ms` is the CORRECTION: what to hand `/set_sync`. Negative means the audio
    plays late and must be pulled earlier.

    `audio_path` is the important parameter. Left unset, this measures a file against ITSELF —
    "is this file's own audio in sync", which nothing else in the app can ask. Set to a donor, it
    measures **the base's picture against the donor's audio directly**, which is the number the
    merge actually needs: the shift to apply to the donor's track so it lands on the base's
    timeline. That is a single measurement, not a difference of two, and it deliberately never
    compares the two PICTURES — which is exactly the comparison that fails on a different cut.

    `offset_ms` is None whenever the windows did not agree, which is a real and common answer —
    see the module docstring on what this cannot measure."""
    cfg = cfg or core.load_config()
    fps = int(cfg.get("lipsync_fps", 12))
    wdur = int(cfg.get("lipsync_window_dur", 24))
    nwin = int(cfg.get("lipsync_windows", 6))
    max_lag = float(max_lag_s if max_lag_s is not None else cfg.get("lipsync_max_lag_s", 4.0))
    min_conf = float(cfg.get("lipsync_min_conf", 0.30))
    need = int(cfg.get("lipsync_min_windows", 3))
    timeout = int(cfg.get("sync_decode_timeout_min", 30)) * 60
    apath = audio_path or path

    if not dur:
        info = media.probe(path)
        dur = (info or {}).get("dur") or 0
    if not dur or dur < wdur * 2:
        return {"offset_ms": None, "confidence": 0.0, "windows": [], "agreed": 0, "tested": 0,
                "method": "lipsync", "face": False,
                "note": "the file is too short to sample"}

    # Skip the first and last 8% — logos, credits and black are exactly where there is no face and
    # no dialogue, and a window that resolves nothing still costs a decode. The `max_lag` floor
    # keeps the audio padding SYMMETRIC: clamped at 0 it would silently shift the lag mapping and
    # every offset would be wrong by however much was clipped.
    lo, hi = max(dur * 0.08, max_lag + 1.0), dur * 0.92
    starts = list(np.linspace(lo, max(lo + wdur, hi - wdur), max(2, nwin)))

    wins, faces = [], 0
    for i, st in enumerate(starts):
        if on_progress:
            on_progress(f"lip-sync: window {i + 1}/{len(starts)}")
        frames = _frames(path, st, wdur, fps, threads=int(cfg.get("sync_ffmpeg_threads", 4)),
                         timeout=timeout)
        vis, had_face = _visual_signal(frames, fps)
        faces += 1 if had_face else 0
        # The audio window is padded by max_lag on both sides — that padding IS the search range,
        # and it costs an audio decode rather than a video one.
        env = _speech_env(apath, ai, st - max_lag, wdur + 2 * max_lag + 1, fps, timeout=timeout)
        rec = {"start": round(float(st), 1), "offset_ms": None, "conf": 0.0, "used": False,
               "why": None}
        if vis.size == 0 or env.size == 0:
            rec["why"] = "no usable signal (decode failed or the window is silent/still)"
            wins.append(rec)
            continue
        if float(np.std(vis)) < MIN_VISUAL_STD:
            rec["why"] = "nothing in frame moves at speech rate"
            wins.append(rec)
            continue
        if float((env > env.mean()).mean()) < MIN_SPEECH_FRAC:
            rec["why"] = "almost no speech in this window"
            wins.append(rec)
            continue
        off, conf = _xcorr(vis, env, fps, max_lag)
        rec["offset_ms"] = None if off is None else int(round(off))
        if off is None:
            rec["why"] = "not enough audio decoded to search the full lag range"
        rec["conf"] = round(conf, 3)
        rec["used"] = off is not None and conf >= min_conf
        if not rec["used"] and off is not None:
            rec["why"] = f"correlation {conf:.2f} below the {min_conf:.2f} floor"
        wins.append(rec)

    used = [w for w in wins if w["used"]]
    method = "lipsync+cv2" if faces else "lipsync"
    if len(used) < need:
        return {"offset_ms": None, "confidence": 0.0, "windows": wins, "agreed": len(used),
                "tested": len(wins), "method": method, "face": bool(faces),
                "note": (f"only {len(used)} of {len(wins)} windows resolved (need {need}). "
                         "That is the expected answer for a film with little on-screen dialogue, "
                         "for animation, or for a track that is narration rather than speech.")}

    # Consensus, not mean: one window locking onto a musical phrase would drag an average. The
    # median is the value, and the spread around it is what decides whether to believe it.
    offs = np.array([w["offset_ms"] for w in used], dtype=float)
    med = float(np.median(offs))
    spread = float(np.median(np.abs(offs - med)))
    agree = [w for w in used if abs(w["offset_ms"] - med) <= max(120.0, 2.5 * spread)]
    if len(agree) < need:
        return {"offset_ms": None, "confidence": 0.0, "windows": wins, "agreed": len(agree),
                "tested": len(wins), "method": method, "face": bool(faces),
                "note": (f"windows resolved but disagreed (spread ±{int(spread)}ms). A real "
                         "displacement is constant across the runtime; a varying one means the "
                         "audio is stretched, not shifted — try the rate test.")}
    val = float(np.median([w["offset_ms"] for w in agree]))
    conf = float(np.mean([w["conf"] for w in agree]))
    core.log(f"lipsync{tag}: {int(val):+d}ms from {len(agree)}/{len(wins)} windows "
             f"(conf {conf:.2f}, {'face-tracked' if faces else 'band-pass region'})")
    return {"offset_ms": int(round(val)), "confidence": round(conf, 3), "windows": wins,
            "agreed": len(agree), "tested": len(wins), "method": method, "face": bool(faces),
            "spread_ms": int(spread),
            "note": ("measured against the picture, so this is an absolute reading — it does not "
                     "depend on any other file. On a dubbed track it is accurate to roughly "
                     "±150ms, because dubbing matches when people speak rather than how their "
                     "lips move.")}


def check(path, tracks=None, cfg=None, tag=""):
    """Measure every audio track of one file. This is what makes a library file's OWN sync
    knowable: a file whose French track reads +0ms and whose grafted English track reads +900ms
    has a bad graft, and nothing else in this app could ever have told you that."""
    cfg = cfg or core.load_config()
    info = media.probe(path)
    if not info:
        return {"path": path, "error": "could not probe the file", "tracks": []}
    auds = info.get("auds") or []
    idx = range(len(auds)) if tracks is None else tracks
    out = []
    for i in idx:
        if i >= len(auds):
            continue
        r = measure(path, i, info.get("dur"), cfg, tag=f"{tag} a:{i}")
        r["track"] = {"index": i, "lang": auds[i].get("lang"), "name": auds[i].get("name")}
        out.append(r)
    return {"path": path, "dur": info.get("dur"), "fps": info.get("fps"), "tracks": out}


def verify_graft(out_path, n_base_auds, cfg, tag=""):
    """Post-merge gate: is the FIRST GRAFTED track in sync with the picture?

    `qc_grafted_audio` already compares the grafted track to the base's own track, which catches a
    graft that landed differently from the base. It cannot catch the case where base and graft are
    equally displaced, and it has nothing to say when the base track is itself wrong. This does,
    because the picture is the reference.

    Returns (ok, offset_ms, confidence). Inconclusive ACCEPTS, exactly like the existing QC:
    absence of evidence is not evidence of misalignment, and a quiet film must not burn its retry
    budget on a measurement that was never going to resolve."""
    if not cfg.get("lipsync_qc"):
        return True, 0, 0.0
    limit = int(cfg.get("qc_max_offset_ms", 1500))
    r = measure(out_path, n_base_auds, None, cfg, tag=tag)
    off, conf = r.get("offset_ms"), r.get("confidence") or 0.0
    if off is None or conf < float(cfg.get("lipsync_min_conf", 0.30)):
        return True, 0, conf                      # inconclusive -> accept
    if abs(off) > limit:
        core.log(f"lipsync QC{tag}: grafted track is {off:+d}ms out against the picture "
                 f"(conf {conf:.2f}) -> rejecting the merge")
        return False, int(off), conf
    return True, int(off), conf


def rescue(base, donor, dur, cfg, tag="", on_progress=None, donor_ai=0, max_lag_s=None):
    """The sync-ladder rung: when the windows and the rate test have both failed, correlate the
    BASE's picture against the DONOR's audio directly.

    This is one measurement, not a difference of two, and that matters. Measuring each file
    against its own picture would give each file's internal displacement but say nothing about the
    content offset BETWEEN them, which is the number the merge needs. Correlating the base's
    mouths against the donor's speech gives that number in a single step — and it never compares
    the two PICTURES, which is precisely the comparison that fails on a different cut.

    The search range is set by how much extra donor AUDIO is decoded around each window, so it can
    be widened cheaply: `sync_probe_lag_s` would need a longer video window, but a ±60s audio pad
    costs one more ffmpeg audio pass. It still cannot rescue a genuinely different EDIT — material
    inserted mid-runtime displaces the windows by different amounts, they disagree, and nothing is
    reported. That is the correct outcome, not a limitation.

    Returns (offset_ms, confidence) or (None, 0.0). Positive means the donor's track needs to be
    pushed later; the value goes straight to `--sync`."""
    if not cfg.get("lipsync_rescue", True) or not cfg.get("lipsync_enabled", True):
        return None, 0.0
    if on_progress:
        on_progress("sync: reading the library's lips against the donor's speech")
    lag = float(max_lag_s if max_lag_s is not None
                else max(cfg.get("lipsync_max_lag_s", 4.0), 60.0))
    r = measure(base, donor_ai, dur, cfg, tag=f"{tag} pair", on_progress=on_progress,
                audio_path=donor, max_lag_s=lag)
    if r.get("offset_ms") is None:
        core.log(f"sync{tag}: lip-sync rescue found nothing ({r.get('note')})")
        return None, 0.0
    core.log(f"sync{tag}: lip-sync rescue — donor speech vs library picture "
             f"{r['offset_ms']:+d}ms (conf {r['confidence']:.2f}, {r['agreed']}/{r['tested']} "
             f"windows, searched ±{int(lag)}s)")
    return int(r["offset_ms"]), float(r["confidence"])


def remedy(kind, ident, cfg, apply=False):
    """The Problems-page remedy. Measures the pair and, with `apply`, stores the offset as a
    deliberate instruction and re-queues the merge. Returns (ok, message)."""
    import os
    from . import pipeline
    rec = core.get_movie(ident) if kind == "movie" else core.get_episode(ident)
    if not rec:
        return False, "record not found"
    base, donor = rec.get("french_path"), rec.get("en_file")
    if not (base and os.path.exists(base)):
        return False, "the library file is not on disk"
    if donor and os.path.exists(donor):
        off, conf = rescue(base, donor, None, dict(cfg, lipsync_rescue=True), tag=f" {ident}")
        if off is None:
            return False, ("lip-sync could not read one of the files — too little on-screen "
                           "dialogue, or a genuinely different edit")
        if not apply:
            return True, f"lip-sync says {off:+d}ms (conf {conf:.2f})"
        setter = core.set_status if kind == "movie" else core.set_ep_status
        setter(ident, rec["status"], sync_offset_ms=int(off), sync_manual=1, error=None)
        queued, note = pipeline.enqueue_merge(kind, ident)
        return queued, f"lip-sync {off:+d}ms (conf {conf:.2f}) — {note}"
    # No donor: still worth reading, because it says whether the LIBRARY file is the problem.
    r = check(base, cfg=cfg, tag=f" {ident}")
    good = [t for t in r.get("tracks", []) if t.get("offset_ms") is not None]
    if not good:
        return False, "no donor on disk, and the library file's own sync could not be read"
    worst = max(good, key=lambda t: abs(t["offset_ms"]))
    return True, (f"no donor to merge; the library file's own tracks read "
                  + ", ".join(f"{t['track']['lang'] or '?'} {t['offset_ms']:+d}ms"
                              for t in good)
                  + (f" — {worst['track']['lang'] or '?'} is the one out of sync"
                     if abs(worst["offset_ms"]) > 200 else " — all within tolerance"))
