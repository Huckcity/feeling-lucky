# I'm Feeling Lucky

Google Play Music's "I'm Feeling Lucky" radio, rebuilt with YouTube Music as the
player. Press Go: it picks a random artist you know (and never plays them) and takes
their 100 most similar artists on Last.fm, or walks Deezer's related-artists graph
when Last.fm has nothing. It drops every artist you've subscribed to, saved, liked,
played or scrobbled, and favours the less famous. A couple of top tracks from each are
found on YouTube Music and handed over (48 tracks, about three hours) as a temporary
queue. Nothing is saved. To keep a station, press **Save** at the top of Up next.

## Install

Linux, [uv](https://docs.astral.sh/uv/), and Chrome (otherwise it opens in your
default browser, as a tab).

```sh
git clone https://github.com/Huckcity/feeling-lucky && cd feeling-lucky
uv sync
sed "s|@DIR@|$PWD|g" feeling-lucky.desktop > ~/.local/share/applications/feeling-lucky.desktop
```

Credentials come from environment variables: `LASTFM_API_KEY` and `LASTFM_USER`,
and `YTMUSIC_AUTH` (plus `YTMUSIC_CLIENT_ID` and `YTMUSIC_CLIENT_SECRET` for OAuth).
If a `secrets` command is on your PATH (`secrets list`, `secrets set NAME`,
`secrets run NAME… -- cmd`), the launcher takes them from that store; otherwise export
them before launching.

## Use

Open **I'm Feeling Lucky** from the app grid, or run `bin/feeling-lucky`. It starts
the local server on 127.0.0.1:7461 if needed and opens a Chrome app window with the
Go button. The station plays in that window; <kbd>Alt</kbd>+<kbd>←</kbd> comes back
to Go, and rolling again replaces the station rather than playing over it.

After the 48 tracks, YouTube Music's own Autoplay appends its suggestions, which lean
familiar. Turn Autoplay off in Up next if you'd rather the station just end.

If a station opens paused and silent, Chrome is blocking autoplay for YouTube Music.
Chrome only lets a site autoplay with sound once it trusts it, so allow it once: open
`chrome://settings/content/sound`, and under "Allowed to play sound" add
`music.youtube.com`.

## Connect your library

Your library is how it picks a seed and knows which artists to skip. It has two halves,
and either one alone is enough.

**Last.fm** (scrobbles; never expires): a Last.fm API key from
https://www.last.fm/api/account/create, set as `LASTFM_API_KEY`, and your username
as `LASTFM_USER`. Every artist you've scrobbled is skipped, and artists
with at least 3 scrobbles can seed a station. The key also switches suggestions to
Last.fm's similar artists.

**YouTube Music** (subscriptions, library, likes, history): read with
[ytmusicapi](https://ytmusicapi.readthedocs.io) (unofficial) and set as `YTMUSIC_AUTH`. When this login expires, the last copy of your YouTube Music
library stays in use and the Go page says so; reconnect whenever you want it fresh.

1. In Chrome, signed in, open music.youtube.com and DevTools → Network. Filter on
   `browse`, then click around the site until a POST to `…/youtubei/v1/browse` shows.
2. Select it, open **Headers**, and copy everything under **Request Headers**
   (Firefox: right-click the request → Copy Value → Copy Request Headers).
3. Run `uv run connect.py` here. It reads the clipboard, checks the login works and
   stores it with `secrets set YTMUSIC_AUTH`. Then click I'm Feeling Lucky again.
   Without a `secrets` store, run `uv run ytmusicapi browser` instead, paste the
   headers, and set the JSON it writes as `YTMUSIC_AUTH`.

This login lasts until Google rotates the cookies, which can be within the hour.
ytmusicapi has deprecated this browser-headers route in favour of OAuth. For a login that
stays fresh, use OAuth:
make a Google Cloud OAuth client of type "TVs and Limited Input devices" with the
YouTube Data API v3 enabled, and publish its consent screen to Production (otherwise
the token dies after 7 days). Then run `uv run ytmusicapi oauth --file oauth.json`
and set the compacted JSON as `YTMUSIC_AUTH`, plus `YTMUSIC_CLIENT_ID` and
`YTMUSIC_CLIENT_SECRET`. Delete the file afterwards.

Your library is cached for a day in `~/.cache/feeling-lucky/library.json`. The first
roll waits while it's read, which takes about a minute for a large library.

## Tuning

The knobs are at the top of `lucky.py`. `MAX_LISTENERS` (Last.fm: listeners of an
artist's top track) and `MAX_FANS` (Deezer fans) are how famous an artist can be
before they're only used to make up numbers; lower them for deeper cuts. `SEED_PLAYS`
is how many scrobbles make an artist a possible seed. `HOPS` is how many of the seed's
Deezer related artists bring their own related artists into the pool, which sets how
wide a Deezer station spreads. `ARTISTS` is how many artists a station draws on.

## Moving parts

- The temporary queue is YouTube's undocumented `watch_videos` link. If YouTube
  retires it, Go says "YouTube wouldn't make a temporary queue".
- A Last.fm roll makes about 50 API calls in a burst. Last.fm asks for no more than
  5 a second averaged over five minutes, which ordinary use stays well under.
- Deezer (the fallback, no account) allows 50 calls per 5 seconds and a roll makes
  about 40, so two Deezer rolls within seconds can hit the limit; Go says so, and the
  next roll works.
- YouTube Music is only used to look the suggested tracks up, signed out, so its
  results aren't steered by your history. A track it can't find is skipped.
- Server log: `~/.cache/feeling-lucky/server.log`. Stop it with `pkill -f feeling-lucky/app.py`.
