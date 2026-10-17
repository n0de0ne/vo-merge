"""A rate stretch has to change the AUDIO, not just its timestamps.

Every stretch used to reach the file as `mkvmerge --sync TID:d,o/p` — the PAL 25<->23.976 ratio,
the 1.001 pulldown, a fitted linear drift. mkvmerge rescales the track's TIMESTAMPS and leaves
every sample exactly as it was, so a 45-minute PAL graft carries timestamps spanning ~115 s more
(or less) than the audio inside them. No player can honour both. One that follows the samples
drifts out of sync; one that follows the timestamps has to cut or pad the audio to keep up, every
second or so — the "micro skips" reported on PAL-sourced series. Reproduced with real mkvmerge on
a pure tone: a constant offset decodes clean, and every stretch ratio breaks it.

So a stretched track is re-timed for real here: decoded, sped up or slowed down by the ratio,
re-encoded, and muxed back with only a CONSTANT offset — the one thing `--sync` does correctly for
audio. Subtitles keep the mkvmerge stretch: a cue is an instant, not a run of samples, so rescaling
its timestamp IS re-timing it.

Two callers:

- the merge (`graft_stretched`): mkvmerge stretches the donor's audio into a small audio-only file
  exactly as before, so where the track STARTS is whatever mkvmerge decided, by construction; only
  the length of the audio inside it is then fixed.
- the repair (`repair_file`): files merged before this existed carry the stretched track already.
  The samples inside it are untouched — that is the whole bug — so the original audio is still
  there, and re-timing it in place needs no re-download.

The measurement (`measure`) is the authority, not the database. It reads a track's own timestamps
against the samples it actually holds, so it finds a stretch whoever made it — this app, or a
release group that built its MULTI the same way — and it is what makes the repair idempotent: a
re-timed track measures 1.0 and is left alone.
"""
import json
import os
import shutil
import subprocess

from . import core

# A full-track |k-1| below this is not worth a re-encode: 1e-5 is 27 ms across a 45-minute episode.
STRETCH_MIN = 1e-5
# The screen reads only the head of the file, which pins k to ~1e-5, so it needs a wider margin
# before it is believed. Anything it flags is measured over the whole track before being touched.
SCREEN_MIN = 1e-4
SCREEN_S = 120
# A track whose timestamps stray further than this from ONE straight line has gaps or jumps, not a
# stretch — a broadcast capture's dropouts, say. A uniform re-time would shift the audio around
# them, so such a track is left alone and reported rather than "fixed" into a desync.
RESID_MAX = 0.040
# How the speed change is made. `tempo` keeps the pitch: each language was recorded at its own
# version's speed (a French dub is voiced against the 25 fps picture at natural pitch), so
# resampling would lower or raise every voice by ~0.7 semitone. `resample` is the exact inverse of
# an uncorrected PAL speed-up and involves no time-stretching at all, for the operator who
# prefers that trade.
METHODS = ("tempo", "resample")
CODECS = ("ac3", "eac3", "flac")
# Chunk size for `measure`. Decoding in fixed chunks is what keeps a two-hour TrueHD track — 1,200
# frames a second — to a few thousand lines of output instead of eight million.
CHUNK = 48000


class RetimeError(Exception):
    """A re-time step failed; the message says which, in terms an operator can act on."""


def _timeout(cfg):
    # A full-track decode/encode is a mux-sized job, so it shares the mux deadlock guard.
    return max(60, int(cfg.get("mux_timeout_min", 240)) * 60)


def _tail(text, n=300):
    return (text or "").strip().splitlines()[-1][:n] if (text or "").strip() else ""


def cleanup(paths):
    for p in paths or ():
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError as e:
            core.log(f"retime: could not remove {p}: {e}")


def fit(chunks):
    """[(pts_s, content_s), ...] for one track, in play order -> the line through its timeline.

    Returns {start, k, resid, dur} or None. `start` is where its first sample plays; `k` how far
    its timestamps are stretched relative to the audio they carry (1.0 = untouched — the PAL
    stretch reads 0.959041 or 1.042708); `resid` the worst distance of any chunk from that line;
    `dur` the audio actually held. `k` is taken from the endpoints rather than a least-squares
    fit, so a single gap shows up in `resid` instead of being averaged into a plausible ratio."""
    if len(chunks) < 3:
        return None
    p0 = chunks[0][0]
    xs, ys, cum = [], [], 0.0
    for pts, n in chunks:
        xs.append(cum); ys.append(pts - p0); cum += n
    if xs[-1] <= 0:
        return None
    k = ys[-1] / xs[-1]
    resid = max(abs(y - k * x) for x, y in zip(xs, ys))
    return {"start": p0, "k": k, "resid": resid, "dur": cum}


def measure(path, aidxs, cfg, seconds=None):
    """`fit` for each audio track in `aidxs` (audio-relative indexes), in ONE decode pass.

    `-copyts` matters: without it ffmpeg rebases every timestamp to the file's earliest stream,
    and each `start` would be off by that stream's lead. `seconds` reads only the head of the
    file — the cheap screen. Returns {aidx: fit-or-None}; raises RetimeError when ffmpeg can't
    read the file at all."""
    if not aidxs:
        return {}
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-copyts"]
    if seconds:
        cmd += ["-t", str(int(seconds))]
    cmd += ["-i", path]
    for a in aidxs:
        cmd += ["-map", f"0:a:{a}"]
    cmd += ["-af", f"asetnsamples=n={CHUNK}:p=0", "-ac", "1", "-c:a", "pcm_s16le",
            "-f", "framecrc", "-"]
    try:
        r = core.run_proc(cmd, capture_output=True, text=True, timeout=_timeout(cfg))
    except subprocess.TimeoutExpired:
        raise RetimeError(f"measuring the audio timeline exceeded {_timeout(cfg) // 60}min")
    if r.returncode != 0:
        raise RetimeError(f"ffmpeg could not read the audio (rc={r.returncode}: "
                          f"{_tail(r.stderr)})")
    tbs, rows = {}, {}
    for line in r.stdout.splitlines():
        if line.startswith("#tb "):                      # "#tb 0: 1/48000"
            idx, frac = line[4:].split(":", 1)
            num, den = frac.strip().split("/")
            tbs[int(idx)] = int(num) / int(den)
            continue
        if not line.strip() or line.startswith("#"):
            continue
        f = [x.strip() for x in line.split(",")]          # stream, dts, pts, duration, size, crc
        rows.setdefault(int(f[0]), []).append((int(f[2]), int(f[3])))
    out = {}
    for s, a in enumerate(aidxs):
        tb = tbs.get(s)
        out[a] = fit([(p * tb, d * tb) for p, d in rows.get(s, [])]) if tb else None
    return out


def _stream(path, aidx):
    """(sample_rate, channels) of one audio track."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", f"a:{aidx}",
                            "-show_entries", "stream=sample_rate,channels", "-of", "json", path],
                           capture_output=True, text=True, timeout=120)
        st = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
        return int(st.get("sample_rate") or 0) or None, int(st.get("channels") or 0) or None
    except Exception:
        return None, None


def rate_pair(k, lo=40000, hi=60000):
    """Integers (S, A) with S/A as close to k as the range allows.

    `asetrate` only takes an integer rate, so the obvious `asetrate=round(sr/k)` is only good to
    1/48000 — 0.15 s of drift by the end of a two-hour film. Resampling to S first and then
    relabelling as A changes the duration by exactly S/A, and searching A over a range finds a
    pair within ~1e-9 of k for any ratio this app produces."""
    best = None
    for a in range(lo, hi):
        s = round(k * a)
        err = abs(s / a - k)
        if best is None or err < best[0]:
            best = (err, s, a)
            if err < 1e-12:
                break
    return best[1], best[2]


_FILTERS = {}


def has_filter(name):
    """Whether this ffmpeg build has filter `name` (rubberband is an optional library)."""
    if name not in _FILTERS:
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True,
                               text=True, timeout=60)
            _FILTERS[name] = any(len(ln.split()) > 1 and ln.split()[1] == name
                                 for ln in r.stdout.splitlines())
        except Exception:
            _FILTERS[name] = False
    return _FILTERS[name]


def speed_filter(k, sr_out, method="tempo", rubberband=True):
    """The ffmpeg filter that makes audio last `k` times as long, ending at `sr_out`.

    Timing measured on synthesized speech, envelope against the ideal k-stretched source:
    `resample` lands within 0.2 ms, rubberband within 2 ms, atempo within 6 ms — all far inside
    lip-sync tolerance. rubberband is preferred for `tempo` because it is also the better
    stretcher; `channels=together` keeps a surround mix's channels phase-coherent. atempo is the
    fallback for a build without librubberband. (A burst train after silence makes atempo look
    ~20 ms early, which is a property of that pathological signal — compensating for it made
    real speech 16-21 ms LATE.)"""
    if method == "resample":
        s, a = rate_pair(k)
        return f"aresample={s},asetrate={a},aresample={sr_out}"
    if rubberband:
        return f"rubberband=tempo={1.0 / k:.12f}:channels=together,aresample={sr_out}"
    return f"atempo={1.0 / k:.12f},aresample={sr_out}"


def _codec_args(codec, ch):
    if codec == "flac":
        return ["-c:a", "flac"]
    # (E-)AC-3 is what every TV and receiver passes through, which keeps the re-timed track
    # direct-playable. 640k is AC-3's ceiling; both encoders stop at 5.1.
    args = ["-c:a", codec, "-b:a", "640k" if (ch or 2) > 2 else "256k"]
    if (ch or 0) > 6:
        args += ["-ac", "6"]
    return args


def render(src, aidx, k, dst, cfg):
    """Decode audio track `aidx` of `src`, make it last `k` times as long, re-encode it into
    `dst` (.mka) starting at 0.

    The input timestamps are thrown away on purpose — they are the stretched ones — and the
    output's are regenerated from the sample count (`asetpts=N/SR/TB` on both sides), so the
    result is contiguous by construction. The caller places it with a constant `--sync`."""
    method = cfg.get("retime_method", "tempo")
    method = method if method in METHODS else "tempo"
    codec = cfg.get("retime_codec", "ac3")
    codec = codec if codec in CODECS else "ac3"
    sr, ch = _stream(src, aidx)
    sr = sr or 48000
    sr_out = sr if codec == "flac" or sr in (48000, 44100, 32000) else 48000
    chain = (f"asetpts=N/SR/TB,"
             f"{speed_filter(k, sr_out, method, rubberband=has_filter('rubberband'))},"
             f"asetpts=N/SR/TB")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src, "-map", f"0:a:{aidx}",
           "-vn", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
           "-af", chain] + _codec_args(codec, ch) + [dst]
    try:
        r = core.run_proc(cmd, capture_output=True, text=True, timeout=_timeout(cfg))
    except subprocess.TimeoutExpired:
        cleanup([dst])
        raise RetimeError(f"re-encoding the audio exceeded {_timeout(cfg) // 60}min")
    except core.Aborted:
        cleanup([dst])
        raise
    if r.returncode != 0 or not os.path.exists(dst):
        cleanup([dst])
        raise RetimeError(f"ffmpeg could not re-time the audio (rc={r.returncode}: "
                          f"{_tail(r.stderr)})")


def _track_args(lang, name, start, default=False, forced=False):
    """mkvmerge options for one re-timed .mka input (its only track is id 0)."""
    args = ["--language", f"0:{lang}", "--default-track", f"0:{1 if default else 0}",
            "--no-global-tags", "--no-chapters", "--no-track-tags"]
    if forced:
        args += ["--forced-track", "0:1"]
    if name:
        args += ["--track-name", f"0:{name}"]
    return args + ["--sync", f"0:{int(round(start * 1000))}"]


def graft_stretched(donor, aud_ids, langs, names, offset, drift, workdir, cfg, tag=""):
    """mkvmerge arguments grafting the donor's audio tracks `aud_ids` at `offset` ms and
    stretched by `drift` — with the stretch applied to the audio itself.

    The stretch is first done exactly as before, by mkvmerge, into a small audio-only file:
    that pins where each track STARTS to mkvmerge's own arithmetic, so nothing about the
    placement changes. Each track is then measured there and re-timed from it. Returns
    (args, temp_files) — the caller adds `args` after the base file and removes the temp files
    once the mux has read them. Raises RetimeError."""
    from .pipeline import run_mux
    ids = sorted(aud_ids)        # mkvmerge writes a source's tracks in ITS order, not the list's
    stem = os.path.splitext(os.path.basename(donor))[0]
    os.makedirs(workdir, exist_ok=True)
    stretched = os.path.join(workdir, f".{stem}.stretched.mka")
    tmps = [stretched]
    try:
        ratio = f"{offset},{round(drift * 1000000)}/1000000"
        cmd = ["mkvmerge", "-o", stretched, "--no-video", "--no-subtitles", "--no-chapters",
               "--no-attachments", "--no-global-tags", "--no-track-tags",
               "--audio-tracks", ",".join(str(i) for i in ids)]
        for i in ids:
            cmd += ["--sync", f"{i}:{ratio}"]
        ok, err = run_mux(cmd + [donor], stretched, cfg)
        if not ok:
            raise RetimeError(f"could not extract the donor's audio ({err})")
        fits = measure(stretched, list(range(len(ids))), cfg)
        args = []
        for j, i in enumerate(ids):
            f = fits.get(j)
            if not f:
                raise RetimeError(f"could not measure donor audio track {i}")
            if f["resid"] > RESID_MAX:
                raise RetimeError(f"donor audio track {i} has timestamp jumps of up to "
                                  f"{f['resid'] * 1000:.0f} ms — a uniform re-time would desync "
                                  f"the audio around them")
            dst = os.path.join(workdir, f".{stem}.retime{j}.mka")
            tmps.append(dst)
            render(stretched, j, f["k"], dst, cfg)
            args += _track_args(langs[i], names.get(i), f["start"]) + [dst]
            core.log(f"retime{tag}: donor track {i} ({langs[i]}) re-timed x{f['k']:.6f}, "
                     f"starts at {f['start']:+.3f}s")
        return args, tmps
    except BaseException:
        cleanup(tmps)
        raise


def _same_file(st, path):
    try:
        now = os.stat(path)
    except OSError:
        return False
    return now.st_size == st.st_size and now.st_mtime_ns == st.st_mtime_ns


def repair_file(path, cfg, flagged=False, dry_run=False, tag=""):
    """Find the stretched audio tracks of one library file and re-time them in place.

    Every audio track is screened from the head of the file; only one that screens as stretched
    is read in full, and only a clean stretch — timestamps on one straight line, |k-1| above
    STRETCH_MIN — is touched. `flagged` (the record's merge says it applied a stretch) skips the
    screen and measures every track in full, so a small stretch below the screen's precision is
    still found.

    The rest of the file is kept exactly: same tracks in the same order, same names, languages
    and flags, chapters and attachments. The file is replaced only if it did not change while
    this ran and the result measures as unstretched.

    Returns {path, tracks: [...], changed, error}. Each track row carries `fix` when it is (or,
    on a dry run, would be) re-timed, otherwise a `note` saying why not."""
    res = {"path": path, "tracks": [], "changed": False, "error": None}
    try:
        j = json.loads(subprocess.run(["mkvmerge", "-J", path], capture_output=True, text=True,
                                      timeout=180).stdout or "{}")
    except Exception as e:
        res["error"] = f"mkvmerge could not read it ({e})"
        return res
    tracks = j.get("tracks") or []
    auds = [t for t in tracks if t.get("type") == "audio"]
    if not auds:
        res["error"] = "no audio track mkvmerge can read"
        return res
    try:
        idxs = list(range(len(auds)))
        if not flagged:
            screen = measure(path, idxs, cfg, seconds=SCREEN_S)
            idxs = [a for a in idxs if screen.get(a) and abs(screen[a]["k"] - 1) > SCREEN_MIN]
        full = measure(path, idxs, cfg) if idxs else {}
    except RetimeError as e:
        res["error"] = str(e)
        return res
    todo = []
    for a in idxs:
        t, f = auds[a], full.get(a)
        p = t.get("properties") or {}
        row = {"aidx": a, "tid": t["id"], "lang": p.get("language") or "und",
               "name": p.get("track_name") or ""}
        if not f:
            row["note"] = "could not measure"
        elif abs(f["k"] - 1) <= STRETCH_MIN:
            row.update(k=round(f["k"], 7), note="not stretched")
        elif f["resid"] > RESID_MAX:
            row.update(k=round(f["k"], 7),
                       note=f"timestamps jump by up to {f['resid'] * 1000:.0f} ms — gaps, not a "
                            f"stretch; left alone")
        else:
            row.update(k=round(f["k"], 7), start=round(f["start"], 3), fix=True)
            todo.append((a, t, f))
        res["tracks"].append(row)
    if dry_run or not todo:
        return res

    from .pipeline import run_mux
    workdir = os.path.join(os.path.dirname(path), "_merged")
    stem = os.path.splitext(os.path.basename(path))[0]
    out = os.path.join(workdir, f".{stem}.retime.mkv")
    tmps = []
    try:
        st0 = os.stat(path)
        need = st0.st_size * 1.05 + max(0.0, float(cfg.get("disk_floor_gb", 10))) * 1e9
        os.makedirs(workdir, exist_ok=True)
        if shutil.disk_usage(workdir).free < need:
            res["error"] = f"not enough disk space to rewrite it ({need / 1e9:.1f} GB needed)"
            return res
        cmd = ["mkvmerge", "-o", out, "--audio-tracks",
               "!" + ",".join(str(t["id"]) for _, t, _ in todo), path]
        fileno = {}
        for n, (a, t, f) in enumerate(todo, start=1):
            dst = os.path.join(workdir, f".{stem}.retime{a}.mka")
            tmps.append(dst)
            render(path, a, f["k"], dst, cfg)
            p = t.get("properties") or {}
            cmd += _track_args(p.get("language_ietf") or p.get("language") or "und",
                               p.get("track_name"), f["start"],
                               default=bool(p.get("default_track")),
                               forced=bool(p.get("forced_track"))) + [dst]
            fileno[t["id"]] = n
        # Put each re-timed track back exactly where the stretched one was.
        cmd += ["--track-order", ",".join(f"{fileno[t['id']]}:0" if t["id"] in fileno
                                          else f"0:{t['id']}" for t in tracks)]
        ok, err = run_mux(cmd, out, cfg)
        if not ok:
            res["error"] = f"remux failed: {err}"
            return res
        if not _same_file(st0, path):
            cleanup([out])
            res["error"] = "the file changed while it was being repaired — left alone"
            return res
        # The first frame may legitimately sit a little early — an (E-)AC-3 encoder's priming
        # puts it 5 ms before the audio it carries — or up to one frame late, because mkvmerge
        # drops a frame the constant offset pushes before zero. Anything beyond is a misplacement.
        check = measure(out, [a for a, _, _ in todo], cfg, seconds=SCREEN_S)
        for a, _, f in todo:
            c = check.get(a)
            if not c or abs(c["k"] - 1) > SCREEN_MIN \
                    or not -0.025 <= c["start"] - f["start"] <= 0.1:
                cleanup([out])
                res["error"] = (f"the re-timed audio track {a} did not verify "
                                f"({'unreadable' if not c else 'k=%.6f start=%+.3fs' % (c['k'], c['start'])})")
                return res
        shutil.move(out, path)
        core.forget_probe(path)
        res["changed"] = True
        core.log(f"retime{tag}: re-timed {len(todo)} audio track(s) of {os.path.basename(path)} "
                 f"({', '.join('x%.6f' % f['k'] for _, _, f in todo)})")
        return res
    except RetimeError as e:
        res["error"] = str(e)
        return res
    finally:
        cleanup(tmps + ([out] if not res["changed"] else []))
        try:
            os.rmdir(workdir)
        except OSError:
            pass
