"""The gap decision, end to end, without touching a disk or an indexer.

`media.py` decides which languages a file is missing, and every download vo-merge ever starts
follows from that answer. These are all pure functions over strings and dicts, so the whole
decision is testable with no fixtures — which is why the suite starts here.

Each test names the real behaviour it pins down; several of them encode bugs that shipped.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import media                                                    # noqa: E402


# --------------------------------------------------------------- language normalisation
@pytest.mark.parametrize("raw,want", [
    ("fre", "fre"), ("fra", "fre"), ("fr", "fre"), ("French", "fre"), ("français", "fre"),
    ("eng", "eng"), ("en", "eng"), ("English", "eng"),
    ("jpn", "jpn"), ("ja", "jpn"), ("Japanese", "jpn"),
    ("pt-BR", "por"), ("eng (commentary)", "eng"),
    ("", "und"), ("und", "und"), ("zxx", "und"), (None, "und"),
])
def test_norm_lang_collapses_every_spelling(raw, want):
    """639-2/B, 639-2/T, 639-1 and plain names all have to land on one code, or a set
    comparison can be defeated by spelling."""
    assert media.norm_lang(raw) == want


def test_norm_lang_keeps_unknown_codes_distinguishable():
    """An unrecognised token falls back to its first three characters rather than becoming
    `und` — but it must never collide with a real language."""
    assert media.norm_lang("klingon") == "kli"


# --------------------------------------------------------------- signs & songs
@pytest.mark.parametrize("name,want", [
    ("Signs", True), ("English Signs", True), ("Signs & Songs", True), ("S&S", True),
    ("Songs", True),
    ("Designs", False),          # substring, not a word
    ("English SDH", False), ("Full", False), ("Forced", False), ("", False), (None, False),
])
def test_is_signs(name, want):
    """A signs track translates on-screen text, not dialogue. Matching it by substring would
    catch 'Designs'; missing it lets 'English Signs' satisfy an English subtitle target."""
    assert media.is_signs(name) is want


def test_signs_only_subtitle_does_not_count_as_that_language():
    """Blue Lock S01E03: one English subtitle track called 'English Signs', and the episode read
    as complete. Someone who can't follow the audio cannot watch it with that track."""
    info = {"auds": [{"lang": "fre"}],
            "subs": [{"lang": "eng", "signs": True, "id": 0}]}
    audio, subs = media.langs(info)
    assert audio == {"fre"}
    assert subs == set(), "a signs-only track must not fill the eng subtitle slot"


def test_language_with_a_real_track_is_present_even_if_signs_also_exists():
    info = {"auds": [{"lang": "fre"}],
            "subs": [{"lang": "eng", "signs": True, "id": 0},
                     {"lang": "eng", "signs": False, "id": 1}]}
    assert media.langs(info)[1] == {"eng"}


def test_sub_rank_puts_signs_last_below_forced():
    """Order is full, SDH, forced, signs.

    Signs last is the point: a forced track at least subtitles the dialogue it covers, a signs
    track covers none of it, and signs used to sort AHEAD of forced — which is how a donor
    carrying both handed over the signs track. SDH above forced is also deliberate: it carries
    every line of dialogue, where forced covers only the marked passages."""
    full = {"lang": "eng", "id": 0, "forced": False, "sdh": False, "signs": False, "codec": "SubRip"}
    forced = {"lang": "eng", "id": 1, "forced": True, "sdh": False, "signs": False, "codec": "SubRip"}
    sdh = {"lang": "eng", "id": 2, "forced": False, "sdh": True, "signs": False, "codec": "SubRip"}
    signs = {"lang": "eng", "id": 3, "forced": False, "sdh": False, "signs": True, "codec": "SubRip"}
    ranked = [s["id"] for s in sorted([signs, sdh, forced, full], key=media.sub_rank)]
    assert ranked == [0, 2, 1, 3]
    assert ranked[-1] == 3, "signs must never be picked ahead of a real subtitle"


def test_sub_rank_prefers_text_over_image():
    text = {"lang": "eng", "id": 5, "forced": False, "sdh": False, "signs": False, "codec": "SubRip"}
    pgs = {"lang": "eng", "id": 1, "forced": False, "sdh": False, "signs": False, "codec": "HDMV PGS"}
    assert sorted([pgs, text], key=media.sub_rank)[0]["id"] == 5


def test_wanted_subs_takes_nothing_when_the_donor_only_has_signs():
    """Grafting it would neither close the gap nor help anyone — and because the gap stays open,
    the next donor grafts its signs track too, until the file carries five useless tracks."""
    donor = {"subs": [{"lang": "eng", "id": 0, "forced": False, "sdh": False, "signs": True,
                       "codec": "SubRip"}]}
    base = {"auds": [{"lang": "fre"}], "subs": []}
    assert media.wanted_subs(donor, base, ["eng"]) == []


def test_wanted_subs_takes_the_best_real_track():
    donor = {"subs": [
        {"lang": "eng", "id": 0, "forced": False, "sdh": False, "signs": True, "codec": "SubRip"},
        {"lang": "eng", "id": 1, "forced": False, "sdh": False, "signs": False, "codec": "SubRip"},
    ]}
    base = {"auds": [{"lang": "fre"}], "subs": []}
    assert [s["id"] for s in media.wanted_subs(donor, base, ["eng"], limit=1)] == [1]


# --------------------------------------------------------------- release names
def test_release_langs_only_reads_the_tag_zone():
    """'The German Doctor' is a title, not a German dub. Scanning the whole name rejects a
    perfectly good release; scanning only after the year/resolution/source token does not."""
    langs, _, _ = media.release_langs("The German Doctor 2013 1080p BluRay x264")
    assert "ger" not in langs

    langs, _, _ = media.release_langs("The German Doctor 2013 GERMAN 1080p BluRay x264")
    assert "ger" in langs


def test_release_langs_reads_multi_and_original_markers():
    assert media.release_langs("Film.2019.MULTI.1080p.BluRay")[1] is True
    assert media.release_langs("Film.2019.VOSTFR.1080p.WEB-DL")[2] is True


@pytest.mark.parametrize("title", [
    "Film.2019.VFF.1080p.BluRay",
    "Film.2019.TRUEFRENCH.1080p.BluRay",
    "Film.2019.FRENCH.1080p.BluRay",
])
def test_french_dub_markers(title):
    assert "fre" in media.release_langs(title)[0]


def test_useless_release_rejects_only_a_dub_we_already_have():
    """The library file already IS the French dub, so a French-only release adds nothing and
    would burn a grab slot."""
    assert media.useless_release("Film.2019.TRUEFRENCH.1080p.BluRay", {"eng"}) is True
    assert media.useless_release("Film.2019.TRUEFRENCH.1080p.BluRay", {"fre"}) is False


def test_useless_release_never_rejects_an_unmarked_release():
    """`Movie.2019.1080p.BluRay` is the common shape and says nothing about its audio. Rejecting
    it on no evidence throws away most of the catalogue."""
    assert media.useless_release("Film.2019.1080p.BluRay.x264", {"eng"}) is False


def test_useless_release_never_rejects_multi_or_vost():
    assert media.useless_release("Film.2019.MULTI.1080p.BluRay", {"eng"}) is False
    assert media.useless_release("Film.2019.VOSTFR.1080p.BluRay", {"eng"}) is False


def test_lang_hits_counts_only_languages_we_still_need():
    assert media.lang_hits("Film.2019.ENGLISH.1080p", {"eng"}) == 1
    assert media.lang_hits("Film.2019.ENGLISH.1080p", {"fre"}) == 0


def test_two_letter_codes_are_not_treated_as_languages():
    """NL/DE/IT collide with source and resolution tokens, and a false positive here REJECTS a
    good release."""
    assert media.release_langs("Film.2019.1080p.NL.BluRay")[0] == set()


# --------------------------------------------------------------- profiles & the gap
def test_orig_resolves_per_title_not_to_literal_jpn():
    """Arcane is filed as anime and made in French. A literal `jpn` target is a gap no release on
    earth can fill — 18 episodes searched forever and ended up ignored while already complete."""
    cfg = {"lang_profiles": {"anime": {"audio": ["fre", "eng", "orig"], "subs": ["fre", "eng"]}}}
    assert media.profile("anime", cfg, orig="Japanese")[0] == ["fre", "eng", "jpn"]
    assert media.profile("anime", cfg, orig="French")[0] == ["fre", "eng"]
    assert media.profile("anime", cfg, orig="Korean")[0] == ["fre", "eng", "kor"]


def test_unknown_original_language_drops_the_slot_rather_than_inventing_one():
    """Radarr reports "?" when it doesn't know, and norm_lang's 3-char fallback would happily
    make "?" itself a target."""
    cfg = {"lang_profiles": {"anime": {"audio": ["fre", "eng", "orig"], "subs": []}}}
    assert media.profile("anime", cfg, orig="?")[0] == ["fre", "eng"]


def test_gap_is_target_minus_present():
    cfg = {"lang_profiles": {"movie": {"audio": ["fre", "eng"], "subs": ["fre", "eng"]}},
           "want_subs": True, "subs_only_gap": True}
    miss_a, miss_s = media.gap_langs({"fre"}, {"fre"}, "movie", cfg)
    assert miss_a == ["eng"] and miss_s == ["eng"]

    miss_a, miss_s = media.gap_langs({"fre", "eng"}, {"fre", "eng"}, "movie", cfg)
    assert miss_a == [] and miss_s == []


def test_subs_only_gap_off_suppresses_a_subtitle_only_shortfall():
    cfg = {"lang_profiles": {"movie": {"audio": ["fre", "eng"], "subs": ["fre", "eng"]}},
           "want_subs": True, "subs_only_gap": False}
    assert media.gap_langs({"fre", "eng"}, {"fre"}, "movie", cfg) == ([], [])
    # ...but a subtitle shortfall still rides along with a real audio gap
    assert media.gap_langs({"fre"}, {"fre"}, "movie", cfg) == (["eng"], ["eng"])


def test_und_never_satisfies_a_target():
    """An untagged track proves nothing about which language is present. Erring this way adds a
    real English track; the opposite error leaves a French-only file forever."""
    cfg = {"lang_profiles": {"movie": {"audio": ["fre", "eng"], "subs": []}}, "want_subs": False}
    assert "eng" in media.gap_langs({"fre", "und"}, set(), "movie", cfg)[0]


def test_anime_with_its_vo_still_needs_english():
    """The case the app exists for: an earlier rule counted 'English OR the original language' as
    filled, which skipped exactly these."""
    cfg = {"lang_profiles": {"anime": {"audio": ["fre", "eng", "orig"], "subs": ["fre", "eng"]}},
           "want_subs": True, "subs_only_gap": True}
    miss_a, _ = media.gap_langs({"fre", "jpn"}, {"fre"}, "anime", cfg, orig="Japanese")
    assert miss_a == ["eng"]


# --------------------------------------------------------------- track selection
def test_wanted_audio_takes_one_track_per_missing_language():
    """A 3-dub donor must not triple the file size, and a Spanish track nobody asked for must
    not be grafted."""
    donor = {"auds": [{"lang": "eng", "id": 1, "ch": 2}, {"lang": "eng", "id": 2, "ch": 6},
                      {"lang": "spa", "id": 3, "ch": 6}]}
    base = {"auds": [{"lang": "fre"}], "subs": []}
    picked = media.wanted_audio(donor, base, ["fre", "eng"])
    assert [a["lang"] for a in picked] == ["eng"]
    assert picked[0]["id"] == 2, "the best-channel variant wins"


def test_wanted_audio_skips_languages_the_base_already_has():
    donor = {"auds": [{"lang": "fre", "id": 1, "ch": 6}, {"lang": "eng", "id": 2, "ch": 6}]}
    base = {"auds": [{"lang": "fre"}], "subs": []}
    assert [a["lang"] for a in media.wanted_audio(donor, base, ["fre", "eng"])] == ["eng"]


def test_wanted_audio_extra_keeps_the_vo_eligible():
    """The VO fallback: no English exists, so the original-language track is grafted instead
    (Norwegian for 'Kraken'). TV silently lacked this — it never passed `extra`."""
    donor = {"auds": [{"lang": "nor", "id": 1, "ch": 6}]}
    base = {"auds": [{"lang": "fre"}], "subs": []}
    assert media.wanted_audio(donor, base, ["fre", "eng"]) == []
    assert [a["lang"] for a in
            media.wanted_audio(donor, base, ["fre", "eng"], extra={"nor"})] == ["nor"]


# --------------------------------------------------------------- kind selection
def test_kind_of_prefers_sonarrs_own_anime_flag():
    cfg = {"media_mount": "/media", "anime_dirs": ["Anime"]}
    assert media.kind_of("/media/Series/Show/f.mkv", "English", cfg, series_type="anime") == "anime"


def test_kind_of_falls_back_to_the_library_folder_then_the_original_language():
    cfg = {"media_mount": "/media", "anime_dirs": ["Anime"]}
    assert media.kind_of("/media/Anime/Show/f.mkv", "English", cfg, series_type="standard") == "anime"
    # an anime FILM looks like this to Radarr, which has no anime flag
    assert media.kind_of("/media/Films/Film/f.mkv", "Japanese", cfg) == "anime"
    assert media.kind_of("/media/Films/Film/f.mkv", "English", cfg) == "movie"
    assert media.kind_of("/media/Series/Show/f.mkv", "English", cfg, series_type="standard") == "series"


# --------------------------------------------------------------- filename hints
def test_vostfr_suppresses_the_french_hint():
    """VOSTFR describes the SUBTITLES, so the audio is the original language. Reading those
    tokens as audio is how you conclude a Japanese file is French."""
    assert media.hint_lang("Film.VOSTFR.1080p", allow_fr=False) is None
    assert media.hint_lang("Film.VFF.1080p", allow_fr=True) == "fre"


def test_hint_lang_needs_whole_tokens():
    assert media.hint_lang("Buffering") is None       # not 'fr' inside a word


# ------------------------------------------------------------------ VOF (French original)
def test_vof_is_a_french_audio_marker():
    """VOF = "Version Originale Française": a French-ORIGINAL title, French audio only. It
    matched nothing — the bare `VF` marker refuses a letter before it (the O) and the VOST-family
    `VO` refuses one after (the F) — so a VOF release advertised no language, was never rejected,
    and got grabbed for files missing ENGLISH, which it can never carry."""
    from app import media
    t = "OSS.117.Alerte.rouge.en.Afrique.noire.2021.VOF.1080p.BluRay.AAC.x265-k7"
    langs, multi, orig = media.release_langs(t)
    assert langs == {"fre"} and not multi and not orig
    # a French film missing ENGLISH: VOF can never help -> rejected
    assert media.useless_release(t, {"eng"}, "OSS 117 Alerte rouge en Afrique noire") is True
    # ...but it IS the answer when French audio is what's missing
    assert media.useless_release(t, {"fre"}, "OSS 117") is False
    assert media.lang_hits(t, {"fre"}) == 1
    # neighbours must not regress
    assert media.release_langs("Movie.2019.VF.1080p")[0] == {"fre"}
    assert media.release_langs("Movie.2019.VOSTFR.1080p")[0] == set()
    assert media.release_langs("Movie.2019.VOSTFR.1080p")[2] is True
    assert media.release_langs("Movie.2019.MULTI.VOF.1080p")[1] is True    # MULTI still wins
    # and a VOF filename resolves an untagged (und) audio track to French
    assert media.hint_lang("OSS.117.2021.VOF.1080p.mkv") == "fre"
