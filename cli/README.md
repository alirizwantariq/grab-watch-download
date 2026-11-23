# mgd — media grab & download (CLI)

A cross-platform (macOS / Windows) command-line download manager built with a
segmented, resumable HTTP engine — no third-party runtime dependencies
(Python 3.10+ standard library only).

The engine design is evidence-based:

| Technique | Source |
| --- | --- |
| **Dynamic file segmentation** — big ranges split in half when a new connection claims work, instead of one static split up front | IDM's own docs: `internetdownloadmanager.com/support/segmentation.html` ("Dynamic file segmentation that uses in-half division rule") |
| **Connection reuse** — workers that finish a range immediately take the next one without extra negotiation stages | same IDM page ("Full reuse of connections that have finished downloading of assigned segments") |
| **Frequent position saves** — segment progress is flushed ~2×/second so a crash, kill, or power loss can resume exactly | same IDM page ("IDM saves all file positions several times per minute") |
| **Bot-protection awareness** — pages behind interactive challenges (e.g. Cloudflare Turnstile, observed live on `grabnwatch.com`) are detected and reported rather than silently corrupting downloads | REA browser capture, this repo's analysis notes |

## Install

```sh
cd media_grab_download/cli
python3 -m mgd --help        # zero-install usage
# or add to PATH:
chmod +x bin/mgd             # then use bin/mgd (add cli/bin to PATH)
```

Windows: works the same with `py -m mgd`; config lives under `%APPDATA%\mgd`.

## Commands

```sh
mgd grab <url>                     # direct files, pages, YouTube, X/Twitter
mgd grab <page-url> --pick 2       # grab the 2nd media link found on a page
mgd grab <page-url> --all          # all links (cap --limit, default 50)
mgd grab "playlist-url" --all      # whole YouTube playlist
mgd grab "youtu.be/.."             # quality menu (interactive) or --quality 720p|audio
mgd grab "x.com/user/status/.."    # X/Twitter media: yt-dlp guest flow first,
                                   # then the x-downloader.com bridge for
                                   # login-gated posts (auto fallback)
mgd grab "x.com/.." -Q 480         # bridge returns a resolution ladder (2xx/4xx)
mgd grab "x.com/.." -Q audio       # same bridge converts to mp3
mgd grab "x.com/.." --resolver ytdlp|x  # force a resolver (default auto)
mgd grab "https://www.erome.tv/.."  # KVS player pages (erome & tube sites):
                                    # inline flashvars quality list, Referer on
                                    # requests, auto cdn mirror fallback
mgd grab "erome.com/a/<album>" --all  # multi-video pages: albums, galleries
mgd page-media "erome.com/a/…"    # JSON catalog of every media item on a page
                                   # (url/title/quality/poster + probed sizes)
mgd native-host                    # Chrome native-messaging host for the
                                   # MediaGrab extension (see chrome-extension/)
mgd watch                         # clipboard monitor: copy any link anywhere
                                   # and it auto-downloads (--all-types)
mgd set-default speed-limit 2MB   # FDM-style global traffic limit (off to clear;
                                   # yt-dlp downloads get their share too)
mgd set-default fragments 8       # stream pieces fetched in parallel (default 8)
mgd grab URL --max-speed 500KB    # per-download speed limit
mgd grab "magnet:?xt=urn:btih:…"  # torrents & magnets: file picker (interactive)
mgd grab "magnet:…" --select-files 1,3-5   # ...or pick files up front
mgd torrent-info "magnet:…"       # JSON file manifest (drives the GUI picker)
mgd stream "magnet:…"             # stream the video while it downloads
                                   # (webtorrent-cli + VLC/IINA, sequential)
mgd jobs                           # [running]/[paused] + per-item actions
mgd pause 1 / mgd resume 1 / mgd cancel 1   # by the number mgd jobs prints…
mgd pause d3fa9c01b2e              # …or by the stable id shown next to it
mgd grab "https://site/video.m3u8" # HLS/DASH streams: assembled with yt-dlp
mgd qualities "youtu.be/.."        # JSON quality/resolution menu (GUI uses this)
mgd convert <file|folder>          # make videos play smoothly on a Mac (only if needed)
mgd convert <folder> --check       # just report what each file needs
mgd set-default mac-ready convert  # after every download: convert | fix | off
mgd doctor                         # engines + config readiness
mgd doctor --json --check-updates  # + yt-dlp version, age, install type, newest release
mgd update-tool yt-dlp             # update it the way it was installed
                                   # (brew upgrade / pip install -U / yt-dlp -U)
mgd grab-page <url> [--json]       # just list media links found on a page
mgd set-default video ~/Movies     # per-category default directory
mgd set-default root ~/Downloads   # default root (categories become subdirs)
mgd defaults                       # show the current mapping
mgd list                           # unfinished downloads + how to resume
mgd resume <id> | resume all       # resume saved downloads
mgd resume <id> --url <link|page>  # continue through a fresh link to the same file
mgd list --json                    # machine-readable (id, state, done, size…)
mgd defaults --json / doctor --json  # settings + tool readiness for GUIs
```

### Download ids

`mgd list` shows two ways to address a download: its row number and a stable
id (`d` + 10 hex digits, derived from where the download is being saved).
Row numbers shift whenever another download starts or finishes, so anything
automated (the MediaGrab app, scripts) should use the id. `pause`, `resume`
and `cancel` accept the id, the output file path, or the row number.
`--json-progress` runs announce their id in the first event:
`{"type":"start","url":…,"id":"d3fa9c01b2e","out_path":…}`.

### Expired links

Video sites often hand out signed links that stop working after a while
(`?token=…`, `?secure=…,<expiry>`). When a download's link dies (401, 403,
404 or 410) mgd re-reads the page it came from, picks the link that looks
like the same file (same path, else same file name) and continues only when
it provably is the same file: the same size, and the same bytes as a piece
already on disk. This happens on resume and mid-download, as long as each
fresh link makes progress. Without a recorded page, the error says so
(`"kind": "link_expired"` in `--json-progress`) and
`mgd resume <id> --url <new link or page>` continues it; a link to a
different file is refused (`"kind": "link_mismatch"`) and the part is left
untouched. A download that fails part-way saves its exact position first.

Error events carry a `kind` the app acts on: `link_expired`,
`link_mismatch`, `network` (connection dropped / host unreachable),
`server` (5xx, 429) and `disk_full`.

### Mac-ready videos

On macOS every finished video is checked with ffprobe and fixed only when it
would not open in QuickTime or would play choppy (no hardware decoding):

| what the file has | what happens |
| --- | --- |
| H.264 / HEVC 4:2:0 + AAC/MP3/AC-3/ALAC in .mp4/.mov | nothing |
| right codecs in .mkv/.webm/.ts, or HEVC tagged `hev1` | rewrapped into .mp4 (`hvc1`), no re-encoding |
| Opus / Vorbis / FLAC / DTS audio | audio converted to AAC, video copied |
| VP9, AV1, 10-bit or 4:4:4 H.264, WMV, MPEG-2/4, interlaced | re-encoded with the Mac's hardware encoder: H.264 up to 1440p, HEVC for 4K, HDR and 10-bit; bitrate matched to the source |

The result is checked (plays, same length) before it replaces the original;
if anything fails, or the conversion is stopped, the original stays.
`--json-progress` reports `{"type":"convert","stage":"start|done|failed|stopped"}`
and `{"type":"convert","progress":0.42}` before the final `done` event.
`mac-ready fix` never re-encodes video; `off` keeps downloads as they arrive.

### What a link turns into

| Link | Handled by |
| --- | --- |
| a file (`.mp4`, `.zip`, …) | the segmented engine |
| a page with `<video>`/KVS player/direct links | page scan, then the engine (page sent as `Referer`) |
| YouTube / X | yt-dlp (quality menu), X falls back to the bridge |
| `.m3u8` / `.mpd` stream playlists | yt-dlp + ffmpeg (a playlist is not a file) |
| a page with no direct media | yt-dlp's site extractors, then a clear error |
| magnet / `.torrent` | aria2c |

### grab options

| Option | Meaning |
| --- | --- |
| `-o PATH` | exact destination file (overrides category routing) |
| `-d DIR`  | destination directory (skips category routing) |
| `-t TYPE` | force category: video audio image archive document software other playlist subtitle |
| `-c N`    | parallel connections (default 6) |
| `-r N`    | retry attempts per range (default 8) |
| `--proxy URL` | `http://h:p`, `https://h:p` (TLS-wrapped) or `socks5://h:p[:user:pass]` |
| `--sha256 HEX` | verify content integrity after download |
| `--cookies FILE` | Netscape `cookies.txt` with your site login: sent with page, file and yt-dlp requests, each cookie only to its own site (remembered for resumes) |
| `--user-agent UA`, `--header K:V` | control request identity |
| `-q` | quiet progress |

Files land in `root/<category>/` (auto-created), or in an explicitly set
default path for that category, or `-o`/`-d` override. A direct file URL is
detected automatically from `Content-Disposition`, `Content-Type`, or the URL;
page URLs are scanned for `<video>`, `<audio>`, `<source>`, `og:video`,
`twitter:player:stream`, and file links.

## How the engine achieves speed / resume / retries

1. **Probe** — one ranged request (`Range: bytes=0-0`) learns the total size
   and whether the server supports ranges, plus `ETag` / `Last-Modified` for
   staleness checks. No full-body request is wasted.
2. **Dynamic segmentation** — the file starts as one large range; each new
   connection splits the largest *unclaimed* range in half (IDM's in-half
   rule) and takes the second half. Finished connections pick up the next
   range immediately, keeping all connections busy.
3. **Retries** — every range request retries independently with exponential
   backoff + jitter (cap 30 s); 429/5xx and transport errors are retryable,
   other 4xx fail fast. Interrupted streams resume at their exact byte
   offset (HTTP `Range`) on the next attempt.
4. **Resume** — state lives beside the partial file (`<name>.mgdpart` +
   `<name>.mgdstate`). On resume, the state is validated against the live
   file identity (`ETag` / `Last-Modified` / size); if the remote changed
   (`If-Range` returns a whole-body 200), the download restarts cleanly
   rather than assembling a corrupt file. `Ctrl+C` is always safe: positions
   are saved and `mgd list` / `mgd resume` continue where it left off.
5. **Integrity** — final size must match the probed size; optional
   `--sha256` verification. Writes go into a preallocated sparse file at
   exact offsets, then an atomic rename publishes the file.

## Proxies and restricted/blocked sites

- `--proxy socks5://127.0.0.1:9050` (e.g. a local Tor or VPN SOCKS port) or
  an `http://` / `https://` proxy; HTTPS targets are tunneled with CONNECT.
- The `HTTP(S)_PROXY` environment is honored automatically. Set a default
  with config or per-run `--proxy`.
- Honest limits: a proxy changes * network reachability and IP reputation. It
  cannot defeat interactive bot challenges (Cloudflare Turnstile, etc.) — for
  those, grab the direct media URL from a real browser (or a tool like
  yt-dlp, which this repo's web app already uses) and hand it to `mgd grab`.

## GUI (macOS app)

`gui/` contains a native SwiftUI app (MediaGrab) that drives this engine:
paste-link window, automatic quality chooser (YouTube/X resolution menus),
a download queue with per-row pause/resume/retry, pickup of downloads started
anywhere else (terminal, browser extension) and a diagnostics sheet. The app
bundle carries its own copy of this engine. Build with
`gui/script/build_app.sh`; make a drag-to-Applications DMG with
`gui/script/create_dmg.sh`. See `gui/README.md`.

## Tests

```sh
cd cli
python3 tests/run_tests.py            # everything; no pytest needed
python3 tests/run_tests.py pause id   # only tests whose name matches
```

The suite runs in a sandbox (`tests/conftest.py`): its own config directory
and download root (`XDG_CONFIG_HOME`, `MGD_ROOT`), and `MGD_NO_OPEN` so
nothing can open the app. It never reads or writes `~/.config/mgd` or
`~/Downloads/mgd`, so it is safe to run while real downloads are going.

Covered: segmented download correctness, pause + resume byte-perfect,
connection-drop retries, no-range streaming fallback, redirect following,
SHA-256 verification, HTTP forward proxy, SOCKS5 proxy, category routing,
filename de-duplication, page-media extraction, stable ids surviving a
shifting list, cancel touching only its own download, atomic state files,
file naming rules, stream/yt-dlp routing, pausing yt-dlp downloads, the
native host protocol and extension hand-offs.

## Known limitations (v1)

- HLS/DASH (`.m3u8`/`.mpd`) streams need yt-dlp and ffmpeg installed; without
  them mgd says so instead of saving the playlist text.
- Credentials in URLs use HTTP Basic; cookies are not stored between runs.
- The CLI starts every `mgd grab` immediately; queueing (N at a time) is done
  by the MediaGrab app.