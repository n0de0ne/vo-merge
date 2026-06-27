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
        # Prowlarr wants repeated indexerIds params; requests handles list values.
        return self._get("/api/v1/search", query=query, type="search",
                         indexerIds=indexer_ids)

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

    def tags(self):
        return self._req("GET", "/api/v3/tag")

    def rescan(self, movie_id):
        return self._req("POST", "/api/v3/command",
                         json={"name": "RescanMovie", "movieId": movie_id})

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

    def tags(self):
        return self._req("GET", "/api/v3/tag")

    def episode_files(self, series_id):
        return self._req("GET", f"/api/v3/episodefile?seriesId={series_id}")

    def episodes(self, series_id):
        return self._req("GET", f"/api/v3/episode?seriesId={series_id}")

    def rescan(self, series_id):
        return self._req("POST", "/api/v3/command",
                         json={"name": "RescanSeries", "seriesId": series_id})

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
        """Targeted scan of `folder` in whichever movie section contains it, so Plex
        re-reads a changed file's streams (e.g. newly-added audio tracks)."""
        for d in self.sections():
            if d.get("type") != "movie":
                continue
            locs = [l.get("path") for l in d.get("Location", []) if l.get("path")]
            if any(folder.startswith(l) for l in locs):
                requests.get(f"{self.url}/library/sections/{d['key']}/refresh",
                             params={"path": folder, "X-Plex-Token": self.token}, timeout=15)
                return True
        return False

    def ping(self):
        r = requests.get(f"{self.url}/identity",
                         params={"X-Plex-Token": self.token}, timeout=15)
        r.raise_for_status(); return True
