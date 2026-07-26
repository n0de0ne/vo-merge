"""File-level truth about a library file: which audio and subtitle languages it ACTUALLY has.

Why this module exists
----------------------
The gap decision used to be taken from metadata other tools had written down:
`scan` trusted a Radarr **tag** maintained by an external host script, and `tv.scan` trusted
Sonarr's `mediaInfo.audioLanguages`. Both are snapshots of whatever those tools parsed at
import time, so they drift and — worse — they fail *silently*:

- `mediaInfo` is empty for anything Sonarr never analysed, and the old `_no_eng()` returned
  False for empty audio, so those files were quietly treated as "already has English".
- A file remuxed or replaced outside the *arrs keeps its stale languages until a rescan.
- A track tagged `und` (very common on French rips) carries no language at all, so no amount
  of metadata reading can classify it.

So: read the container. `mkvmerge -J` reports every track's language, name and flags, and it
handles mkv/mp4/avi/ts alike. That is the only source that cannot be stale.

`und` tracks
------------
A file whose only audio track is `und` is genuinely unclassifiable from tags. Rather than
guess, we look at the track NAME and then the FILE NAME for an explicit human marker
("VFF", "TRUEFRENCH", "English"...). If there is still nothing, the language stays `und` and
the caller decides: `has_lang()` answers "no" for a wanted language, which makes the file a
gap. That errs toward offering to add a real English track — harmless if the und track turned
out to be English, whereas the opposite error leaves a French-only file forever.

Subtitle markers are deliberately NOT read from filenames: "VOSTFR" means original audio with
French subs, so the very tokens that look French describe the subtitle track, not the audio.
Reading them as audio hints is how you conclude a Japanese file is French.
"""
import json, os, re, subprocess
from . import core

VIDEXT = (".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv")

# ISO 639 is a mess: mkvmerge may report 639-2/B ("fre"), 639-2/T ("fra"), 639-1 ("fr"), or a
# plain English name. Everything is normalised to one canonical 3-letter code per language so
# set comparisons ("is eng present?") can't be defeated by spelling.
_ALIAS = {
    "eng": "eng", "en": "eng", "english": "eng", "anglais": "eng",
    "fre": "fre", "fra": "fre", "fr": "fre", "french": "fre", "francais": "fre",
    "français": "fre",
    "jpn": "jpn", "ja": "jpn", "jp": "jpn", "japanese": "jpn", "japonais": "jpn",
    "spa": "spa", "es": "spa", "esp": "spa", "spanish": "spa", "castellano": "spa",
    "ger": "ger", "deu": "ger", "de": "ger", "german": "ger", "allemand": "ger",
    "ita": "ita", "it": "ita", "italian": "ita",
    "por": "por", "pt": "por", "portuguese": "por",
    "rus": "rus", "ru": "rus", "russian": "rus",
    "kor": "kor", "ko": "kor", "korean": "kor",
    "zho": "zho", "chi": "zho", "zh": "zho", "cmn": "zho", "chinese": "zho", "mandarin": "zho",
    "nld": "nld", "dut": "nld", "nl": "nld", "dutch": "nld",
    "swe": "swe", "sv": "swe", "swedish": "swe",
    "nor": "nor", "no": "nor", "nob": "nor", "norwegian": "nor",
    "dan": "dan", "da": "dan", "danish": "dan",
    "fin": "fin", "fi": "fin", "finnish": "fin",
    "isl": "isl", "ice": "isl", "icelandic": "isl",
    "pol": "pol", "pl": "pol", "polish": "pol",
    "ces": "ces", "cze": "ces", "cs": "ces", "czech": "ces",
    "slk": "slk", "slo": "slk", "sk": "slk", "slovak": "slk",
    "hun": "hun", "hu": "hun", "hungarian": "hun",
    "ell": "ell", "gre": "ell", "el": "ell", "greek": "ell",
    "ron": "ron", "rum": "ron", "ro": "ron", "romanian": "ron",
    "tur": "tur", "tr": "tur", "turkish": "tur",
    "ara": "ara", "ar": "ara", "arabic": "ara",
    "heb": "heb", "he": "heb", "hebrew": "heb",
    "hin": "hin", "hi": "hin", "hindi": "hin",
    "tha": "tha", "th": "tha", "thai": "tha",
    "vie": "vie", "vi": "vie", "vietnamese": "vie",
    "ind": "ind", "id": "ind", "indonesian": "ind",
    "ukr": "ukr", "uk": "ukr", "ukrainian": "ukr",
    "cat": "cat", "ca": "cat", "catalan": "cat",
    "und": "und", "": "und", "mis": "und", "zxx": "und", "unknown": "und", "undetermined": "und",
}

# Human markers found in TRACK names (and, as a last resort, file names). Deliberately narrow:
# every pattern here must be unambiguous about the AUDIO language. `vf`/`fr` are safe as whole
# tokens because "vostfr"/"subfrench" have no word boundary before their "fr".
_NAME_HINTS = (
    (re.compile(r'(?<![a-z])(vff|vfq|vfi|vf|truefrench|french|francais|français)(?![a-z])', re.I), "fre"),
    (re.compile(r'(?<![a-z])(english|anglais|eng)(?![a-z])', re.I), "eng"),
    (re.compile(r'(?<![a-z])(japanese|japonais|jpn)(?![a-z])', re.I), "jpn"),
    (re.compile(r'(?<![a-z])(spanish|espanol|español|castellano)(?![a-z])', re.I), "spa"),
    (re.compile(r'(?<![a-z])(german|allemand|deutsch)(?![a-z])', re.I), "ger"),
    (re.compile(r'(?<![a-z])(italian|italiano)(?![a-z])', re.I), "ita"),
)
# "VOSTFR"/"VOST"/"SUBFRENCH" describe SUBTITLES. If a filename carries one of these, the audio
# is the ORIGINAL language — so a French hint from the same filename must not be trusted.
_SUB_ONLY_FR = re.compile(r'(?<![a-z])(vostfr|vost|sub\s?french|subfrench|sous[- ]titr)', re.I)


def norm_lang(v):
    """Any spelling of a language -> one canonical 3-letter code ('und' when unknown)."""
    s = str(v or "").strip().lower()
    if s in _ALIAS:
        return _ALIAS[s]
    s = re.split(r'[-_;(]', s)[0].strip()          # 'pt-BR', 'eng (commentary)'
    return _ALIAS.get(s, s[:3] if s else "und")


def hint_lang(text, allow_fr=True):
    """Language implied by a human-written track/file name, or None. `allow_fr=False` suppresses
    the French hints — used for filenames that also say VOSTFR, where 'FR' is about subtitles."""
    for rx, code in _NAME_HINTS:
        if code == "fre" and not allow_fr:
            continue
        if rx.search(text or ""):
            return code
    return None


def _track_lang(props, name):
    """A track's language: its tag if it has one, else a marker in its own name."""
    lang = norm_lang(props.get("language_ietf") or props.get("language"))
    if lang != "und":
        return lang
    return hint_lang(name) or "und"


def probe(path):
    """Full track inventory for one media file, or None if it can't be read.

    Returns {dur, fps, auds: [...], subs: [...]}. Audio/subtitle entries carry the mkvmerge
    track `id` (what --audio-tracks/--subtitle-tracks take), the resolved `lang`, the codec,
    and the flags that decide whether a subtitle track is a usable full translation."""
    try:
        j = json.loads(subprocess.run(["mkvmerge", "-J", path], capture_output=True,
                                      text=True, timeout=180).stdout)
    except Exception:
        return None
    base = os.path.basename(path)
    # A filename that advertises French subs is telling us the audio is NOT French.
    allow_fr = not _SUB_ONLY_FR.search(base)
    auds, subs = [], []
    for t in j.get("tracks", []):
        p = t.get("properties", {}) or {}
        name = p.get("track_name") or ""
        lang = _track_lang(p, name)
        if t.get("type") == "audio":
            auds.append({"id": t["id"], "lang": lang, "codec": t.get("codec"),
                         "ch": p.get("audio_channels"), "name": name,
                         "default": bool(p.get("default_track")),
                         "forced": bool(p.get("forced_track"))})
        elif t.get("type") == "subtitles":
            subs.append({"id": t["id"], "lang": lang, "codec": t.get("codec"), "name": name,
                         "forced": bool(p.get("forced_track")),
                         "sdh": bool(p.get("flag_hearing_impaired"))
                                or bool(re.search(r'\b(sdh|cc|hearing)\b', name, re.I)),
                         "default": bool(p.get("default_track"))})
    # Last resort for a file with exactly ONE untagged, unnamed audio track: the filename.
    if len(auds) == 1 and auds[0]["lang"] == "und":
        h = hint_lang(base, allow_fr=allow_fr)
        if h:
            auds[0]["lang"] = h
            auds[0]["from_filename"] = True
    dur = (j.get("container", {}).get("properties", {}) or {}).get("duration")
    return {"dur": (dur / 1e9 if isinstance(dur, (int, float)) else None),
            "fps": _fps(path), "auds": auds, "subs": subs}


def _fps(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=avg_frame_rate",
                              "-of", "default=nk=1:nw=1", path],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        n, d = out.split("/")
        return round(float(n) / float(d), 3) if float(d) else None
    except Exception:
        return None


def langs(info):
    """(audio langs, subtitle langs) as sets of canonical codes; 'und' is excluded because an
    untyped track proves nothing about which languages are present."""
    if not info:
        return set(), set()
    return ({a["lang"] for a in info["auds"]} - {"und"},
            {s["lang"] for s in info["subs"]} - {"und"})


def sub_rank(s):
    """Sort key for picking WHICH subtitle track to graft when a release ships several.
    A full translation beats a forced/signs-only track, which beats an SDH variant; image
    subs (VobSub/PGS) lose to text so a text track wins when both exist."""
    image = 1 if re.search(r'(pgs|vobsub|dvd|hdmv)', str(s.get("codec") or ""), re.I) else 0
    signs = 1 if re.search(r'\b(signs?|songs?|s&s)\b', s.get("name") or "", re.I) else 0
    return (1 if s.get("forced") else 0, 1 if s.get("sdh") else 0, signs, image, s["id"])


def has_lang(info, code):
    """True if the file definitely carries `code` as an audio language."""
    a, _ = langs(info)
    return norm_lang(code) in a


def wanted_subs(donor_info, base_info, want, limit=2):
    """Donor subtitle tracks worth adding: a wanted language the base file doesn't already have,
    best variant first, capped at `limit` (anime packs routinely carry 6+ English sub tracks)."""
    if not donor_info or not want:
        return []
    _, have = langs(base_info)
    out = []
    for code in want:
        if code in have:
            continue
        cands = sorted((s for s in donor_info["subs"] if s["lang"] == code), key=sub_rank)
        out += cands[:max(1, limit)]
    return out


def wanted_audio(donor_info, base_info, want, extra=()):
    """Donor audio tracks worth adding: a target language the base lacks. One track per language
    (the best-channel variant), so a 3-dub release doesn't triple the file size. `extra` carries
    the original-language VO codes, which stay eligible as the fallback when a title's original
    language isn't in the profile and no English exists."""
    if not donor_info:
        return []
    have, _ = langs(base_info)
    ok = {norm_lang(x) for x in want} | {norm_lang(x) for x in extra}
    out, taken = [], set(have)
    for a in sorted(donor_info["auds"], key=lambda t: -(t.get("ch") or 0)):
        if a["lang"] == "und" or a["lang"] in taken or a["lang"] not in ok:
            continue
        out.append(a); taken.add(a["lang"])
    return sorted(out, key=lambda t: t["id"])


# ------------------------------------------------------------------ audit (cached)
def audit(path, refresh=False):
    """(audio langs, subtitle langs, error) for a library file, read from the container and
    cached until the file's size/mtime change. `error` is a string when the file couldn't be
    read at all — that must NOT be mistaken for "has no English", so callers skip it."""
    if not refresh:
        row = core.get_probe(path)
        if row:
            return (_split(row["auds"]), _split(row["subs"]), row["err"])
    info = probe(path)
    if not info:
        core.put_probe(path, err="unreadable")
        return set(), set(), "unreadable"
    a, s = langs(info)
    if not info["auds"]:
        core.put_probe(path, dur=info["dur"], fps=info["fps"], err="no audio track")
        return set(), set(), "no audio track"
    core.put_probe(path, dur=info["dur"], fps=info["fps"], auds=",".join(sorted(a)),
                   subs=",".join(sorted(s)), ntracks=len(info["auds"]))
    return a, s, None


def _split(v):
    return {x for x in (v or "").split(",") if x}


_PROFILES = {"movie":  {"audio": ["fre", "eng"],        "subs": ["fre", "eng"]},
             "series": {"audio": ["fre", "eng"],        "subs": ["fre", "eng"]},
             "anime":  {"audio": ["fre", "eng", "jpn"], "subs": ["fre", "eng"]}}


def profile(kind, cfg):
    """The target language set for a kind of title ("movie" | "series" | "anime")."""
    p = (cfg.get("lang_profiles") or {}).get(kind) or _PROFILES.get(kind) or _PROFILES["movie"]
    return ([norm_lang(x) for x in (p.get("audio") or [])],
            [norm_lang(x) for x in (p.get("subs") or [])])


def kind_of(path, original_lang, cfg, series_type=None):
    """Which profile applies. Sonarr's own `seriesType` is authoritative when we have it;
    otherwise the library folder (`anime_dirs`) and then a Japanese original language, which is
    what an anime film looks like to Radarr — it has no anime flag of its own."""
    if (series_type or "").lower() == "anime":
        return "anime"
    mount = (cfg.get("media_mount") or "/media").rstrip("/")
    top = ""
    if path and path.startswith(mount + "/"):
        top = path[len(mount) + 1:].split(os.sep, 1)[0]
    if top and top in set(cfg.get("anime_dirs") or ["Anime"]):
        return "anime"
    if norm_lang(original_lang) == "jpn":
        return "anime"
    return "series" if series_type else "movie"


def gap_langs(auds, subs, kind, cfg):
    """(missing audio codes, missing subtitle codes) against the kind's target profile.

    `und` is not a language, so it never satisfies a target — see the module docstring. A
    subtitle shortfall on its own is only reported when `subs_only_gap` is on; otherwise subs
    ride along with an audio graft and a file that merely lacks subtitles doesn't trigger a
    whole download by itself."""
    want_a, want_s = profile(kind, cfg)
    miss_a = [c for c in want_a if c not in auds]
    miss_s = []
    if cfg.get("want_subs", True):
        miss_s = [c for c in want_s if c not in subs]
        if miss_s and not miss_a and not cfg.get("subs_only_gap", False):
            miss_s = []
    return miss_a, miss_s


def gap_kind(auds, subs, kind, cfg):
    """Coarse label for `gap_langs`: "audio", "subs", "audio+subs", or "" when nothing."""
    a, s = gap_langs(auds, subs, kind, cfg)
    return "+".join([x for x in (("audio" if a else ""), ("subs" if s else "")) if x])


# ------------------------------------------------------------------ path mapping
def to_media(path, cfg):
    """A Radarr/Sonarr path -> this container's /media path. The *arrs report their own view
    (`/data/Films/...` by default, see plex_media_prefix); we mount the same share at
    media_mount. Returns None when the result isn't on disk, so a mis-mapped path surfaces as
    an explicit scan error instead of silently becoming a wrong french_path."""
    if not path:
        return None
    mount = cfg.get("media_mount", "/media").rstrip("/")
    prefix = (cfg.get("plex_media_prefix") or "/data").rstrip("/")
    for cand in ([path] if path.startswith(mount + "/") else []) + \
                ([mount + path[len(prefix):]] if path.startswith(prefix + "/") else []):
        if os.path.exists(cand):
            return cand
    # last resort: the *arrs' folder leaf under each top-level library dir. Covers a prefix we
    # were never told about, without inventing a path that doesn't exist.
    tail = os.path.join(os.path.basename(os.path.dirname(path)), os.path.basename(path))
    try:
        for top in sorted(os.listdir(mount)):
            cand = os.path.join(mount, top, tail)
            if os.path.exists(cand):
                return cand
    except OSError:
        pass
    return None
