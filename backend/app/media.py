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
import json, os, re, subprocess, time
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


# A "signs & songs" track translates on-screen text and the opening/ending lyrics — nothing that
# is SPOKEN. It exists for viewers watching a dub, who need the billboards translated and nothing
# else. Tagged `eng` like any other subtitle, it is not an English subtitle in the sense the
# profile means: someone who doesn't understand the audio cannot watch the show with it. Blue Lock
# S01E03 ended up with exactly one English subtitle track, "English Signs", and read as complete.
_SIGNS_RX = re.compile(r'\b(signs?|songs?|s\s*[&+/]\s*s)\b', re.I)


def is_signs(name):
    return bool(_SIGNS_RX.search(name or ""))


def probe(path):
    """Full track inventory for one media file, or None if it can't be read.

    Returns {dur, fps, ok, auds: [...], subs: [...]}. Audio/subtitle entries carry the mkvmerge
    track `id` (what --audio-tracks/--subtitle-tracks take), the resolved `lang`, the codec,
    and the flags that decide whether a subtitle track is a usable full translation.

    `ok` is mkvmerge's own "I recognise and support this container". It matters because an
    unsupported container parses fine and yields an EMPTY track list — indistinguishable, without
    this flag, from a file that genuinely has no audio."""
    try:
        j = json.loads(subprocess.run(["mkvmerge", "-J", path], capture_output=True,
                                      text=True, timeout=180).stdout)
    except Exception:
        return None
    cont = j.get("container", {}) or {}
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
                         "signs": is_signs(name),
                         "sdh": bool(p.get("flag_hearing_impaired"))
                                or bool(re.search(r'\b(sdh|cc|hearing)\b', name, re.I)),
                         "default": bool(p.get("default_track"))})
    # Last resort for a file with exactly ONE untagged, unnamed audio track: the filename.
    if len(auds) == 1 and auds[0]["lang"] == "und":
        h = hint_lang(base, allow_fr=allow_fr)
        if h:
            auds[0]["lang"] = h
            auds[0]["from_filename"] = True
    dur = (cont.get("properties", {}) or {}).get("duration")
    return {"dur": (dur / 1e9 if isinstance(dur, (int, float)) else None),
            "fps": _fps(path), "auds": auds, "subs": subs,
            "ok": bool(cont.get("recognized")) and bool(cont.get("supported"))}


# ------------------------------------------------------------------ sidecar subtitles
# Bazarr (and most subtitle tooling) writes EXTERNAL subtitle files next to the media file
# rather than muxing them in: "Some Film (2020).en.srt". mkvmerge only reports what is inside
# the container, so without this every subtitle Bazarr ever fetched reads as still missing —
# the coverage number never improves, and with `subs_only_gap` on we would download a release
# for a subtitle that is already sitting on disk.
#
# The layout is /media/<library>/<title or show>/[Season NN/]<file>.mkv, with sidecars beside
# the file and sharing its stem, so "same directory, same stem" finds them.
_SUB_EXT = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup")
# tokens that qualify a subtitle rather than naming its language
_SUB_QUAL = {"forced", "sdh", "hi", "cc", "full", "foreign", "signs", "songs", "default"}
# ...and the subset of those that means "this doesn't subtitle the dialogue at all", so the file
# doesn't count as carrying that language (same rule as an embedded signs & songs track).
_SIGNS_QUAL = {"signs", "songs"}


_DIRCACHE = {}
_DIR_TTL = 30.0        # seconds
_DIR_MAX = 4000        # directories held before the cache is dropped wholesale


def _scandir(d):
    """One listing per directory, reused briefly. A 26-episode season folder would otherwise be
    re-listed once per episode.

    Short-lived on purpose: this is a process-global dict, and the webhook path probes files
    outside any scan pass, so an entry that lived forever would eventually answer for a
    directory whose subtitles have since changed."""
    now = time.time()
    hit = _DIRCACHE.get(d)
    if hit and now - hit[0] < _DIR_TTL:
        return hit[1]
    try:
        ent = [(e.name, e.stat()) for e in os.scandir(d) if e.is_file()]
    except OSError:
        ent = []
    if len(_DIRCACHE) > _DIR_MAX:
        _DIRCACHE.clear()
    _DIRCACHE[d] = (now, ent)
    return ent


def forget_dirs():
    """Drop the per-directory listing cache (a scan pass calls this at the start)."""
    _DIRCACHE.clear()


def sidecar_subs(path):
    """(languages, fingerprint) of external subtitle files belonging to `path`.

    The fingerprint is what makes the probe cache notice a subtitle appearing later: the .mkv's
    own size and mtime do not change when Bazarr drops an .srt beside it, so a progressive scan
    would otherwise never re-read the file.

    A sidecar with no recognisable language token stays `und` — excluded, exactly like an
    untagged embedded track. Guessing would be worse than reporting the gap."""
    d = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    langs_, fp = set(), []
    for name, st in _scandir(d):
        low = name.lower()
        if not low.startswith(stem + ".") or not low.endswith(_SUB_EXT):
            continue
        fp.append(f"{low}:{st.st_size}")
        # "<stem>.en.forced.srt" -> ["en", "forced"];  "<stem>.srt" -> []
        mid = low[len(stem) + 1:]
        mid = mid[:mid.rfind(".")] if "." in mid else ""       # drop the extension
        toks = mid.split(".")
        if any(t in _SIGNS_QUAL for t in toks):
            continue        # "<stem>.en.signs.ass" translates on-screen text, not the dialogue
        for tok in toks:
            # Only a KNOWN language counts. norm_lang falls back to the first three characters
            # of anything it doesn't recognise, which would turn "srt", "default" or a release
            # group's name into a language and mark the file as already subtitled.
            code = _ALIAS.get(tok) if tok and tok not in _SUB_QUAL else None
            if code and code != "und":
                langs_.add(code)
                break
    return langs_, ";".join(sorted(fp))


def mkv_error(r, limit=300):
    """Why a mkvmerge run failed.

    mkvmerge writes its diagnostics to **stdout**, not stderr — stderr is empty even on a hard
    failure (verified: a missing input file gives rc=2, an empty stderr, and
    "Error: The file '…' could not be opened for reading" on stdout). Reading `r.stderr` therefore
    recorded a bare "mkvmerge rc=2:" with the cause discarded on EVERY failure, which is how three
    unrelated films end up with the same blank error."""
    text = "\n".join(x for x in (r.stdout or "", r.stderr or "") if x.strip())
    errs = [ln.strip() for ln in text.splitlines()
            if ln.strip().startswith(("Error:", "Warning: The"))]
    msg = " ".join(errs) or " ".join(ln.strip() for ln in text.splitlines()[-3:] if ln.strip())
    return msg[-limit:] if msg else f"mkvmerge printed nothing (rc={r.returncode})"


def ffprobe_audio(path):
    """How many audio streams ffmpeg sees, or None if ffprobe couldn't read the file either.

    mkvmerge is the source of truth for what we can MUX, but it is not the source of truth for
    what the file CONTAINS: a codec it can't handle inside a container it can read comes back as
    an empty track list. That difference is the whole question when deciding whether a file is
    broken, so ask a second tool before saying it has no audio."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                            "-show_entries", "stream=index", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return len([x for x in r.stdout.splitlines() if x.strip()])


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
    untyped track proves nothing about which languages are present.

    A signs & songs track does not count as that language's subtitles either — it translates the
    on-screen text for someone who is listening to a dub, not the dialogue. Counting it is how a
    file whose only English subtitle says "English Signs" reads as meeting an eng subtitle target.
    A language that ALSO has a real track is of course still present; only signs-only is excluded."""
    if not info:
        return set(), set()
    real = {s["lang"] for s in info["subs"] if not s.get("signs")} - {"und"}
    return ({a["lang"] for a in info["auds"]} - {"und"}, real)


def sub_rank(s):
    """Sort key for picking WHICH subtitle track to graft when a release ships several.

    Signs & songs sorts LAST — below even a forced track. A forced track at least subtitles the
    dialogue it covers; a signs track subtitles no dialogue at all, so it is the worst possible
    answer to "this file is missing English subtitles". It used to sort ahead of forced, which is
    how a donor carrying both handed over the signs track.

    Then forced below full, SDH below that, and image subs (VobSub/PGS) below text."""
    image = 1 if re.search(r'(pgs|vobsub|dvd|hdmv)', str(s.get("codec") or ""), re.I) else 0
    signs = 1 if (s.get("signs") if "signs" in s else is_signs(s.get("name"))) else 0
    return (signs, 1 if s.get("forced") else 0, 1 if s.get("sdh") else 0, image, s["id"])


def has_lang(info, code):
    """True if the file definitely carries `code` as an audio language."""
    a, _ = langs(info)
    return norm_lang(code) in a


def wanted_subs(donor_info, base_info, want, limit=2):
    """Donor subtitle tracks worth adding: a wanted language the base file doesn't already have,
    best variant first, capped at `limit` (anime packs routinely carry 6+ English sub tracks).

    A language the donor covers ONLY with a signs & songs track is skipped entirely. Grafting it
    would neither close the gap (`langs` doesn't count signs) nor help anyone watch the episode —
    and since the gap stays open, the next donor would graft its signs track too, and the one
    after that, until the file carries five useless tracks and still isn't subtitled."""
    if not donor_info or not want:
        return []
    _, have = langs(base_info)
    out = []
    for code in want:
        if code in have:
            continue
        cands = sorted((s for s in donor_info["subs"] if s["lang"] == code), key=sub_rank)
        if not any(not (s.get("signs") if "signs" in s else is_signs(s.get("name")))
                   for s in cands):
            continue                      # signs-only for this language — take nothing
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


# ------------------------------------------------- what a RELEASE NAME advertises
# Scoring used to know exactly two things: "French dub" (reject) and "English/MULTI" (boost).
# That is the right rule for one library shape and blind for every other — a profile targeting
# German or Spanish got no signal at all, and a Spanish-dub release wasn't recognised as a dub.
# These markers are the scene tags a release uses to advertise the audio it carries, per
# language, so scoring can ask the only question that generalises: does this release appear to
# carry a language this file is still missing?
#
# Only full words and the unambiguous short tags are listed. Two-letter codes (NL, DE, IT…) are
# deliberately absent: they collide with resolution/source tokens and with ordinary title words,
# and a false positive here REJECTS a good release.
_DUB_MARKERS = {
    "fre": r'VFF|VFQ|VFI|VF2|VFNF|TRUEFRENCH|FRENCH|FRANCAIS|FRAN[CÇ]AIS|(?<![A-Z])VF(?![A-Z])',
    "eng": r'ENGLISH|(?<![A-Z])ENG(?![A-Z])',
    "ger": r'GERMAN|DEUTSCH|(?<![A-Z])GER(?![A-Z])',
    "spa": r'SPANISH|ESPANOL|ESPA[NÑ]OL|CASTELLANO|LATINO|(?<![A-Z])SPA(?![A-Z])',
    "ita": r'ITALIAN|ITALIANO|(?<![A-Z])ITA(?![A-Z])',
    "jpn": r'JAPANESE|JAPONAIS|(?<![A-Z])JPN(?![A-Z])',
    "por": r'PORTUGUESE|DUBLADO|(?<![A-Z])POR(?![A-Z])',
    "rus": r'RUSSIAN|(?<![A-Z])RUS(?![A-Z])',
    "kor": r'KOREAN|(?<![A-Z])KOR(?![A-Z])',
    "zho": r'CHINESE|MANDARIN|CANTONESE',
    "nld": r'DUTCH|NEDERLANDS',
    "pol": r'POLISH|LEKTOR|POLSKI',
    "tur": r'TURKISH|TURKCE',
    "swe": r'SWEDISH', "nor": r'NORWEGIAN', "dan": r'DANISH', "fin": r'FINNISH',
    "ces": r'CZECH', "hun": r'HUNGARIAN', "ell": r'GREEK', "ron": r'ROMANIAN',
    "ara": r'ARABIC', "heb": r'HEBREW', "hin": r'HINDI', "tha": r'THAI',
    "vie": r'VIETNAMESE', "ukr": r'UKRAINIAN', "cat": r'CATALAN',
}
_DUB_RX = {c: re.compile(rf'(?<![A-Za-z])(?:{p})(?![a-z])', re.I) for c, p in _DUB_MARKERS.items()}
# "carries several audio languages" — not a language of its own
_MULTI_RX = re.compile(r'(?<![A-Za-z])(MULTI|DUAL[\s._-]?AUDIO|DUAL)(?![A-Za-z])', re.I)
# "original audio, foreign subs" — says what the audio ISN'T (a dub), so never reject on it
_ORIG_RX = re.compile(r'(?<![A-Za-z])(VOSTFR|VOSTA|VOST|SUBFRENCH|SUBBED|VO)(?![A-Za-z])', re.I)


# Scene names are `Title . <year|SxxExx|resolution|source> . TAGS…`, so the tags live after the
# first of those. Scanning only that zone is what stops a film called "The German Doctor" from
# reading as a German dub — while still seeing the real GERMAN tag when the same title has one.
_TAGZONE_RX = re.compile(
    r'(?<![0-9])(19\d{2}|20\d{2})(?![0-9])|(?<![A-Za-z])[Ss]\d{1,3}([Ee]\d{1,4})?(?![0-9])'
    r'|(?<![A-Za-z0-9])(2160p|1080p|720p|480p)|(?<![A-Za-z])(blu-?ray|bdrip|brrip|web-?dl|webrip|hdtv|dvdrip|remux)',
    re.I)


def release_langs(title, ignore_title=""):
    """(advertised languages, multi, original) read off a RELEASE NAME.

    Only the tag zone — everything after the year / SxxExx / resolution / source — is inspected,
    because that is where a scene release states its audio. `ignore_title` is used only as a
    fallback for names with no such anchor, where its words are dropped before scanning."""
    t = title or ""
    m = _TAGZONE_RX.search(t)
    if m:
        t = t[m.start():]
    elif ignore_title:
        drop = _toks(ignore_title)
        if drop:
            t = " ".join(w for w in re.split(r'([^A-Za-z0-9]+)', t) if w.lower() not in drop)
    return ({c for c, rx in _DUB_RX.items() if rx.search(t)},
            bool(_MULTI_RX.search(t)), bool(_ORIG_RX.search(t)))


def _toks(s):
    return set(re.findall(r'[a-z0-9]+', (s or '').lower()))


def useless_release(title, need, media_title=""):
    """True when a release advertises dubs and NONE of them is a language this file still needs.

    This is the generalised form of the old French-dub-only reject: the library file already
    carries its own dub, so a release offering only that same language adds nothing and would
    burn a download slot. A release that advertises nothing (the common `Movie.2019.1080p.BluRay`
    shape) says nothing about its audio and is never rejected here; nor is MULTI, nor an
    original-audio/VOST release."""
    langs_, multi, orig = release_langs(title, media_title)
    if multi or orig or not langs_:
        return False
    return not (langs_ & {norm_lang(x) for x in (need or ())})


def lang_hits(title, need, media_title=""):
    """How many of the still-missing languages this release name advertises — direct evidence
    it is worth grabbing, and the language-agnostic replacement for the old English-only boost."""
    langs_, _, _ = release_langs(title, media_title)
    return len(langs_ & {norm_lang(x) for x in (need or ())})


# ------------------------------------------------------------------ audit (cached)
# How many files a pass actually READ vs served from cache. A progressive scan is nearly all
# cache hits, so without this it looks identical to a scan that did nothing — and the operator
# asking "did it pick up the files it missed?" has no way to tell.
STATS = {"probed": 0, "cached": 0}


def reset_stats():
    STATS.update(probed=0, cached=0)
    forget_dirs()          # a fresh pass must re-list directories, not reuse last pass's view


def audit(path, refresh=False):
    """(audio langs, subtitle langs, error) for a library file, cached until the file — or the
    external subtitles beside it — change. `error` is a string when the file couldn't be read at
    all; that must NOT be mistaken for "has no English", so callers skip it.

    The subtitle set is the union of the container's own tracks and any sidecar files
    (`Some Film (2020).en.srt`). Both play in Plex, so both satisfy the profile — and counting
    only embedded tracks would mean every subtitle Bazarr ever fetched still read as missing."""
    side, side_fp = sidecar_subs(path)
    if not refresh:
        row = core.get_probe(path, sidecars=side_fp)
        if row:
            STATS["cached"] += 1
            return (_split(row["auds"]), _split(row["subs"]), row["err"])
    STATS["probed"] += 1
    info = probe(path)
    if not info:
        core.put_probe(path, err="unreadable", sidecars=side_fp)
        return set(), set(), "unreadable"
    a, s = langs(info)
    s |= side                     # an external .srt satisfies the target exactly like a track
    if not info["auds"]:
        # Four very different situations used to collapse into one "no audio track" — including
        # a file mkvmerge simply can't parse, whose track list is empty for that reason alone.
        # Only the last is a broken FILE; the others are statements about our tools. Anything
        # destructive keys off "no audio track", so the distinction has to be made here.
        n = ffprobe_audio(path)
        if not info.get("ok"):                             # mkvmerge can't parse this container
            err = ("unreadable" if n is None else
                   f"unsupported container ({n} audio stream(s) per ffprobe)" if n else
                   "unsupported container")
        elif n is None:    err = "unreadable"              # ffprobe can't open it either
        elif n > 0:        err = f"audio mkvmerge can't read ({n} stream(s) per ffprobe)"
        else:              err = "no audio track"          # both tools agree: there is none
        core.put_probe(path, dur=info["dur"], fps=info["fps"], err=err, sidecars=side_fp)
        return set(), set(), err
    core.put_probe(path, dur=info["dur"], fps=info["fps"], auds=",".join(sorted(a)),
                   subs=",".join(sorted(s)), ntracks=len(info["auds"]), sidecars=side_fp)
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
