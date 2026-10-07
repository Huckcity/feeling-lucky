"""The station engine.

A roll starts from a random artist you already know, never played itself. The
suggestions come from Last.fm's similar artists (what people actually listen to
together), or Deezer's related-artists graph when Last.fm has nothing, rather than
YouTube's radio. Everyone you have subscribed to, saved, liked, played or scrobbled
is dropped and the less famous are favoured. A couple of top tracks from each are
looked up on YouTube Music, which is only the player.

The station is handed to YouTube Music as a temporary queue, so nothing is saved
unless you press Save there.
"""

import json
import os
import random
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from ytmusicapi import OAuthCredentials, YTMusic

CACHE = Path.home() / ".cache" / "feeling-lucky" / "library.json"
CACHE_TTL = 24 * 3600
LIBRARY_LIMIT = 10_000
EDITS = re.compile(r"\b(slowed|sped[ -]up|speed[ -]up|nightcore|8d audio)\b", re.I)  # re-edits, not songs
DEEZER = "https://api.deezer.com/"
LASTFM = "https://ws.audioscrobbler.com/2.0/"
STATION_SIZE = 48  # YouTube's temporary queues hold about 50 tracks, roughly three hours
HOPS = 6  # Deezer: of the seed's related artists, how many have their own related artists join the pool
ARTISTS = 32  # artists drawn per station, up to two tracks each (keeps a roll under Deezer's 50 calls / 5 s)
MAX_FANS = 250_000  # Deezer fans; past this an artist is probably one you've heard of
MAX_LISTENERS = 500_000  # Last.fm listeners of an artist's top track; the same line, on Last.fm's scale
SEED_PLAYS = 3  # scrobbles before an artist can seed a station, so one-off listens don't
SEARCHES = 8  # parallel YouTube Music lookups


def account_client() -> YTMusic | None:
    """Your account, from the YTMUSIC_AUTH secret (browser headers or an OAuth token)."""
    auth = os.environ.get("YTMUSIC_AUTH")
    if not auth:
        return None
    client_id, client_secret = os.environ.get("YTMUSIC_CLIENT_ID"), os.environ.get("YTMUSIC_CLIENT_SECRET")
    credentials = OAuthCredentials(client_id, client_secret) if client_id and client_secret else None
    return YTMusic(auth, oauth_credentials=credentials)


def channel_id(browse_id: str | None) -> str | None:
    # library listings prefix an artist's channel id with MPLA
    return browse_id[4:] if browse_id and browse_id.startswith("MPLA") else browse_id


def key(name: str) -> str:
    """A name for matching across services: case, accents, spacing and punctuation dropped."""
    return "".join(c for c in unicodedata.normalize("NFKD", name.casefold()) if c.isalnum())


def deezer(path: str, **params) -> list[dict]:
    data = requests.get(DEEZER + path, params=params, timeout=15).json()
    if "error" in data:
        raise RuntimeError(f"Deezer said: {data['error'].get('message')}. Wait a few seconds and roll again.")
    return data["data"]


def lastfm_user() -> str | None:
    return os.environ.get("LASTFM_USER") if os.environ.get("LASTFM_API_KEY") else None


def lastfm(method: str, **params) -> dict:
    query = {"method": method, "api_key": os.environ["LASTFM_API_KEY"], "format": "json", **params}
    data = requests.get(LASTFM, params=query, timeout=15).json()
    if "error" in data:
        raise LookupError(f"Last.fm said: {data.get('message')}")
    return data


def fetch_lastfm(user: str) -> dict:
    """Everyone you've scrobbled, and the ones you've played enough to seed from."""
    artists, page, pages = [], 1, 1
    while page <= min(pages, 10):
        top = lastfm("user.getTopArtists", user=user, period="overall", limit=1000, page=page)["topartists"]
        artists += top["artist"]
        pages, page = int(top["@attr"]["totalPages"]), page + 1
    return {
        "artist_names": [a["name"] for a in artists],
        "seeds": [a["name"] for a in artists if int(a["playcount"]) >= SEED_PLAYS],
    }


def queue_url(video_ids: list[str]) -> str:
    """A YouTube Music link that plays these tracks as an unsaved queue (YouTube's temporary-list link)."""
    response = requests.get(
        "https://www.youtube.com/watch_videos",
        params={"video_ids": ",".join(video_ids)},
        cookies={"SOCS": "CAI"},  # skips the cookie-consent interstitial
        allow_redirects=False,
        timeout=15,
    )
    list_id = parse_qs(urlparse(response.headers.get("location", "")).query).get("list", [None])[0]
    if not list_id:
        raise RuntimeError(f"YouTube wouldn't make a temporary queue (HTTP {response.status_code}).")
    return f"https://music.youtube.com/watch?v={video_ids[0]}&list={list_id}"


def fetch_youtube(yt: YTMusic) -> dict | None:
    """Your YouTube Music library, or None when the login has expired: then it reads as empty, not as an error."""
    ids: set[str] = set()
    names: set[str] = set()
    song_ids: set[str] = set()

    subscriptions = yt.get_library_subscriptions(limit=LIBRARY_LIMIT)
    library_songs = yt.get_library_songs(limit=LIBRARY_LIMIT)
    if not subscriptions and not library_songs:
        return None
    for artist in subscriptions + yt.get_library_artists(limit=LIBRARY_LIMIT):
        if artist.get("browseId"):
            ids.add(channel_id(artist["browseId"]))
        names.add(key(artist["artist"]))

    saved = library_songs + yt.get_liked_songs(limit=LIBRARY_LIMIT)["tracks"]
    try:
        history = yt.get_history()
    except Exception:  # history can be paused or empty
        history = []
    for track in saved + history:
        for artist in track.get("artists") or []:
            if artist.get("id"):
                ids.add(channel_id(artist["id"]))
            if artist.get("name"):
                names.add(key(artist["name"]))
        if track.get("videoId"):
            song_ids.add(track["videoId"])

    songs = {
        t["videoId"]: {"videoId": t["videoId"], "title": t["title"], "artist": t["artists"][0]["name"]}
        for t in saved
        if t.get("videoId") and t.get("artists")
    }
    return {
        "artist_ids": sorted(ids),
        "artist_names": sorted(names),
        "song_ids": sorted(song_ids),
        "songs": list(songs.values()),
        "subscriptions": [
            {"id": channel_id(a["browseId"]), "name": a["artist"]} for a in subscriptions if a.get("browseId")
        ],
    }


class Library:
    """What you already know, from YouTube Music and Last.fm: artists to keep out, and artists to start from."""

    def __init__(self, data: dict):
        youtube, scrobbles = data.get("youtube") or {}, data.get("lastfm") or {}
        self.artist_ids = set(youtube.get("artist_ids", []))
        self.artist_names = {key(n) for n in youtube.get("artist_names", []) + scrobbles.get("artist_names", [])}
        self.song_ids = set(youtube.get("song_ids", []))
        self.seeds = [
            [a["name"] for a in youtube.get("subscriptions", [])],
            [s["artist"] for s in youtube.get("songs", [])],
            scrobbles.get("seeds", []),
        ]

    def knows(self, artist: dict) -> bool:
        return artist.get("id") in self.artist_ids or key(artist["name"]) in self.artist_names

    def pick_seed(self) -> str:
        """An artist you subscribe to, of a song in your library, or that you've scrobbled a few times."""
        pools = [pool for pool in self.seeds if pool]
        if not pools:
            raise LookupError("Your library has no artists to start from.")
        return random.choice(random.choice(pools))


class LibraryLoader:
    """Reads your library in the background, cached for a day so a roll never waits on it twice.

    The two halves refresh separately. When the YouTube Music login has expired, its half
    stays as last read, and a read that fails never replaces what's cached.
    """

    def __init__(self, yt: YTMusic | None):
        self.yt = yt
        self.lastfm_user = lastfm_user()
        self.data: dict = {}
        self.library: Library | None = None
        self.error: str | None = None
        self.warning: str | None = None
        self.fetched_at = 0.0
        self.ready = threading.Event()
        self.refreshing = threading.Lock()
        if yt is None and self.lastfm_user is None:
            self.error = "Not connected to YouTube Music or Last.fm yet. See the README."
            self.ready.set()
            return
        if CACHE.exists():
            cached = json.loads(CACHE.read_text())
            if "artist_ids" in cached:  # written before Last.fm joined: all of it is the YouTube Music half
                cached = {"youtube": cached, "fetched_at": cached["fetched_at"]}
            self.data, self.fetched_at = cached, cached["fetched_at"]
            self.library = Library(cached)
            self.ready.set()
        self.refresh_if_stale()

    def state(self) -> str:
        if self.yt is None and self.lastfm_user is None:
            return "setup"
        if self.library:
            return "ready"
        return "error" if self.error else "loading"

    def refresh_if_stale(self) -> None:
        stale = time.time() - self.fetched_at > CACHE_TTL
        if self.state() != "setup" and stale and self.refreshing.acquire(blocking=False):
            threading.Thread(target=self.refresh, daemon=True).start()

    def refresh(self) -> None:
        try:
            data, now = dict(self.data), time.time()
            if self.yt:
                try:
                    youtube = fetch_youtube(self.yt)
                except Exception as e:
                    youtube = None
                    print(f"Couldn't read YouTube Music: {e}", flush=True)
                if youtube:
                    data["youtube"], self.warning = youtube | {"fetched_at": now}, None
                else:
                    as_of = (data.get("youtube") or {}).get("fetched_at")
                    kept = f"Using your library as of {time.strftime('%-d %b', time.localtime(as_of))}. " if as_of else ""
                    self.warning = f"The YouTube Music login has expired. {kept}Run uv run connect.py to refresh it."
            if self.lastfm_user:
                try:
                    data["lastfm"] = fetch_lastfm(self.lastfm_user) | {"fetched_at": now}
                except Exception as e:
                    print(f"Couldn't read Last.fm scrobbles: {e}", flush=True)
            if not data.get("youtube") and not data.get("lastfm"):
                raise LookupError(self.warning or "Neither YouTube Music nor Last.fm could be read.")
            data["fetched_at"] = now
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(data))
            self.data, self.library, self.fetched_at, self.error = data, Library(data), now, None
        except Exception as e:
            if self.library is None:
                self.error = f"Couldn't read your library: {e}"
        finally:
            self.ready.set()
            self.refreshing.release()


def spread(tracks: list[dict]) -> list[dict]:
    """Reorders so the same artist never plays twice in a row, where that's possible."""
    tracks = list(tracks)
    for i in range(1, len(tracks)):
        previous = key(tracks[i - 1]["artist"])
        if key(tracks[i]["artist"]) == previous:
            j = next((j for j in range(i + 1, len(tracks)) if key(tracks[j]["artist"]) != previous), None)
            if j is not None:
                tracks[i], tracks[j] = tracks[j], tracks[i]
    return tracks


class Station:
    """One roll: a seed artist you know, and up to STATION_SIZE tracks by similar artists you don't."""

    def __init__(self, catalogue: YTMusic, library: Library):
        self.catalogue = catalogue  # YouTube Music, only for finding the suggested tracks
        self.library = library
        self.seed = library.pick_seed()
        self.source: str | None = None
        self.tracks = self.build()

    def unknown(self, name: str) -> bool:
        return not self.library.knows({"name": name}) and key(name) != "variousartists"

    def build(self) -> list[dict]:
        sources = [("Last.fm", self.from_lastfm)] if os.environ.get("LASTFM_API_KEY") else []
        sources.append(("Deezer", self.from_deezer))
        failures = []
        for name, source in sources:
            try:
                artists = source()
            except Exception as e:
                failures.append(e)
                continue
            if artists:
                self.source = name
                break
        else:
            if len(failures) == len(sources):
                raise failures[-1]
            return []

        firsts, seconds = [], []
        for top in artists:
            picks = random.sample(top, min(2, len(top)))
            firsts += picks[:1]
            seconds += picks[1:]
        random.shuffle(seconds)
        tracks = self.on_youtube(firsts)
        wanted = STATION_SIZE - len(tracks)
        tracks += self.on_youtube(seconds[: wanted + wanted // 4])  # a few spare for lookups that miss
        unique = list({t["videoId"]: t for t in tracks}.values())
        return spread(unique[:STATION_SIZE])

    def from_lastfm(self) -> list[list[dict]]:
        """Top tracks of up to ARTISTS of the seed's 100 most similar artists on Last.fm, the less famous first."""
        similar = lastfm("artist.getSimilar", artist=self.seed, limit=100, autocorrect=1)["similarartists"]["artist"]
        candidates = [a["name"] for a in similar if self.unknown(a["name"])]
        random.shuffle(candidates)
        # fame is only known once the top tracks are in, so fetch a few spare to drop the famous
        with ThreadPoolExecutor(SEARCHES) as pool:
            tops = [top for top in pool.map(self.lastfm_top, candidates[: ARTISTS * 3 // 2]) if top]
        tops.sort(key=lambda top: top[0]["fans"] if top[0]["fans"] > MAX_LISTENERS else 0)
        return tops[:ARTISTS]

    def lastfm_top(self, artist: str) -> list[dict]:
        try:
            tracks = lastfm("artist.getTopTracks", artist=artist, limit=8)["toptracks"]["track"]
        except Exception:
            return []
        fans = int(tracks[0]["listeners"]) if tracks else 0
        own = [t for t in tracks if not EDITS.search(t["name"])][:5]
        return [{"title": t["name"], "artist": {"name": t["artist"]["name"]}, "fans": fans} for t in own]

    def from_deezer(self) -> list[list[dict]]:
        """Top tracks of up to ARTISTS from two steps out on Deezer's related-artists graph, the less famous first."""
        found = deezer("search/artist", q=self.seed, limit=5)
        seed = next((a for a in found if key(a["name"]) == key(self.seed)), None)
        if seed is None:
            return []
        near = [a for a in deezer(f"artist/{seed['id']}/related", limit=40) if a["id"] != seed["id"]]
        # hop on from the less famous neighbours, so the pool doesn't drift toward the charts
        quiet = [a for a in near if a["nb_fan"] <= MAX_FANS] or near
        with ThreadPoolExecutor(HOPS) as pool:
            hops = pool.map(self.deezer_related, random.sample(quiet, min(HOPS, len(quiet))))
            far = [a for related in hops for a in related]
        pool_ = {a["id"]: a for a in near + far if a["id"] != seed["id"]}
        candidates = [a for a in pool_.values() if self.unknown(a["name"])]
        random.shuffle(candidates)
        # everyone under MAX_FANS first, in random order; the famous only to make up numbers, least famous first
        candidates.sort(key=lambda a: a["nb_fan"] if a["nb_fan"] > MAX_FANS else 0)
        with ThreadPoolExecutor(SEARCHES) as pool:
            return [top for top in pool.map(self.deezer_top, candidates[:ARTISTS]) if top]

    def deezer_related(self, artist: dict) -> list[dict]:
        try:
            return deezer(f"artist/{artist['id']}/related", limit=40)
        except Exception:  # one missing hop only thins the pool
            return []

    def deezer_top(self, artist: dict) -> list[dict]:
        try:
            tracks = deezer(f"artist/{artist['id']}/top", limit=10)
        except Exception:
            return []
        # their own songs only: a feature on someone famous's track would smuggle the famous one in
        own = [t for t in tracks if t["artist"]["id"] == artist["id"] and not EDITS.search(t["title"])]
        return [t | {"fans": artist["nb_fan"]} for t in own[:5]]

    def on_youtube(self, tracks: list[dict]) -> list[dict]:
        with ThreadPoolExecutor(SEARCHES) as pool:
            return [t for t in pool.map(self.find, tracks) if t]

    def find(self, track: dict) -> dict | None:
        """The YouTube Music version of a Deezer track, unless it's one you know."""
        artist, title = track["artist"]["name"], track["title"]
        try:
            results = self.catalogue.search(f"{artist} {title}", filter="songs", limit=5)
        except Exception:
            return None
        by_artist = [
            r for r in results if r.get("videoId") and any(key(a["name"]) == key(artist) for a in r.get("artists") or [])
        ]
        same_song = [r for r in by_artist if key(title) in key(r["title"]) or key(r["title"]) in key(title)]
        best = (same_song or by_artist or [None])[0]
        if best is None or best["videoId"] in self.library.song_ids or EDITS.search(best["title"]):
            return None
        if any(self.library.knows(a) for a in best["artists"]):
            return None
        return {"videoId": best["videoId"], "title": best["title"], "artist": artist, "fans": track["fans"]}
