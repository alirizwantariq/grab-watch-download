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
mgd grab <url>                     # download; HTML pages get media extraction
mgd grab <page-url> --pick 2       # grab the 2nd media link found on a page
mgd grab <page-url> --all          # grab up to 20 found media links
mgd grab-page <url> [--json]       # just list media links found on a page
mgd set-default video ~/Movies     # per-category default directory
mgd set-default root ~/Downloads   # default root (categories become subdirs)
mgd defaults                       # show the current mapping
mgd list                           # unfinished downloads + how to resume
mgd resume <id> | resume all       # resume saved downloads
```

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

## Tests

```sh
cd cli
python3 tests/run_tests.py     # 16 tests; no pytest needed
```

Covered: segmented download correctness, pause + resume byte-perfect,
connection-drop retries, no-range streaming fallback, redirect following,
SHA-256 verification, HTTP forward proxy, SOCKS5 proxy, category routing,
filename de-duplication, page-media extraction.

## Known limitations (v1)

- HLS/DASH (`.m3u8`/`.mpd`) manifests are detected in pages but not reassembled
  — pass segment URLs or use yt-dlp for adaptive streams.
- Credentials in URLs use HTTP Basic; cookies are not stored between runs.
- No bandwidth throttler or scheduler yet (queues land in a later version).