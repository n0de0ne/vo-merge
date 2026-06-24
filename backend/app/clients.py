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


class QBittorrent:
    """qB WebUI API v2 with cookie-session auth (Radarr-style)."""
    def __init__(self, url, user, password):
        self.url = url.rstrip("/"); self.user = user; self.password = password
        self.s = requests.Session()

    def login(self):
        r = self.s.post(f"{self.url}/api/v2/auth/login",
                        data={"username": self.user, "password": self.password},
                        headers={"Referer": self.url}, timeout=30)
        if r.text.strip() != "Ok.":
            raise RuntimeError(f"qB login failed: {r.status_code} {r.text!r}")
        return True

    def version(self):
        return self.s.get(f"{self.url}/api/v2/app/version", timeout=30).text

    def add(self, urls, category, savepath):
        r = self.s.post(f"{self.url}/api/v2/torrents/add",
                        data={"urls": urls, "category": category,
                              "savepath": savepath, "autoTMM": "false"},
                        headers={"Referer": self.url}, timeout=60)
        r.raise_for_status()
        return r.text  # "Ok." on success

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

    def ping(self):
        r = requests.get(f"{self.url}/identity",
                         params={"X-Plex-Token": self.token}, timeout=15)
        r.raise_for_status(); return True
