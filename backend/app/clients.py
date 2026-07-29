"""Thin HTTP clients for Prowlarr, Radarr, qBittorrent, Plex."""
import requests, urllib.parse


class Prowlarr:
    def __init__(self, url, key):
        self.url = url.rstrip("/"); self.key = key

    def _get(self, path, **params):
        r = requests.get(f"{self.url}{path}", params=params,
                         headers={"X-Api-Key": self.key}, timeout=60)
        r.raise_for_status(); return r.json()

    def search(self, query, indexer_ids):
        # Prowlarr wants repeated indexerIds params; requests handles list values. Omitting the
        # parameter entirely searches EVERY configured indexer, which is what an empty list has
        # to mean: indexer IDs are per-instance numbers, so a fresh install has none configured
        # and "search nothing" would make every title look like it has no releases.
        params = {"query": query, "type": "search"}
        if indexer_ids:
            params["indexerIds"] = list(indexer_ids)
        return self._get("/api/v1/search", **params)

    def ping(self):
        return self._get("/api/v1/system/status")


class Radarr:
    def __init__(self, url, key):
        self.url = url.rstrip("/"); self.key = key

    def _req(self, method, path, **kw):
        r = requests.request(method, f"{self.url}{path}",
                             headers={"X-Api-Key": self.key}, timeout=60, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    def movies(self):
        return self._req("GET", "/api/v3/movie")

    def movie(self, movie_id):
        """One movie by Radarr id — what a webhook needs, instead of pulling the whole library."""
        return self._req("GET", f"/api/v3/movie/{movie_id}")

    def tags(self):
        return self._req("GET", "/api/v3/tag")

    def rescan(self, movie_id):
        return self._req("POST", "/api/v3/command",
                         json={"name": "RescanMovie", "movieId": movie_id})

    def delete_movie_file(self, file_id):
        """Delete the file from disk AND from Radarr's DB. Unlinking it ourselves would leave
        Radarr believing the movie is still present, so it would never search for a replacement —
        which is the whole point of removing a broken file."""
        return self._req("DELETE", f"/api/v3/moviefile/{file_id}")

    def search(self, movie_ids):
        return self._req("POST", "/api/v3/command",
                         json={"name": "MoviesSearch", "movieIds": list(movie_ids)})

    def ping(self):
        return self._req("GET", "/api/v3/system/status")


class Sonarr:
    def __init__(self, url, key):
        self.url = url.rstrip("/"); self.key = key

    def _req(self, method, path, **kw):
        r = requests.request(method, f"{self.url}{path}",
                             headers={"X-Api-Key": self.key}, timeout=60, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    def series(self):
        return self._req("GET", "/api/v3/series")

    def series_one(self, series_id):
        """One series by Sonarr id — what a webhook needs, instead of pulling the whole library."""
        return self._req("GET", f"/api/v3/series/{series_id}")

    def tags(self):
        return self._req("GET", "/api/v3/tag")

    def episode_files(self, series_id):
        return self._req("GET", f"/api/v3/episodefile?seriesId={series_id}")

    def episodes(self, series_id):
        return self._req("GET", f"/api/v3/episode?seriesId={series_id}")

    def rescan(self, series_id):
        return self._req("POST", "/api/v3/command",
                         json={"name": "RescanSeries", "seriesId": series_id})

    def delete_episode_file(self, file_id):
        """Delete from disk AND from Sonarr's DB — see Radarr.delete_movie_file."""
        return self._req("DELETE", f"/api/v3/episodefile/{file_id}")

    def search(self, episode_ids):
        return self._req("POST", "/api/v3/command",
                         json={"name": "EpisodeSearch", "episodeIds": list(episode_ids)})

    def ping(self):
        return self._req("GET", "/api/v3/system/status")


class QBittorrent:
    """qB WebUI API v2 with cookie-session auth (Radarr-style)."""
    def __init__(self, url, user, password):
        self.url = url.rstrip("/"); self.user = user; self.password = password
        self.s = requests.Session()

    def login(self):
        r = self.s.post(f"{self.url}/api/v2/auth/login",
                        data={"username": self.user, "password": self.password},
                        headers={"Referer": self.url}, timeout=30)
        # qB returns 200 "Ok." OR (qB 5.x / some proxies) 204 with an empty body on
        # success, setting a QBT_SID cookie. "Fails." or 403 = bad credentials.
        if r.text.strip() == "Fails." or r.status_code == 403:
            raise RuntimeError("qB login failed: invalid credentials")
        if r.status_code not in (200, 204):
            raise RuntimeError(f"qB login failed: {r.status_code} {r.text!r}")
        if r.text.strip() != "Ok." and not any("SID" in k for k in self.s.cookies.keys()):
            raise RuntimeError(f"qB login: no session cookie ({r.status_code})")
        return True

    def version(self):
        return self.s.get(f"{self.url}/api/v2/app/version", timeout=30).text

    def create_category(self, name, savepath):
        try:
            self.s.post(f"{self.url}/api/v2/torrents/createCategory",
                        data={"category": name, "savePath": savepath},
                        headers={"Referer": self.url}, timeout=30)
        except Exception:
            pass  # already exists / non-fatal

    def add(self, urls=None, category=None, savepath=None, torrent_file=None):
        """Add by magnet/URL (urls=) OR by uploading .torrent bytes (torrent_file=).
        qB's /add never returns the hash — callers confirm it via torrents()/hashes()."""
        data = {"autoTMM": "false"}
        if category: data["category"] = category
        if savepath: data["savepath"] = savepath
        if urls:     data["urls"] = urls
        files = {"torrents": ("vo.torrent", torrent_file, "application/x-bittorrent")} if torrent_file else None
        r = self.s.post(f"{self.url}/api/v2/torrents/add", data=data, files=files,
                        headers={"Referer": self.url}, timeout=60)
        if r.status_code == 409:
            return {"duplicate": True}   # already in qB — fine
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}    # qB returns "Ok." / "Fails."

    def hashes(self, category=None):
        return {t["hash"].lower() for t in self.torrents(category) if t.get("hash")}

    def delete(self, hashes, delete_files=True):
        if not hashes:
            return
        self.s.post(f"{self.url}/api/v2/torrents/delete",
                    data={"hashes": "|".join(hashes), "deleteFiles": str(delete_files).lower()},
                    headers={"Referer": self.url}, timeout=30)

    def torrents(self, category=None):
        p = {"category": category} if category else {}
        return self.s.get(f"{self.url}/api/v2/torrents/info", params=p, timeout=30).json()

    def torrent(self, torrent_hash):
        """One torrent's current info dict, or None. qB answers `hashes=` on the same endpoint,
        so this costs one small request instead of listing a whole category — and it finds the
        torrent whatever category it is in, which matters when re-resolving a donor whose path
        moved."""
        if not torrent_hash:
            return None
        r = self.s.get(f"{self.url}/api/v2/torrents/info",
                       params={"hashes": str(torrent_hash).lower()}, timeout=30)
        r.raise_for_status()
        got = r.json() or []
        return got[0] if got else None

    def stop(self, hashes):
        """Stop (pause) torrents. NEVER cap share limits instead — qB's limit-reached
        action can be 'remove torrent + delete content', destroying an unmerged donor."""
        if not hashes:
            return
        r = self.s.post(f"{self.url}/api/v2/torrents/stop",
                        data={"hashes": "|".join(hashes)},
                        headers={"Referer": self.url}, timeout=30)
        r.raise_for_status()

    def trackers(self, h):
        """Announce URLs registered on a torrent (skips the DHT/PeX/LSD pseudo-entries)."""
        try:
            r = self.s.get(f"{self.url}/api/v2/torrents/trackers", params={"hash": h}, timeout=30)
            return [t.get("url", "") for t in r.json()
                    if t.get("url", "").startswith(("http", "udp"))]
        except Exception:
            return []

    def files(self, torrent_hash):
        return self.s.get(f"{self.url}/api/v2/torrents/files",
                          params={"hash": torrent_hash}, timeout=30).json()

    def ping(self):
        self.login(); return self.version()


class Plex:
    def __init__(self, url, token):
        self.url = url.rstrip("/"); self.token = token

    def analyze(self, rating_key):
        requests.put(f"{self.url}/library/metadata/{rating_key}/analyze",
                     params={"X-Plex-Token": self.token}, timeout=30)

    def refresh_section(self, section_id):
        requests.get(f"{self.url}/library/sections/{section_id}/refresh",
                     params={"X-Plex-Token": self.token}, timeout=30)

    def sections(self):
        r = requests.get(f"{self.url}/library/sections",
                         params={"X-Plex-Token": self.token},
                         headers={"Accept": "application/json"}, timeout=15)
        return r.json().get("MediaContainer", {}).get("Directory", [])

    def scan_path(self, folder):
        """Targeted scan of `folder` in whichever section (movie OR show, incl. the -EN
        libraries) contains it, so Plex re-reads changed files / picks up new symlinks."""
        done = False
        for d in self.sections():
            locs = [l.get("path") for l in d.get("Location", []) if l.get("path")]
            if any(folder == l or folder.startswith(l.rstrip("/") + "/") for l in locs):
                requests.get(f"{self.url}/library/sections/{d['key']}/refresh",
                             params={"path": folder, "X-Plex-Token": self.token}, timeout=15)
                done = True   # a folder can live in >1 section (original + -EN) -> refresh all
        return done

    def _search(self, query, want_type):
        r = requests.get(f"{self.url}/search", params={"query": query, "X-Plex-Token": self.token},
                         headers={"Accept": "application/json"}, timeout=15)
        return [m for m in r.json().get("MediaContainer", {}).get("Metadata", [])
                if m.get("type") == want_type]

    def _children(self, rating_key):
        r = requests.get(f"{self.url}/library/metadata/{rating_key}/children",
                         params={"X-Plex-Token": self.token},
                         headers={"Accept": "application/json"}, timeout=15)
        return r.json().get("MediaContainer", {}).get("Metadata", [])

    def rating_keys(self, title, year=None, season=None, episode=None):
        """EVERY ratingKey for this item, across ALL sections. A title mirrored into a -EN
        library exists TWICE — once in Films/Series/Anime and once in Films-EN/Series-EN/
        Anime-EN — as two separate items with two keys. Returning only the first (as this used
        to) analysed whichever Plex happened to list first and left the other stale, so a
        grafted track showed up in one library and not the other."""
        keys = []
        if season is None:                                   # movie
            for m in self._search(title, "movie"):
                if not year or abs(int(m.get("year") or 0) - int(year)) <= 1:
                    if m.get("ratingKey"):
                        keys.append(m["ratingKey"])
            return keys
        for show in self._search(title, "show"):             # episode: show -> season -> episode
            try:
                seasons = self._children(show.get("ratingKey"))
                sk = next((s.get("ratingKey") for s in seasons
                           if str(s.get("index")) == str(season)), None)
                if not sk:
                    continue
                rk = next((e.get("ratingKey") for e in self._children(sk)
                           if str(e.get("index")) == str(episode)), None)
                if rk:
                    keys.append(rk)
            except Exception:
                continue                                     # one bad section mustn't hide the rest
        return keys

    def refresh_analyze(self, folders, title, year=None, season=None, episode=None):
        """Partial-scan each folder (registers new files/symlinks) AND `analyze` every matching
        item. A plain scan does NOT re-read a file's audio/subtitle streams when it is replaced
        IN PLACE under the same name — only analyze does. Pass both the library folder and its
        -EN mirror so each PMS re-reads both. Returns the ratingKeys analysed."""
        for folder in ([folders] if isinstance(folders, str) else folders):
            if not folder:
                continue
            try:
                self.scan_path(folder)
            except Exception:
                pass
        keys = self.rating_keys(title, year, season, episode)
        for rk in keys:
            try:
                self.analyze(rk)
            except Exception:
                pass
        return keys

    def ping(self):
        r = requests.get(f"{self.url}/identity",
                         params={"X-Plex-Token": self.token}, timeout=15)
        r.raise_for_status(); return True
