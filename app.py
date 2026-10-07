"""Serves the Go page and the call that rolls a station, on 127.0.0.1 only."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ytmusicapi import YTMusic

import lucky

PORT = int(os.environ.get("LUCKY_PORT", "7461"))
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
HERE = Path(__file__).parent
FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/icon.svg": ("icon.svg", "image/svg+xml")}

account = lucky.account_client()
catalogue = YTMusic()  # signed out: only used to look tracks up, so results aren't steered by your history
library = lucky.LibraryLoader(account)


def go() -> dict:
    if not library.ready.wait(timeout=180):
        raise TimeoutError("Still reading your library. Try again in a moment.")
    if library.library is None:
        raise RuntimeError(library.error)
    library.refresh_if_stale()
    for _ in range(3):  # a seed Deezer doesn't know, or one with no unknown neighbours, gets a reroll
        station = lucky.Station(catalogue, library.library)
        if station.tracks:
            break
    else:
        raise LookupError("Three seeds in a row turned up nothing new. Roll again.")
    return {
        "url": lucky.queue_url([t["videoId"] for t in station.tracks]),
        "seed": station.seed,
        "source": station.source,
        "count": len(station.tracks),
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Host") not in HOSTS:
            return self.reply(403, {"error": "forbidden"})
        if self.path in FILES:
            name, content_type = FILES[self.path]
            return self.reply(200, (HERE / name).read_bytes(), content_type)
        if self.path == "/api/status":
            return self.reply(200, {"state": library.state(), "error": library.error, "warning": library.warning})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        # the custom header forces a CORS preflight, so no other site can drive this
        if self.headers.get("Host") not in HOSTS or self.headers.get("X-Lucky") != "1":
            return self.reply(403, {"error": "forbidden"})
        if self.path != "/api/go":
            return self.reply(404, {"error": "not found"})
        try:
            self.reply(200, go())
        except Exception as e:
            self.reply(502, {"error": str(e)})

    def reply(self, code: int, body: dict | bytes, content_type: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    print(f"Feeling Lucky on http://127.0.0.1:{PORT}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
