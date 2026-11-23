"""mgd - media grab & download CLI.

Commands:
  mgd grab <url>            Download a URL. Direct files download immediately;
                            HTML pages get scanned for media links first.
  mgd grab-page <url>       List downloadable media links found on a page.
  mgd set-default <cat> <path>   Save a default directory for a category.
  mgd set-default root <path>    Save the default download root.
  mgd defaults              Show current default paths.
  mgd list                  List unfinished downloads that can be resumed.
  mgd resume <id>|all       Resume saved downloads.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse as u

from . import categorize, grab, net, torrent as _torrent_mod
from .engine import (Downloader, DownloadError, HttpError, Interrupted,
                     Stopper, STATE_SUFFIX, PART_SUFFIX)

BANNER = "mgd - media grab & download (stdlib-only segmented downloader)"
DEFAULT_TIMEOUT = 30.0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="mgd", description=BANNER)
    ap.add_argument("-c", "--conns", type=int, default=None,
                    help="max parallel connections")
    ap.add_argument("-r", "--retries", type=int, default=None,
                    help="retry attempts per range")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("grab", aliases=["get", "dl", "fetch"],
                       help="download a URL (page URLs get media extraction; "
                            "YouTube/X URLs get quality options)")
    g.add_argument("url")
    g.add_argument("-o", "--out", default=None,
                   help="exact destination file path (overrides category dirs)")
    g.add_argument("--out-name", default=None, dest="out_name",
                   help="rename the file (keeps category folders; extension "
                        "added automatically when omitted)")
    g.add_argument("-d", "--dir", default=None,
                   help="destination directory (skips category mapping)")
    g.add_argument("-t", "--type", default=None, dest="filetype",
                   choices=list(categorize.CATEGORIES), help="force category")
    g.add_argument("--quality", "-Q", default=None,
                   help="best|1080|720|480|360|audio (for YouTube/X; omit and "
                        "mgd asks interactively when run in a terminal)")
    g.add_argument("--proxy", default=None,
                   help="proxy URL: http://h:p, https://h:p or socks5://h:p")
    g.add_argument("--referer", default=None,
                   help="Referer header for the media requests (some CDNs "
                        "require the source page; set automatically when "
                        "grabbing via a scanned page)")
    g.add_argument("--max-speed", default=None,
                   help="per-download speed limit: 2MB, 512KB, 500000 (bytes/s); "
                        "global default: mgd set-default speed-limit")
    g.add_argument("--select-files", default=None,
                   help="torrents: download only these file numbers (1-based, "
                        "e.g. 1,3,5-7; see mgd torrent-info)")
    g.add_argument("--all-files", action="store_true",
                   help="torrents: download every file without asking")
    g.add_argument("--cookies", default=None,
                   help="Netscape cookies.txt with your site login (sent only to "
                        "the sites it belongs to; the browser extension fills it in)")
    g.add_argument("--cookies-from-browser", default=None,
                   help="browser to pull cookies from for yt-dlp: chrome, firefox, edge, safari, brave")
    g.add_argument("--resolver", default=None, choices=["auto", "ytdlp", "xd"],
                   help="X/Twitter resolver: auto (yt-dlp then bridge), ytdlp, xd")
    g.add_argument("-c", "--conns", type=int, default=None)
    g.add_argument("-r", "--retries", type=int, default=None)
    g.add_argument("--sha256", default=None, help="expected SHA-256 to verify")
    g.add_argument("--pick", type=int, default=None,
                   help="grab the Nth media link (1-based) instead of prompting")
    g.add_argument("--all", action="store_true",
                   help="download all found media links on a page (cap: --limit, "
                        "default 50); for YouTube/X playlists it means the whole playlist")
    g.add_argument("--limit", type=int, default=50, metavar="N",
                   help="max items downloaded with --all (default 50)")
    g.add_argument("--user-agent", default=None)
    g.add_argument("--header", action="append", default=[], metavar="KEY:VALUE",
                   help="extra request header (repeatable)")
    g.add_argument("--quiet", "-q", action="store_true", help="no progress line")
    g.add_argument("--json-progress", action="store_true", dest="json_progress",
                   help="emit NDJSON progress events (for GUIs/scripts)")

    gp = sub.add_parser("grab-page", aliases=["links", "scan"],
                        help="list media links on an HTML page")
    gp.add_argument("url")
    gp.add_argument("--proxy", default=None)
    gp.add_argument("--json", action="store_true", dest="as_json")

    sp = sub.add_parser("set-default",
                        help="set default dir for a category/root, or preferred cookie browser")
    sp.add_argument("category", choices=["root", "cookies-browser",
                                         "speed-limit", "open-app", "fragments",
                                         *categorize.CATEGORIES])
    sp.add_argument("value", help="path (or browser name for cookies-browser)")

    df = sub.add_parser("defaults", aliases=["config"], help="show defaults + config")
    df.add_argument("--json", action="store_true", dest="as_json",
                    help="machine-readable settings (used by the GUI)")

    lj = sub.add_parser("list", aliases=["jobs", "ls"],
                        help="list unfinished downloads with [running]/[paused] state")
    lj.add_argument("--json", action="store_true", dest="as_json", help="machine-readable list")
    rs = sub.add_parser("resume", aliases=["cont"],
                        help="resume a saved download")
    rs.add_argument("target", help="id from 'mgd list' or 'all'")
    rs.add_argument("--json-progress", action="store_true", dest="json_progress",
                    help="emit NDJSON progress events (for GUIs/scripts)")
    ps = sub.add_parser("pause", help="pause a running download (by id from 'mgd list')")
    ps.add_argument("target")
    cx = sub.add_parser("cancel", aliases=["rm", "del"],
                        help="cancel a download and delete its partial data")
    cx.add_argument("target")
    dr = sub.add_parser("doctor", help="show engines, config and readiness")
    dr.add_argument("--json", action="store_true", dest="as_json",
                    help="machine-readable readiness report (used by the GUI)")
    ql = sub.add_parser("qualities", aliases=["formats"],
                        help="list downloadable qualities/resolutions for a "
                             "URL (JSON; used by the GUI and scripts)")
    ql.add_argument("url")
    ql.add_argument("--proxy", default=None)
    ql.add_argument("--cookies", default=None)
    ql.add_argument("--cookies-from-browser", default=None)

    pm = sub.add_parser("page-media", aliases=["catalog"],
                        help="list every media item on a page with quality/"
                             "thumbnail/size info (JSON; used by the GUI "
                             "batch sheet and the Chrome extension)")
    pm.add_argument("url")
    pm.add_argument("--proxy", default=None)
    pm.add_argument("--user-agent", default=None)
    pm.add_argument("--limit", type=int, default=100)
    pm.add_argument("--cookies", default=None, help="Netscape cookies.txt")
    pm.add_argument("--no-size", action="store_true",
                    help="skip parallel size probing (titles only)")
    nh = sub.add_parser("native-host",
                        help="Chrome native-messaging host for the MediaGrab "
                             "extension (stdio protocol; run by Chrome)")
    w = sub.add_parser("watch", aliases=["clipboard-watch"],
                       help="watch the clipboard and auto-download any link "
                            "you copy (macOS)")
    w.add_argument("--all-types", action="store_true",
                   help="grab any copied http(s) URL, not just media/platform links")
    w.add_argument("--interval", type=float, default=1.5,
                   help="pasteboard poll interval in seconds")
    w.add_argument("-d", "--dir", default=None,
                   help="destination directory for clipboard downloads")
    ti = sub.add_parser("torrent-info", aliases=["torrent-files"],
                        help="list the files inside a magnet/torrent (JSON; "
                             "drives the GUI picker)")
    ti.add_argument("url")
    ti.add_argument("--proxy", default=None)
    st = sub.add_parser("stream",
                        help="stream a torrent's video while it downloads "
                             "(sequential; webtorrent + VLC/IINA)")
    st.add_argument("url")
    return ap


_CFG = None


def _cfg() -> dict:
    global _CFG
    if _CFG is None:
        _CFG = categorize.load_config()
    return _CFG


def proxy_of(args) -> str | None:
    p = getattr(args, "proxy", None)
    if p:
        return p
    return _cfg().get("proxy") or (os.environ.get("HTTPS_PROXY")
                                   or os.environ.get("HTTP_PROXY")
                                   or os.environ.get("https_proxy")
                                   or os.environ.get("http_proxy"))


# ── grab ─────────────────────────────────────────────────────────────────
PLATFORM_RE = re.compile(
    r"https?://(www\.|m\.|music\.)?(youtube\.com|youtu\.be)\S*", re.I)
X_RE = re.compile(
    r"https?://(www\.|mobile\.)?(x\.com|twitter\.com)\S*", re.I)
QUALITY_PATTERNS = ["best", "audio"]


def _is_platform(url: str) -> bool:
    return bool(PLATFORM_RE.match(url) or X_RE.match(url))


# ── X/Twitter third-party resolver bridge (x-downloader.com public API) ────
XD_API = "https://api.x-downloader.com"
XD_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
         "Origin": "https://x-downloader.com", "Referer": "https://x-downloader.com/"}


def _xd_post(path: str, payload: dict, timeout: float = 30.0) -> dict:
    import urllib.request
    req = urllib.request.Request(f"{XD_API}{path}", data=json.dumps(payload).encode(),
                                 headers={**XD_UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _xd_get_state(req_id: str, timeout: float = 30.0) -> dict:
    import urllib.request
    req = urllib.request.Request(f"{XD_API}/download/{req_id}", headers=XD_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _xd_resolve(tweet_url: str, type_ext: str = ".mp4", deadline: float = 75.0) -> dict:
    """validate -> request -> poll /download/<id> until finished|error.
    Raises RuntimeError with the service's own explanation on error."""
    _xd_post("/validate", {"url": tweet_url})
    resp = _xd_post("/request", {"url": tweet_url, "type": type_ext})
    req_id = resp.get("_id")
    if not req_id:
        raise RuntimeError("resolver did not accept the link")
    start = time.monotonic()
    while True:
        time.sleep(2.0)
        st = _xd_get_state(req_id)
        if st.get("status") in ("finished", "error"):
            if st.get("status") == "error":
                msg = (st.get("error") or "the tweet could not be resolved").lower()
                if "account owner" in msg or "author limit" in msg:
                    raise RuntimeError(
                        "X is gating this tweet (author limits who can view it) - "
                        "a signed-in cookie session is required for it")
                if "no video" in msg:
                    raise RuntimeError("no downloadable video in this tweet")
                if "withheld" in msg or "moderator" in msg:
                    raise RuntimeError("this tweet is withheld/removed from X")
                raise RuntimeError(f"resolver error: {msg}")
            return st
        if time.monotonic() - start > deadline:
            raise RuntimeError("resolver timed out preparing the download")


def _xd_pick_format(st: dict, quality: str | None, interactive: bool) -> dict:
    """Choose (host, filename) from the finished response's quality ladder."""
    formats = st.get("formats") or []
    top = {"host": st.get("host"), "filename": st.get("filename"),
           "label": st.get("resolution") or "Highest"}

    def height_of(label: str) -> int:
        import re as _re
        m = _re.search(r"x(\d{3,4})", label or "")
        return int(m.group(1)) if m else 0

    # dedupe: top-level entry equals the highest variant usually
    entries = [top]
    for f in formats:
        if f.get("filename") and f["filename"] != top.get("filename"):
            entries.append({"host": f.get("host"), "filename": f["filename"],
                            "label": f.get("label", "")})
    entries.sort(key=lambda e: -height_of(e.get("label", "")))
    if quality and re.fullmatch(r"\d{3,4}p?", quality.strip().lower()):
        n = int(re.sub(r"p", "", quality.strip().lower()))
        fits = [e for e in entries if height_of(e["label"]) and height_of(e["label"]) <= n]
        return fits[0] if fits else entries[0]
    if quality == "best":
        return entries[0]
    if interactive and len(entries) > 1:
        print(f"\n{st.get('title') or 'X video'} — choose quality:")
        for i, e in enumerate(entries, 1):
            print(f"  {i}. {e.get('label') or 'best'}")
        try:
            ans = input(f"quality [1-{len(entries)}] (Enter=1): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return entries[0]
        if ans.isdigit() and 1 <= int(ans) <= len(entries):
            return entries[int(ans) - 1]
    return entries[0]


def _grab_via_xd(args, cfg: dict, url: str) -> int:
    """Resolve an X/Twitter URL through the public x-downloader.com bridge,
    then download with mgd's segmented engine (parallel + resumable)."""
    try:
        type_ext = ".mp3" if (args.quality or "").lower() in ("audio", "mp3") else ".mp4"
        st = _xd_resolve(url, type_ext)
        chosen = _xd_pick_format(st, args.quality if type_ext == ".mp4" else "best",
                                 sys.stdin.isatty())
        if not chosen.get("host") or not chosen.get("filename"):
            raise RuntimeError("resolver returned no media")
        raw_name = chosen["filename"]          # e.g. v/AbCdEf/Title_🌶️_xY.mp4
        dl_url = f"https://{chosen['host']}/" \
                 f"{u.quote(raw_name, safe='/')}"
        local_name = pathlib.PurePosixPath(raw_name).name  # keep emoji etc.
        # user rename (GUI sheet): keep the bridge extension when omitted
        out_name = getattr(args, "out_name", None)
        if out_name:
            base = _clean_out_name(out_name)
            bridge_ext = pathlib.PurePosixPath(local_name).suffix
            if base and not _typed_extension(base, bridge_ext):
                base += bridge_ext
            local_name = base or local_name
        print(_arrow(f"{st.get('title') or url} [{chosen.get('label', 'best')}]"))
    except RuntimeError as err:
        print(f"error: {err}", file=sys.stderr)
        print("To download gated posts yourself, export cookies with a browser "
              "extension and run: mgd grab URL --cookies cookies.txt", file=sys.stderr)
        return 1
    category = "audio" if type_ext == ".mp3" else (args.filetype or "video")
    if args.out:
        out_path = pathlib.Path(os.path.abspath(os.path.expanduser(args.out)))
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = pathlib.Path(os.path.abspath(os.path.expanduser(args.dir))) \
            if args.dir else categorize.category_dir(cfg, category)
        from .engine import _url_filename
        out_path = categorize.claim_unique_path(
            out_dir, local_name or _url_filename(dl_url))
        out_path.parent.mkdir(parents=True, exist_ok=True)
    dl = Downloader(dl_url, out_path, proxy=args.proxy,
                    conns=args.conns if args.conns else cfg.get("conns", 6),
                    stopper=Stopper())
    dl.pid = os.getpid()
    as_json = getattr(args, "json_progress", False)
    if as_json:
        print(json.dumps(_start_event(url, out_path)), flush=True)

    def _cb(done: int, total: int | None, _json: bool = as_json) -> None:
        if _json:
            print(json.dumps({"type": "progress", "done": done,
                              "total": total or 0}), flush=True)
        else:
            plain(done, total)

    plain = None if (as_json or getattr(args, "quiet", False)) else _make_progress()
    try:
        _journal_record(out_path)
        on_progress = _cb if (as_json or plain is not None) else None
        result = dl.run(on_progress=on_progress)
        if as_json:
            print(json.dumps({"type": "done", "path": str(result.path),
                              "size": result.size}), flush=True)
        else:
            print()
            print(_ok(f"{result.path} {_size_str(result.size)}"))
        return 0
    except KeyboardInterrupt:
        dl.abort()
        if as_json:
            print(json.dumps({"type": "paused"}), flush=True)
        else:
            print("\npaused - resume with: mgd resume", file=sys.stderr)
        return 130
    except (DownloadError, HttpError, OSError) as err:
        if as_json:
            print(json.dumps({"type": "error", "message": str(err)}), flush=True)
        else:
            print(_fail(str(err)), file=sys.stderr)
        return 1

BOT_CHECK_MARKERS = ("sign in to confirm", "not a bot", "confirm you")


DEFAULT_FRAGMENTS = 8


def _ytdlp_speed_args(args, cfg: dict) -> list[str]:
    """Throughput flags for yt-dlp runs.

    - HLS/DASH streams (and YouTube's chunked formats) arrive as many small
      pieces; yt-dlp fetches them one at a time unless told otherwise.
      config "fragments" (default 8) pieces run in parallel.
    - The speed limit (--max-speed, or the global "speed_limit" shared by
      every running download) applies to yt-dlp downloads too. yt-dlp takes
      a fixed rate, so the global limit is split by the downloads running
      when this one starts."""
    out: list[str] = []
    try:
        fragments = int(cfg.get("fragments", DEFAULT_FRAGMENTS))
    except (TypeError, ValueError):
        fragments = DEFAULT_FRAGMENTS
    if fragments > 1:
        out += ["--concurrent-fragments", str(min(fragments, 32))]
    from .engine import parse_speed, _count_active_downloads
    own = _speed_limit_arg(getattr(args, "max_speed", None))
    try:
        global_limit = parse_speed(cfg.get("speed_limit"))
    except ValueError:
        global_limit = None
    limit = own
    if global_limit:
        # this run has no state file yet, so it is not in the count
        share = global_limit // (_count_active_downloads(include_self=False) + 1)
        limit = share if limit is None else min(limit, share)
    if limit:
        out += ["--limit-rate", str(max(1024, int(limit)))]
    return out


def _ytdlp_base_args() -> list[str]:
    return ["--no-warnings", "--extractor-retries", "5",
            "--socket-timeout", "30", "--no-check-certificates"]


def _ytdlp_progress_event(line: str) -> dict | None:
    """'MGDPROG <done> <total> <estimate>' -> NDJSON progress event.

    yt-dlp prints 'NA' for unknown values; total falls back to the estimate."""
    parts = line.split()
    if len(parts) != 4:
        return None

    def _num(s: str) -> int | None:
        return int(s) if s.isdigit() else None

    done = _num(parts[1])
    total = _num(parts[2])
    est = _num(parts[3])
    if done is None:
        return None
    if total is None or total <= 0:
        total = est or 0
    return {"type": "progress", "done": done, "total": total}


def _stop_child(proc) -> None:
    """Stop a helper process (yt-dlp/aria2c) and wait for it: SIGTERM lets
    it close its partial files cleanly, SIGKILL if it will not go."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _run_ytdlp_strategy(ytdlp: str, name: str, cmd: list[str],
                        json_progress: bool = False) -> int:
    print(f"# yt-dlp strategy: {name}", flush=True)
    proc = None
    try:
        if not json_progress:
            proc = subprocess.Popen(cmd)
            return proc.wait()
        # GUI mode: stream yt-dlp output, translating the MGD progress
        # template into NDJSON events and capturing the final file path.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True,
                                bufsize=1, encoding="utf-8", errors="replace")
        final_path: str | None = None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if line.startswith("MGDPROG "):
                ev = _ytdlp_progress_event(line)
                if ev:
                    print(json.dumps(ev), flush=True)
                continue
            if line.startswith("MGDFINAL "):
                final_path = line[len("MGDFINAL "):]
                continue
            print(line, flush=True)
        code = proc.wait()
        if code == 0 and final_path:
            size = None
            try:
                size = os.path.getsize(final_path)
            except OSError:
                pass
            print(json.dumps({"type": "done", "path": final_path,
                              "size": size}), flush=True)
        return code
    except KeyboardInterrupt:
        # pause/cancel from the GUI arrives as SIGTERM to mgd only: yt-dlp
        # would keep downloading in the background unless stopped here
        _stop_child(proc)
        if json_progress:
            print(json.dumps({"type": "paused"}), flush=True)
        print("\nstopped (yt-dlp partials resume on rerun)", file=sys.stderr)
        return 130


def _detect_cookie_browsers() -> list[str]:
    """Browsers whose cookie stores look usable on this Mac; detected first."""
    as_dir = pathlib.Path.home() / "Library" / "Application Support"
    chromium = {
        "arc": as_dir / "Arc",
        "vivaldi": as_dir / "Vivaldi",
        "chrome": as_dir / "Google" / "Chrome",
        "brave": as_dir / "BraveSoftware" / "Brave-Browser",
        "edge": as_dir / "Microsoft Edge",
    }
    found = []
    for name, base in chromium.items():
        try:
            if not base.exists():
                continue
            if any(base.glob("*/Cookies")) or any(base.glob("Profile */Cookies")):
                found.append(name)
            else:
                found.append(name)  # dir exists; a profile may need a launch
        except OSError:
            found.append(name)  # TCC-protected: likely real data, let yt-dlp try
    try:
        safari = pathlib.Path.home() / ("Library/Containers/com.apple.Safari/Data/"
                                        "Library/Cookies/Cookies.binarycookies")
        safari.resolve()  # raises EPERM when data exists but TCC blocks
        if safari.exists():
            found.append("safari")
    except OSError:
        found.append("safari")
    order = {"safari": 99}
    return sorted(set(found), key=lambda b: (order.get(b, 10), b))


C_GREEN, C_RED, C_YELLOW, C_DIM, C_BOLD, C_END = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")


def _colors_enabled() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _ok(msg: str) -> str:
    return f"{C_GREEN}✓{C_END} {msg}" if _colors_enabled() else msg


def _fail(msg: str) -> str:
    return f"{C_RED}✗{C_END} {msg}" if _colors_enabled() else msg


def _arrow(msg: str) -> str:
    return f"{C_BOLD}→{C_END} {msg}" if _colors_enabled() else f"> {msg}"


def _map_quality(q: str | None) -> tuple[str | None, list[str]]:
    """Friendly quality keyword -> (yt-dlp format selector, extra download args).
    (None, []) means 'let yt-dlp pick the best'."""
    if not q:
        return None, []
    q = q.strip().lower()
    if q in ("best", "h", "high", "highest"):
        return None, []
    if q in ("audio", "audio-only", "mp3", "a"):
        if shutil.which("ffmpeg"):
            return "ba/bestaudio", ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        print("(note: ffmpeg not found - saving best audio as m4a instead of mp3)",
              file=sys.stderr)
        return "ba[ext=m4a]/ba/bestaudio", []
    if q.startswith("f"):  # explicit format id(s), e.g. f137 or f137+140
        return q[1:], []
    m = re.fullmatch(r"(\d{3,4})p?", q)
    if m:  # height like 720 or 1080p
        n = m.group(1)
        return (f"bv*[height<={n}]+ba/b[height<={n}]"
                f"/bv*[height<={n}]/b"), []
    if re.fullmatch(r"[\d+]+", q):  # raw format ids e.g. 137+140
        return q, []
    print(f"(unknown quality '{q}' - using best)", file=sys.stderr)
    return None, []


def _fmt_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _size_str_small(n) -> str:
    v = float(n) if n else 0
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.0f} {unit}" if v > 9 or unit in ("B", "KB") else f"{v:.1f} {unit}"
        v /= 1024.0
    return "?"


def _quality_menu(info: dict) -> tuple[str | None, list[str]]:
    """Interactive quality menu built from yt-dlp -J format data."""
    fmts = info.get("formats") or []
    has_ffmpeg = bool(shutil.which("ffmpeg"))
    videos = [f for f in fmts if f.get("vcodec") not in (None, "none")
              and f.get("height")]
    best_h: dict[int, dict] = {}
    for f in sorted(videos, key=lambda f: (-(f["height"]), -(f.get("tbr") or 0))):
        best_h.setdefault(f["height"], f)
    heights = sorted(best_h, reverse=True)[:6]
    title = info.get("title") or "video"
    duration = _fmt_int(info.get("duration"))

    def opt_size(f):
        n = f.get("filesize") or f.get("filesize_approx")
        return f", ~{_size_str_small(n)}" if n else ""

    options: list[tuple[str, str | None, list[str]]] = []
    options.append(("Highest quality (best video + audio)", None, []))
    for h in heights:
        f = best_h[h]
        sel = f"bv*[height<={h}]+ba/b[height<={h}]/bv*[height<={h}]/b"
        options.append((f"{h}p ({f.get('ext', '?')}{opt_size(f)})", sel, []))
    options.append((f"audio only ({'mp3' if has_ffmpeg else 'm4a'})",
                    None, ["__AUDIO__"]))

    extra_meta = f"  ({duration // 60}:{duration % 60:02d})" if duration else ""
    print(f"\n{_colors_enabled() and C_BOLD or ''}{title}{_colors_enabled() and C_END or ''}{extra_meta}")
    print("Choose a quality:")
    for i, (label, _sel, _x) in enumerate(options, 1):
        print(f"  {i}. {label}")
    while True:
        try:
            answer = input(f"quality [1-{len(options)}] (Enter=1): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None, []
        if not answer:
            return None, []
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            _label, sel, x = options[int(answer) - 1]
            if x == ["__AUDIO__"]:
                return _map_quality("audio")
            return sel, []
        print("  pick a number (1-{})".format(len(options)), file=sys.stderr)


def _curate_qualities(info: dict) -> list[dict]:
    """yt-dlp -J info -> [{label, quality}] passable to --quality."""
    fmts = info.get("formats") or []
    has_ffmpeg = bool(shutil.which("ffmpeg"))
    videos = [f for f in fmts if f.get("vcodec") not in (None, "none")
              and f.get("height")]
    best_h: dict[int, dict] = {}
    for f in sorted(videos, key=lambda f: (-(f["height"]), -(f.get("tbr") or 0))):
        best_h.setdefault(f["height"], f)
    out: list[dict] = [{"label": "Highest quality (best video + audio)",
                        "quality": "best"}]
    for h in sorted(best_h, reverse=True)[:6]:
        f = best_h[h]
        n = f.get("filesize") or f.get("filesize_approx")
        size = f", ~{_size_str_small(n)}" if n else ""
        out.append({"label": f"{h}p ({f.get('ext', '?')}{size})",
                    "quality": str(h)})
    out.append({"label": f"audio only ({'mp3' if has_ffmpeg else 'm4a'})",
                "quality": "audio"})
    return out


def _xd_quality_entries(st: dict) -> list[dict]:
    """x-downloader bridge response -> [{label, quality}] passable to --quality.

    The bridge reports widths like '854x480'; --quality takes the height cap
    number, which _xd_pick_format maps back to the best matching entry."""
    def height_of(label: str) -> int:
        m = re.search(r"x(\d{3,4})", label or "")
        return int(m.group(1)) if m else 0

    entries = [st.get("filename") and
               {"label": st.get("resolution") or "Highest", "quality": "best"}]
    seen_filenames = {st.get("filename")}
    for f in st.get("formats") or []:
        fn = f.get("filename")
        if not fn or fn in seen_filenames:
            continue
        seen_filenames.add(fn)
        h = height_of(f.get("label", ""))
        entries.append({"label": f.get("label", ""),
                        "quality": str(h) if h else "best"})
    entries = [e for e in entries if e]
    entries.append({"label": "audio only (mp3)", "quality": "audio"})
    return entries


def _strip_impersonate(args_list: list[str]) -> list[str]:
    """-J probing doesn't need --impersonate; drop flag and its value."""
    out, skip = [], False
    for a in args_list:
        if skip:
            skip = False
            continue
        if a == "--impersonate":
            skip = True
            continue
        out.append(a)
    return out


def _probe_ytdlp_info(ytdlp: str, strategies: list[tuple[str, list[str]]],
                      url: str, extra: list[str]) -> tuple[dict | None, str | None]:
    """Run 'yt-dlp -J' through each strategy; return (info, strategy name)."""
    for name, base in strategies:
        base = _strip_impersonate(base)
        cmd = [ytdlp, *base, "--no-playlist", "-J", *extra, url]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if proc.returncode != 0:
            continue
        try:
            return json.loads(proc.stdout), name
        except ValueError:
            continue
    return None, None


def _grab_platform(args, cfg: dict, url: str | None = None,
                   quick: bool = False, report: bool = True) -> int:
    """YouTube/X/Twitter URLs: resolve via yt-dlp's upstream extractors with a
    strategy chain (plain -> impersonate -> browser cookies), then download
    with an interactive quality menu (or --quality).

    quick=True is for ordinary pages and plain stream links (not a login
    platform): only the two cookie-less strategies run, so an unsupported
    page fails in a couple of seconds instead of walking every browser's
    cookie store. report=False leaves the failure message to the caller."""
    url = url or args.url
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        print("error: YouTube/X URLs need yt-dlp "
              "(install: brew install yt-dlp  or  pip install yt-dlp)",
              file=sys.stderr)
        return 1
    base_flags = ["--no-playlist"] if not args.all else []  # --all: expand playlists
    proxy_args = []
    if args.proxy:
        proxy_args = ["--proxy", args.proxy]
    else:
        envp = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") \
            or os.environ.get("https_proxy") or os.environ.get("http_proxy")
        if envp:
            proxy_args = ["--proxy", envp]
    explicit_cookies = []
    if getattr(args, "cookies", None):
        explicit_cookies = ["--cookies", args.cookies]
    if getattr(args, "cookies_from_browser", None):
        explicit_cookies = ["--cookies-from-browser", args.cookies_from_browser]

    strategies = [
        ("chrome UA", _ytdlp_base_args() + [
            "--user-agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"]),
        ("impersonate chrome", _ytdlp_base_args()
            + ["--impersonate", "chrome",
               "--extractor-args", "generic:impersonate"]),
    ]
    if explicit_cookies:
        strategies.insert(0, ("caller cookies", _ytdlp_base_args() + explicit_cookies))
    else:
        preferred = cfg.get("cookies_browser")
        detected = _detect_cookie_browsers()
        SUPPORTED = {"chrome", "brave", "edge", "vivaldi", "firefox", "chromium",
                     "opera", "whale", "safari"}
        order_hint = []
        if preferred in SUPPORTED:
            order_hint.append(preferred)
        for b in detected:
            if b in SUPPORTED and b not in order_hint:
                order_hint.append(b)
        if "safari" in order_hint:  # needs Full Disk Access; last resort
            order_hint.remove("safari")
            order_hint.append("safari")
        for browser in order_hint:
            strategies.append(
                (f"cookies-from-{browser}",
                 _ytdlp_base_args() + ["--impersonate", "chrome",
                                       "--cookies-from-browser", browser]))

    # ── quality selection ─────────────────────────────────────────────────
    selector: str | None = None
    quality_extra: list[str] = []
    if args.quality is not None:
        selector, quality_extra = _map_quality(args.quality)
    elif not args.all and sys.stdin.isatty():
        info, probe_strategy = _probe_ytdlp_info(
            ytdlp, strategies, url, base_flags + proxy_args)
        if info is not None:
            print(_arrow(f"resolved via: {probe_strategy}"))
            selector, quality_extra = _quality_menu(info)
            strategies = [(strat_name, strat_args) for strat_name, strat_args in
                          strategies if strat_name == probe_strategy] or strategies
        # probe failed: keep full chain for the download (menu skipped)

    # ── destination ───────────────────────────────────────────────────────
    audio_only = bool(selector == "ba/bestaudio" or quality_extra)
    dest_args = []
    out_name = _clean_out_name(getattr(args, "out_name", None))
    if out_name and not args.out:
        # user rename (GUI sheet): name template inside the category dir.
        # yt-dlp decides the container, so a typed media extension is
        # dropped in favour of %(ext)s ("clip.mp4" never ends up holding
        # webm data); literal % must be doubled inside an output template
        base = out_name
        ext = _typed_extension(base)
        if ext and categorize.EXT_MAP.get(ext) in ("video", "audio", "playlist"):
            # includes ".m3u8"/".mpd": the suggested name of a stream link
            base = base[:-(len(ext) + 1)].rstrip() or base
        base = base.replace("%", "%%") + ".%(ext)s"
        default_cat = "audio" if audio_only else (args.filetype or "video")
        d = (os.path.abspath(os.path.expanduser(args.dir)) if args.dir
             else str(categorize.category_dir(cfg, default_cat)))
        pathlib.Path(d).mkdir(parents=True, exist_ok=True)
        dest_args = ["-o", os.path.join(d.replace("%", "%%"), base)]
    elif args.out:
        o = args.out
        if pathlib.Path(o).suffix and "%(ext)s" not in o:
            o += ".%(ext)s"
        dest_args = ["-o", o]
        pathlib.Path(os.path.expanduser(o)).parent.mkdir(parents=True, exist_ok=True)
    elif args.dir:
        dest_args = ["-P", os.path.expanduser(args.dir)]
    else:
        default_cat = "audio" if audio_only else (args.filetype or "video")
        dest_args = ["-P", str(categorize.category_dir(cfg, default_cat))]

    last_err = ""
    # GUI/NDJSON mode: make yt-dlp stream progress + final path in a format
    # _run_ytdlp_strategy translates into the same events mgd emits.
    dl_flags: list[str] = []
    if getattr(args, "json_progress", False):
        dl_flags = ["--newline", "--progress",
                    "--progress-template",
                    "download:MGDPROG %(progress.downloaded_bytes)s "
                    "%(progress.total_bytes)s %(progress.total_bytes_estimate)s",
                    "--print", "after_move:MGDFINAL %(filepath)s"]
    # For X posts without caller-supplied cookies, guest extraction never
    # succeeds for gated media - limit the chain to the two UA/impersonate
    # strategies and hand off to the bridge quickly.
    max_strategies = len(strategies)
    if url.startswith(("https://x.com", "https://twitter.com", "https://www.x.com",
                       "https://www.twitter.com")) and not explicit_cookies:
        max_strategies = 2
    if quick:
        max_strategies = min(max_strategies, 3 if explicit_cookies else 2)
    referer_args = []
    if getattr(args, "referer", None) and args.referer != url:
        referer_args = ["--add-headers", f"Referer:{args.referer}"]
    # Among formats of the chosen resolution, prefer H.264 video + AAC audio:
    # that pair lands in an .mp4 every player opens (QuickTime and Quick
    # Look cannot play the VP9/Opus .webm yt-dlp would otherwise favour).
    # Resolution and frame rate still come first, so 4K stays 4K.
    sort_args: list[str] = []
    by_ids = bool(selector) and "height<=" not in selector and selector != "ba/bestaudio"
    if not audio_only and not by_ids:
        sort_args = ["-S", "res,fps,vcodec:h264,acodec:m4a"]
    speed_args = _ytdlp_speed_args(args, cfg)
    if getattr(args, "json_progress", False):
        print(json.dumps({"type": "start", "url": url, "engine": "yt-dlp"}),
              flush=True)
    for name, base in strategies[:max_strategies]:
        cmd = [ytdlp, *base, *base_flags, *dl_flags, *dest_args, *proxy_args,
               *referer_args, *sort_args, *speed_args]
        if selector:
            cmd += ["-f", selector]
        cmd += [*quality_extra, "--", url]
        code = _run_ytdlp_strategy(ytdlp, name, cmd,
                                   json_progress=getattr(args, "json_progress", False))
        if code == 0:
            return 0
        if code in (130, -signal.SIGTERM, -signal.SIGINT):
            return 130          # paused/cancelled: do not try other strategies
        last_err = f"strategy {name} exited {code}"

    # ── third-party resolver bridge for gated X posts ────────────────────
    resolver = getattr(args, "resolver", None) or cfg.get("resolver", "auto")
    if X_RE.match(url) and resolver in ("auto", "xd"):
        print("# yt-dlp could not resolve this X post - trying the "
              "x-downloader.com bridge")
        if resolver == "auto" and explicit_cookies:
            # caller supplied cookies: one more targeted yt-dlp retry is pointless, bridge anyway
            pass
        return _grab_via_xd(args, cfg, url)

    hint = ""
    if getattr(args, "cookies_from_browser", None) is None and not quick:
        hint = ("\nTip: open this link once in your browser (solve any "
                "challenge/login), then rerun - or export cookies and use "
                "--cookies cookies.txt. Safari users: grant your terminal "
                "Full Disk Access (System Settings > Privacy & Security).")
    if not report:
        return 1            # caller reports / falls back to its own result
    if getattr(args, "json_progress", False):
        print(json.dumps({"type": "error", "message":
                          "could not download this link (the site blocked "
                          "the request or needs a login)"}), flush=True)
    print(f"error: all yt-dlp strategies failed ({last_err}). {hint}",
          file=sys.stderr)
    return 1


_MANIFEST_RE = re.compile(r"\.(m3u8|mpd)(?:[?#]|$)", re.I)


def _is_stream_manifest(url: str) -> bool:
    """HLS/DASH playlists: a list of segments, not a file. The segmented
    engine would only save the playlist text - yt-dlp (+ffmpeg) assembles
    the actual video."""
    return bool(_MANIFEST_RE.search(u.urlsplit(url).path or ""))


def cmd_grab(args) -> int:
    cfg = _cfg()
    conns = args.conns if args.conns else cfg.get("conns", 6)
    retries = args.retries if args.retries else cfg.get("retries", 8)
    headers = {}
    if args.user_agent:
        headers["User-Agent"] = args.user_agent
    for h in args.header or []:
        if ":" not in h:
            print(f"error: header must be KEY:VALUE, got: {h}", file=sys.stderr)
            return 2
        k, v = h.split(":", 1)
        headers[k.strip()] = v.strip()
    stopper = Stopper()

    if _torrent_mod.is_torrent(args.url):
        return _grab_torrent(args, cfg, args.url)

    cookies_file = _use_cookies(getattr(args, "cookies", None))
    if getattr(args, "cookies", None) and not cookies_file:
        args.cookies = None             # do not hand a broken file to yt-dlp

    if args.referer:
        # explicit referer: CDNs gated by it (erome etc.) would 403 even the
        # is-it-a-page probe below, sending the grab down a dead-end path
        headers.setdefault("Referer", args.referer)

    if _is_platform(args.url):
        return _grab_platform(args, cfg)

    have_ytdlp = bool(shutil.which("yt-dlp"))
    if _is_stream_manifest(args.url):
        if have_ytdlp:
            print("# HLS/DASH stream - assembling with yt-dlp")
            return _grab_platform(args, cfg, args.url, quick=True)
        print("error: this is an HLS/DASH stream playlist; install yt-dlp "
              "and ffmpeg to download it (brew install yt-dlp ffmpeg)",
              file=sys.stderr)
        return 1

    try:
        page_links = _page_media(args.url, headers, args.proxy)
    except HttpError as err:
        if err.status in (403, 429) and shutil.which("yt-dlp"):
            print(f"# plain fetch blocked by {err.status} - delegating to yt-dlp")
            return _grab_platform(args, cfg, args.url)
        print(f"error: {err} - this host may block automated clients "
              "(try --proxy or a different network)", file=sys.stderr)
        return 1
    except OSError as err:
        print(f"error: could not reach the host after retries: "
              f"{type(err).__name__}: {err}\n(try again, or use --proxy / a "
              "different network)", file=sys.stderr)
        return 1
    if page_links is None:
        targets = [{"url": args.url, "kind": "direct", "title": ""}]
    else:
        links = page_links
        # a page whose only video is an adaptive stream, or where the plain
        # scan finds nothing (players that build their source in JS): let
        # yt-dlp's site extractors have a go before giving up
        direct = [l for l in links if not _is_stream_manifest(l["url"])]
        playable = [l for l in direct if l["kind"] != "link-file"]
        if have_ytdlp and not playable and not args.pick:
            print("# no direct media file on this page - trying yt-dlp extractors")
            code = _grab_platform(args, cfg, args.url, quick=True, report=False)
            if code in (0, 130):
                return code
            if not direct:
                msg = ("no downloadable media found on that page (no direct "
                       "file, and yt-dlp does not support this site)")
                if getattr(args, "json_progress", False):
                    print(json.dumps({"type": "error", "message": msg}), flush=True)
                print(f"error: {msg}", file=sys.stderr)
                return 1
        if not args.pick:
            # --pick N counts the links exactly as `mgd grab-page` lists
            # them; otherwise prefer real files over stream playlists
            links = direct or links
        if not links:
            print("no downloadable media found on that page "
                  "(use 'mgd grab-page' to inspect candidates)", file=sys.stderr)
            return 1
        if args.all:
            links = links[:args.limit]
        elif args.pick:
            if not 1 <= args.pick <= len(links):
                print(f"error: --pick {args.pick} out of range ({len(links)} found)",
                      file=sys.stderr)
                return 2
            links = [links[args.pick - 1]]
        elif sys.stdin.isatty():
            print(f"{len(links)} media link(s) found:")
            for i, l in enumerate(links[:25], 1):
                extra = f" - {l['title']}" if l["title"] else ""
                print(f"  {i:>2}. [{l['kind']:<10}] {l['url']}{extra}")
            try:
                answer = input(f"grab which link [1-{min(len(links), 25)}] (q=quit): ")
            except (EOFError, KeyboardInterrupt):
                print()
                return 130
            answer = answer.strip().lower()
            if answer in ("q", "quit"):
                return 130
            if not (answer.isdigit() and 1 <= int(answer) <= min(len(links), 25)):
                print("no valid choice made", file=sys.stderr)
                return 2
            links = [links[int(answer) - 1]]
        else:
            cand = grab.pick_media_candidate(links)
            if cand is None:
                print("no media candidate found", file=sys.stderr)
                return 1
            links = [cand]
        targets = links
    stopper = Stopper()

    results, failures = [], 0
    for t in targets:
        url_i = t["url"]
        title = f" ({t['title']})" if t.get("title") else ""
        if _is_stream_manifest(url_i):
            # a stream playlist picked from a page: assemble it with yt-dlp
            # (the page is the Referer its CDN expects)
            if not have_ytdlp:
                msg = ("this is an HLS/DASH stream; install yt-dlp and ffmpeg "
                       "to download it (brew install yt-dlp ffmpeg)")
                if getattr(args, "json_progress", False):
                    print(json.dumps({"type": "error", "message": msg}), flush=True)
                print(_fail(f"{url_i}: {msg}"), file=sys.stderr)
                failures += 1
                continue
            if page_links is not None and not args.referer:
                args.referer = args.url
            code = _grab_platform(args, cfg, url_i, quick=True)
            if code == 130:
                return 130
            if code == 0:
                results.append(url_i)
            else:
                failures += 1
            continue
        try:
            out_path = _destination(args, cfg, url_i)
        except (RuntimeError, OSError) as err:
            if getattr(args, "json_progress", False):
                print(json.dumps({"type": "error", "message": str(err)}), flush=True)
            print(f"error: {err}", file=sys.stderr)
            return 2
        if getattr(args, "json_progress", False):
            print(json.dumps(_start_event(url_i, out_path)), flush=True)
        print(_arrow(f"{url_i}{title}"))
        print(f"  saving to {out_path}")
        _journal_record(out_path)
        dl = Downloader(url_i, out_path, proxy=args.proxy, conns=conns,
                        retries=retries, stopper=stopper,
                        timeout=DEFAULT_TIMEOUT,
                        max_speed=_speed_limit_arg(getattr(args, "max_speed", None)),
                        referer=(getattr(args, "referer", None)
                                 or (args.url if page_links is not None else None)),
                        mirror_hosts=_mirror_hosts_for(url_i) or
                                     _mirror_hosts_for(args.url),
                        cookies_file=cookies_file,
                        expected_sha256=args.sha256 if t["kind"] == "direct" else None)
        dl.pid = os.getpid()
        try:
            progress_cb = None
            if args.json_progress:
                def progress_cb(done, total):
                    print(json.dumps({"type": "progress", "done": done,
                                      "total": total}), flush=True)
            elif not args.quiet:
                progress_cb = _make_progress()
            result = dl.run(on_progress=progress_cb)
            print()
            if getattr(args, "json_progress", False):
                print(json.dumps({"type": "done", "path": str(result.path),
                                  "size": result.size}), flush=True)
            else:
                print(_ok(f"{result.path} {_size_str(result.size)}"
                          + (" [resumed]" if result.resumed else "")))
            results.append(result)
        except KeyboardInterrupt:
            dl.abort()
            print(f"\npaused - state saved; continue with: {_arrow('mgd resume')}".strip(),
                  file=sys.stderr)
            return 130
        except (DownloadError, HttpError, OSError) as err:
            if getattr(args, "json_progress", False):
                print(json.dumps(
                    {"type": "error", "message": str(err)}), flush=True)
            else:
                print(_fail(f"{url_i}: {err}"), file=sys.stderr)
            failures += 1
    if results:
        print(f"{len(results)} downloaded, {failures} failed")
    return 1 if failures and not results else 0


def _fetch_page(url: str, headers: dict, proxy: str | None,
                attempts: int = 3) -> str | None:
    """Full page text, or None when the url is a plain file (not HTML).
    Retries transient connection failures (TLS hiccups, resets)."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        conn = None
        try:
            conn, resp = net.open_request(url, proxy, "GET",
                                           headers=headers or None,
                                           timeout=DEFAULT_TIMEOUT)
            close = getattr(conn, "close", None)
            try:
                status = resp.status
                if status >= 400:
                    raise HttpError(status)
                body_head = resp.read(4096)
                if not grab.is_html(resp.getheader("Content-Type"), body_head):
                    return None
                return (body_head + resp.read()).decode("utf-8", "replace")
            finally:
                if callable(close):
                    try:
                        close()
                    except OSError:
                        pass
        except OSError as exc:
            # ssl.SSLError (e.g. BAD_SIGNATURE), resets, timeouts: transient
            last_exc = exc
            if attempt < attempts:
                time.sleep(min(2.0, 0.5 * (2 ** (attempt - 1))))
    raise last_exc if last_exc else HttpError(500, "page fetch failed")


def _page_media(url: str, headers: dict, proxy: str | None,
                attempts: int = 3):
    """None if url is a plain file; [{kind,url,title}] if it is an HTML page."""
    html = _fetch_page(url, headers, proxy, attempts)
    return None if html is None else grab.extract_links(html, url)


# Backup cdn mirror hostnames per site. Sites that balance between several
# edge hosts (erome uses cdn77: observed "nude.erome.tv" hard-502ing for a
# region while "sextape.erome.tv" served the very same contents path) get
# another round of probes on a sibling host when one edge is down.
MIRROR_HOSTS = {
    "erome.tv": ["sextape.erome.tv", "nude.erome.tv"],
}


# ── torrents & magnets (aria2 engine + webtorrent streaming) ────────────

ARIA2_SUMMARY_RE = re.compile(
    r"\[#\w+\s+([\d.]+)([KMGT]i?B)?/([\d.]+)([KMGT]i?B)?\((\d+)%\)")
_ARIA2_UNITS = {"B": 1, "KiB": 1 << 10, "MiB": 1 << 20,
                "GiB": 1 << 30, "TiB": 1 << 40, None: 1}
TORRENT_STATE_KIND = "mgd-torrent-state"


def _aria2_bytes(num: str, unit: str | None) -> int:
    return int(float(num) * _ARIA2_UNITS.get(unit, 1))


def _torrent_metadata(url: str, cfg: dict, proxy: str | None) -> tuple[dict, str]:
    """(parsed metainfo, local .torrent path). Magnets resolve via aria2
    DHT metadata fetch; remote .torrent urls are downloaded first."""
    aria2 = shutil.which("aria2c")
    if not aria2:
        raise RuntimeError("aria2c not found (brew install aria2)")
    import tempfile
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mgd-torrent-"))
    src = url
    if _torrent_mod.is_magnet(url):
        # DHT lookup; cold magnets can take 5-60s
        proc = subprocess.run(
            [aria2, url, "--bt-metadata-only=true", "--bt-save-metadata=true",
             "--dir", str(tmp), "--console-log-level", "warn",
             "--bt-max-peers", "80", "--enable-dht=true"],
            capture_output=True, text=True, timeout=180)
        saved = sorted(tmp.glob("*.torrent"), key=lambda p: p.stat().st_mtime)
        if not saved:
            raise RuntimeError("could not fetch magnet metadata via DHT "
                               f"(aria2: {(proc.stderr or proc.stdout).strip()[-200:]})")
        src = str(saved[-1])
    elif re.match(r"^https?://", url):
        conn, resp = net.open_request(url, proxy, "GET", timeout=DEFAULT_TIMEOUT)
        try:
            data = resp.read()
        finally:
            close = getattr(conn, "close", None)
            if callable(close):
                close()
        if resp.status >= 400:
            raise HttpError(resp.status)
        src = str(tmp / "remote.torrent")
        pathlib.Path(src).write_bytes(data)
    raw = pathlib.Path(src).read_bytes()
    meta = _torrent_mod.bdecode(raw)
    if not isinstance(meta, dict):
        raise RuntimeError("not a valid torrent file")
    return meta, src


def _torrent_file_picker(files: list[dict], interactive: bool,
                         args) -> list[int] | None:
    """Which file indices to download (1-based, aria2 --select-file).
    None = all files; [] = user declined."""
    pick = getattr(args, "select_files", None)
    if pick:
        out: list[int] = []
        for part in str(pick).split(","):
            part = part.strip()
            if "-" in part:
                a, _, b = part.partition("-")
                out += list(range(int(a), int(b) + 1))
            elif part:
                out.append(int(part))
        valid = {f["index"] for f in files}
        bad = [i for i in out if i not in valid]
        if bad:
            raise RuntimeError(f"--select-files out of range: {bad}")
        return sorted(set(out))
    if getattr(args, "all_files", False):
        return None
    if not interactive or len(files) == 1:
        return None          # non-tty default: everything
    print(f"{len(files)} file(s) in this torrent:")
    for f in files:
        mark = " (video)" if f["video"] else ""
        print(f"  {f['index']:>3}. {_size_str(f['size'])}  {f['path']}{mark}")
    try:
        answer = input("download which files (all / 1,3,5-7 / q): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return []
    if answer in ("q", "quit", ""):
        return []
    if answer in ("all", "a", "*"):
        return None
    return _torrent_file_picker(files, False,
                                argparse.Namespace(select_files=answer))


def _torrent_state_path(dest_dir: pathlib.Path, name: str) -> pathlib.Path:
    safe = re.sub(r"[^\w.\- ]", "_", name)[:80] or "torrent"
    return dest_dir / f"{safe}{STATE_SUFFIX}"


def _torrent_save_state(st: dict) -> None:
    try:
        categorize.atomic_write_text(st["_path"], json.dumps(
            {k: v for k, v in st.items() if k != "_path"}))
    except OSError:
        pass


def _torrent_spawn_aria2(url: str, src: str, dest_dir: pathlib.Path,
                         select: list[int] | None, cfg: dict,
                         json_mode: bool, on_progress=None, st_path=None):
    """Run aria2c, translating its summary lines into mgd's NDJSON events.
    Raises DownloadError when aria2 fails; SIGTERM surfaces as
    KeyboardInterrupt (handled by the caller, state already on disk)."""
    aria2 = shutil.which("aria2c")
    if not aria2:
        raise RuntimeError("aria2c not found (brew install aria2)")
    cmd = [aria2, url,
           "--dir", str(dest_dir),
           "--seed-time", "0",
           "--bt-detach-seed-only=true",
           "--summary-interval", "1",
           "--console-log-level", "warn",
           "--file-allocation", "none",
           "--auto-file-renaming=false",
           "--continue=true",
           "--bt-max-peers", "80",
           "--enable-dht=true"]
    if select:
        cmd += ["--select-file", ",".join(str(i) for i in select)]
    from .engine import parse_speed
    limit = parse_speed(cfg.get("speed_limit"))
    if limit:
        cmd += ["--max-overall-download-limit", str(limit)]
    if json_mode:
        print(json.dumps(_start_event(url, dest_dir, st_path)), flush=True)
    else:
        print(_arrow(f"torrent: {url[:70]}"))
        print(f"  engine: aria2c  dir: {dest_dir}"
              + (f"  files: {','.join(str(i) for i in select)}"
                 if select else "  files: all"))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    done = last_total = 0
    last_save = 0.0
    for line in proc.stdout:
        m = ARIA2_SUMMARY_RE.search(line)
        if not m:
            continue
        done = _aria2_bytes(m.group(1), m.group(2))
        last_total = _aria2_bytes(m.group(3), m.group(4))
        if json_mode:
            print(json.dumps({"type": "progress", "done": done,
                              "total": last_total}), flush=True)
        else:
            print(f"\r  {done} / {last_total} bytes", end="", flush=True)
        # keep the state file's progress fresh (mgd jobs / GUI read it)
        now = time.monotonic()
        if now - last_save > 2.0:
            last_save = now
            if on_progress:
                on_progress(done, last_total)
    code = proc.wait()
    if code != 0:
        raise DownloadError(f"aria2c exited with {code}")
    if not json_mode:
        print()
    return done, last_total


def _grab_torrent(args, cfg: dict, url: str) -> int:
    """Torrent/magnet grab: fetch metadata, let the user pick files
    (interactive list, --select-files, or the GUI picker), then run
    aria2c with mgd's NDJSON progress contract."""
    meta, src = _torrent_metadata(url, cfg, proxy_of(args))
    name = _torrent_mod.torrent_name(meta) or "torrent"
    files = _torrent_mod.torrent_files(meta)
    select = _torrent_file_picker(files, sys.stdin.isatty(), args)
    if select == []:
        print("no files selected - nothing to do")
        return 130
    if args.dir:
        dest_dir = pathlib.Path(os.path.abspath(os.path.expanduser(args.dir)))
    elif args.out:
        dest_dir = pathlib.Path(os.path.abspath(os.path.expanduser(args.out)))
        if dest_dir.suffix:
            dest_dir = dest_dir.parent
    else:
        has_video = any(f["video"] for f in files)
        dest_dir = categorize.category_dir(cfg, "video" if has_video else "other")
    dest_dir.mkdir(parents=True, exist_ok=True)

    selected_files = files if select is None else [f for f in files
                                                   if f["index"] in select]
    total = sum(f["size"] for f in selected_files)
    st = {"kind": TORRENT_STATE_KIND, "url": url, "name": name,
          "dir": str(dest_dir), "select": select,
          "files": selected_files, "size": total,
          "done": 0, "pid": os.getpid(),
          "filename": name, "out_path": str(dest_dir)}
    st_path = _torrent_state_path(dest_dir, name)
    st["_path"] = st_path
    _torrent_save_state(st)
    _journal_record(st_path)

    json_mode = bool(getattr(args, "json_progress", False))

    def _st_progress(done: int, total: int, _st=st) -> None:
        _st["done"] = done
        _st["size"] = total or _st.get("size", 0)
        _torrent_save_state(_st)

    try:
        _torrent_spawn_aria2(url, src, dest_dir, select, cfg,
                              json_mode, on_progress=_st_progress,
                              st_path=st_path)
        st_path.unlink(missing_ok=True)
        if json_mode:
            print(json.dumps({"type": "done", "path": str(dest_dir),
                              "size": total}), flush=True)
        else:
            print(_ok(f"{name} complete → {dest_dir}"))
        return 0
    except KeyboardInterrupt:
        if json_mode:
            print(json.dumps({"type": "paused"}), flush=True)
        else:
            print("\npaused - state saved; resume with: mgd resume", file=sys.stderr)
        return 130
    except (DownloadError, RuntimeError) as err:
        if json_mode:
            print(json.dumps({"type": "error", "message": str(err)}), flush=True)
            return 1
        print(_fail(str(err)), file=sys.stderr)
        return 1


def _resume_torrent(st: dict, st_path: pathlib.Path, json_mode: bool) -> int:
    """Resume a paused torrent (aria2's .aria2 control files hold the
    piece state; we re-run with the stored magnet/dir/selection)."""
    cfg = _cfg()
    url = st.get("url", "")
    dest_dir = pathlib.Path(st.get("dir") or st.get("out_path") or ".")
    dest_dir.mkdir(parents=True, exist_ok=True)
    meta, src = _torrent_metadata(url, cfg, cfg.get("proxy"))
    st["pid"] = os.getpid()
    st["_path"] = st_path
    _torrent_save_state(st)

    def _st_progress(done: int, total: int, _st=st) -> None:
        _st["done"] = done
        _st["size"] = total or _st.get("size", 0)
        _torrent_save_state(_st)

    try:
        _torrent_spawn_aria2(url, src, dest_dir,
                             st.get("select"), cfg, json_mode,
                             on_progress=_st_progress, st_path=st_path)
        st_path.unlink(missing_ok=True)
        if json_mode:
            print(json.dumps({"type": "done", "path": str(dest_dir),
                              "size": st.get("size", 0)}), flush=True)
        return 0
    except KeyboardInterrupt:
        if json_mode:
            print(json.dumps({"type": "paused"}), flush=True)
        return 130


def cmd_torrent_info(args) -> int:
    """JSON manifest for a magnet/torrent: {name, hash, files:[{index,
    path, size, video}]}. Drives the GUI's torrent file-picker sheet."""
    out = {"name": None, "hash": None, "files": [], "error": None}
    try:
        meta, _ = _torrent_metadata(args.url, _cfg(), args.proxy)
        out["name"] = _torrent_mod.torrent_name(meta)
        try:
            out["hash"] = _torrent_mod.info_hash(meta)
        except Exception:
            h = _torrent_mod.magnet_info_hash(args.url)
            out["hash"] = h if h else None
        out["files"] = _torrent_mod.torrent_files(meta)
    except Exception as err:
        out["error"] = f"{type(err).__name__}: {err}"
        print(json.dumps(out))
        return 1
    print(json.dumps(out))
    return 0


def cmd_stream(args) -> int:
    """Stream a torrent's video sequentially while it downloads
    (webtorrent-cli + VLC/IINA)."""
    url = args.url
    if not _torrent_mod.is_torrent(url):
        print("error: stream needs a magnet link or .torrent file", file=sys.stderr)
        return 2
    wt = shutil.which("webtorrent")
    if not wt:
        print("streaming needs webtorrent-cli (sequential download). "
              "Install it with:\n  brew install webtorrent-cli", file=sys.stderr)
        return 2
    if pathlib.Path("/Applications/IINA.app").exists():
        player = "iina"
    elif pathlib.Path("/Applications/VLC.app").exists():
        player = "vlc"
    else:
        print("no video player found - install VLC (brew install --cask vlc)",
              file=sys.stderr)
        return 2
    print(_arrow(f"streaming {url[:70]}"))
    print(_arrow(f"player: {player} — close the player when done watching"))
    stream_dir = pathlib.Path("~/Downloads/mgd/torrent-stream").expanduser()
    stream_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([wt, url, f"--{player}", "--keep-seeding"],
                          cwd=stream_dir)
    return proc.returncode


def _use_cookies(path) -> str | None:
    """Send the cookies in this cookies.txt with every request of this
    process (each only to its own site). Returns the path when loaded."""
    if not path:
        return None
    path = os.path.abspath(os.path.expanduser(str(path)))
    jar = net.load_cookie_file(path) if os.path.exists(path) else None
    if jar is None:
        print(f"# cookies file not usable, continuing without it: {path}", file=sys.stderr)
        return None
    net.set_cookie_jar(jar)
    return path


def _cookie_dir() -> pathlib.Path:
    return categorize.config_path().parent / "cookies"


def _start_event(url: str, out_path=None, st_path=None) -> dict:
    """NDJSON 'start' event. Carries the download's stable id and where it
    is being saved, so a GUI can pause/resume/cancel exactly this download
    later instead of guessing by url or list position."""
    ev = {"type": "start", "url": url}
    if st_path is None and out_path is not None:
        st_path = pathlib.Path(os.path.abspath(str(out_path)) + STATE_SUFFIX)
    if st_path is not None:
        ev["id"] = _state_id(pathlib.Path(st_path))
    if out_path is not None:
        ev["out_path"] = str(out_path)
    return ev


_NAME_BAD = '/\\:\x00' + "".join(chr(c) for c in range(1, 32))
# extensions a typed name may legitimately end in beyond categorize.EXT_MAP
_EXTRA_EXTS = {"bin", "torrent", "m4s", "part", "dat", "log", "db", "img",
               "tgz", "tbz", "whl", "jar", "ipa", "vob", "m2ts", "f4v", "weba"}


def _clean_out_name(raw) -> str:
    """A user-typed file name made safe to drop inside a folder: no path
    separators or control characters, no leading dots/spaces."""
    name = "".join("-" if ch in _NAME_BAD else ch for ch in str(raw or ""))
    return name.strip().lstrip(".").strip()[:200]


def _typed_extension(name: str, also: str = "") -> str:
    """The real file extension at the end of a typed name ('' when the dot
    is just punctuation: 'Dr. Strange', 'take 1.2')."""
    suffix = pathlib.PurePosixPath(name).suffix.lower().lstrip(".")
    if not suffix or " " in suffix:
        return ""
    known = suffix in categorize.EXT_MAP or suffix in _EXTRA_EXTS \
        or (also and suffix == also.lower().lstrip("."))
    return suffix if known else ""


def _speed_limit_arg(value) -> int | None:
    """--max-speed flag value -> bytes/s (None when unset)."""
    if not value:
        return None
    from .engine import parse_speed
    try:
        return parse_speed(value)
    except ValueError as err:
        print(f"warning: {err}; continuing without the per-download limit",
              file=sys.stderr)
        return None


def _mirror_hosts_for(url: str) -> list[str]:
    for site, hosts in MIRROR_HOSTS.items():
        if site in url:
            return list(hosts)
    return []


def _destination(args, cfg: dict, url: str) -> pathlib.Path:
    from .engine import _url_filename
    if args.out:
        p = pathlib.Path(os.path.abspath(os.path.expanduser(args.out)))
        if p.exists() and p.is_dir():
            p = p / _url_filename(url)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    # --out-name: rename while keeping the category folder routing
    name = _url_filename(url)
    out_name = getattr(args, "out_name", None)
    if out_name:
        typed = _clean_out_name(out_name)
        if not typed:
            raise RuntimeError("--out-name cannot be empty")
        orig_ext = pathlib.PurePosixPath(name).suffix      # ".mp4" or ""
        if not _typed_extension(typed, orig_ext) and orig_ext:
            # no (real) extension typed -> keep the original one
            typed = f"{typed}{orig_ext}"
        name = typed
    category = args.filetype or categorize.categorize(name, None)
    d = pathlib.Path(os.path.abspath(os.path.expanduser(args.dir))) if args.dir \
        else categorize.category_dir(cfg, category)
    d.mkdir(parents=True, exist_ok=True)
    return categorize.claim_unique_path(d, name)


class _Progress:
    def __init__(self):
        self.lock = threading.Lock()
        self.last = 0.0

    def __call__(self, done: int, total: int | None) -> None:
        now = time.monotonic()
        with self.lock:
            if now - self.last < 0.2:
                return
            self.last = now
            if total:
                pct = min(100, done * 100 // total)
                bar = "#" * min(30, pct * 30 // 100)
                sys.stdout.write(f"\r[{bar:<30}] {pct:>3}%  {_size_str(done)}/{_size_str(total)}   ")
            else:
                sys.stdout.write(f"\rdone: {done} bytes   ")
            sys.stdout.flush()


def _make_progress() -> _Progress:
    return _Progress()


def _size_str(n) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return ""


# ── grab-page ────────────────────────────────────────────────────────────
def cmd_grab_page(args) -> int:
    try:
        media = _page_media(args.url, {}, args.proxy)
    except HttpError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    if media is None:
        print("that URL is a direct file, not an HTML page (use: mgd grab)", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(media, indent=2))
        return 0
    print(f"{len(media)} media link(s) on {args.url}")
    for i, l in enumerate(media, 1):
        print(f"  {i:>2}. [{l['kind']:<10}] {l['url']}" + (f" - {l['title']}" if l.get("title") else ""))
    return 0


# ── config commands ──────────────────────────────────────────────────────
def cmd_set_default(args) -> int:
    cfg = _cfg()
    if args.category == "root":
        cfg["root"] = args.value
    elif args.category == "cookies-browser":
        name = args.value.strip().lower()
        cfg["cookies_browser"] = name
        print(f"saved: preferred yt-dlp cookie browser -> {name} "
              "(yt-dlp supports: brave, chrome, chromium, edge, firefox, opera, safari, vivaldi, whale)")
    elif args.category in ("speed-limit", "speed_limit"):
        from .engine import parse_speed
        try:
            limit = parse_speed(args.value)
        except ValueError as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        if limit:
            cfg["speed_limit"] = limit
            print(f"saved: global speed limit -> {args.value} "
                  f"({limit} B/s, shared by every running download; "
                  "applies to new downloads and within ~5s to running ones)")
        else:
            cfg.pop("speed_limit", None)
            print("saved: global speed limit -> off")
    elif args.category == "fragments":
        try:
            n = int(args.value)
        except ValueError:
            print("error: fragments takes a number (1 = one piece at a time)", file=sys.stderr)
            return 2
        cfg["fragments"] = max(1, min(32, n))
        print(f"saved: stream pieces downloaded in parallel -> {cfg['fragments']}")
    elif args.category == "open-app":
        val = args.value.strip().lower()
        if val not in ("on", "off"):
            print("error: open-app accepts on or off", file=sys.stderr)
            return 2
        cfg["open_app"] = val
        print(f"saved: open MediaGrab on extension downloads -> {val}")
    else:
        cfg.setdefault("defaults", {})[args.category] = args.value
    categorize.save_config(cfg)
    if args.category not in ("cookies-browser", "speed-limit", "open-app", "fragments"):
        print(f"saved: {args.category} -> {args.value}")
    return 0


def cmd_defaults(_args) -> int:
    cfg = _cfg()
    root = categorize.default_root(cfg)
    if getattr(_args, "as_json", False):
        print(json.dumps({
            "root": str(root),
            "speed_limit": cfg.get("speed_limit") or None,
            "defaults": cfg.get("defaults") or {},
            "conns": cfg.get("conns", 6),
            "open_app": cfg.get("open_app", "on"),
            "cookies_browser": cfg.get("cookies_browser"),
        }))
        return 0
    print(f"root: {root}")
    limit = cfg.get("speed_limit")
    print(f"speed limit: "
          + (f"{limit} B/s ({round(limit / 1_000_000, 2)} MB/s)" if limit
             else "off"))
    for cat in categorize.CATEGORIES:
        v = cfg.get("defaults", {}).get(cat, "")
        print(f"  {cat:<10} {v if v else '-'}")
    return 0


# ── list / resume ────────────────────────────────────────────────────────
def _journal_path() -> pathlib.Path:
    return categorize.config_path().parent / "journal.json"


def _journal_record(out_path: pathlib.Path) -> None:
    """Remember where downloads live so list/pause/resume can find them.
    Accepts either the output file path or a state path directly (torrent
    state files are handed over as-is). Entries whose state file is gone
    (finished / cancelled downloads) are dropped on the way."""
    try:
        jp = _journal_path()
        jp.parent.mkdir(parents=True, exist_ok=True)
        entries: list[str] = []
        if jp.exists():
            try:
                loaded = json.loads(jp.read_text(encoding="utf-8"))
                entries = [str(e) for e in loaded] if isinstance(loaded, list) else []
            except ValueError:
                entries = []
        out_path = pathlib.Path(os.path.abspath(os.path.expanduser(str(out_path))))
        if str(out_path).endswith(STATE_SUFFIX):
            sp = str(out_path)
        else:
            sp = str(out_path.parent / (out_path.name + STATE_SUFFIX))
        kept = [e for e in entries if e != sp and os.path.exists(e)]
        kept.append(sp)
        categorize.atomic_write_text(jp, json.dumps(kept[-500:]))
    except OSError:
        pass


def _find_states() -> list[pathlib.Path]:
    cfg = _cfg()
    roots = {categorize.default_root(cfg)}
    for v in cfg.get("defaults", {}).values():
        roots.add(pathlib.Path(os.path.expanduser(v)))
    out: list[pathlib.Path] = []
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        for p in root.glob(f"**/*{STATE_SUFFIX}"):
            if p not in seen:
                seen.add(p)
                out.append(p)
    try:
        entries = json.loads(_journal_path().read_text(encoding="utf-8"))
    except (ValueError, OSError):
        entries = []
    for line in entries if isinstance(entries, list) else []:
        p = pathlib.Path(str(line))
        if p.exists() and p not in seen:
            seen.add(p)
            out.append(p)
    return sorted(out)


_PID_CACHE: dict[int, bool] = {}


def _check_pids(pids) -> None:
    """Fill _PID_CACHE: is each pid a live mgd process right now?

    "The pid exists" is not enough. After a crash or power loss the pid in a
    state file can belong to an unrelated program by the time anyone looks
    (the download would show as running forever and refuse to resume), and
    a process that has exited but was not reaped yet still answers signal 0.
    One `ps` call for all pids settles both."""
    todo = []
    for pid in pids:
        if pid in _PID_CACHE:
            continue
        if pid == os.getpid():
            _PID_CACHE[pid] = True
            continue
        try:
            os.kill(pid, 0)
        except PermissionError:
            pass                     # exists (someone else's): let ps decide
        except OSError:
            _PID_CACHE[pid] = False
            continue
        todo.append(pid)
    if not todo:
        return
    if os.name == "nt":
        _PID_CACHE.update({pid: True for pid in todo})
        return
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=,stat=,command=", "-p", ",".join(map(str, todo))],
            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        _PID_CACHE.update({pid: True for pid in todo})   # cannot tell: be careful
        return
    seen: dict[int, bool] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        command = parts[2] if len(parts) > 2 else ""
        seen[int(parts[0])] = not parts[1].startswith("Z") and "mgd" in command
    for pid in todo:
        _PID_CACHE[pid] = seen.get(pid, False)


def _state_pid(st: dict) -> int | None:
    try:
        pid = int(st.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _state_pid_alive(st: dict, fresh: bool = False) -> bool:
    """True while the mgd process that owns this download is running."""
    pid = _state_pid(st)
    if pid is None:
        return False
    if fresh:
        _PID_CACHE.pop(pid, None)
    _check_pids([pid])
    return _PID_CACHE.get(pid, False)


def _state_id(st_path: pathlib.Path) -> str:
    """Stable id of a download: derived from where its state file lives, so
    it never changes while other downloads start and finish around it
    (the 1-based numbers `mgd list` prints do). Always starts with a letter
    so it can never be mistaken for one of those numbers."""
    import hashlib
    full = os.path.abspath(str(st_path))
    return "d" + hashlib.sha1(full.encode("utf-8", "surrogatepass")).hexdigest()[:10]


def _state_done(st: dict) -> int:
    if st.get("kind") == TORRENT_STATE_KIND:
        return int(st.get("done") or 0)
    return sum(s.get("pos", 0) for s in st.get("segments", [])
               if s.get("pos", 0) > 0)


def _state_item(st_path: pathlib.Path, st: dict) -> dict:
    """One row of `mgd list --json` / the native host's `query`."""
    return {"id": _state_id(st_path),
            "url": st.get("source_url") or st.get("url"),
            "filename": st.get("filename"),
            "out_path": st.get("out_path"),
            "state_path": str(st_path),
            "size": st.get("size"),
            "done": _state_done(st),
            "kind": "torrent" if st.get("kind") == TORRENT_STATE_KIND else "file",
            "state": "running" if _state_pid_alive(st) else "paused"}


def _state_items(states: list[pathlib.Path]) -> list[dict]:
    """Rows for every readable state file (one process check for all)."""
    loaded = [(p, _read_state(p)) for p in states]
    loaded = [(p, st) for p, st in loaded if st is not None]
    _check_pids([pid for pid in (_state_pid(st) for _, st in loaded) if pid])
    return [_state_item(p, st) for p, st in loaded]


def _read_state(st_path: pathlib.Path) -> dict | None:
    try:
        st = json.loads(st_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return st if isinstance(st, dict) else None


def _resolve_target(target: str, states: list[pathlib.Path],
                    verb: str) -> pathlib.Path | None:
    """Which download `mgd pause|resume|cancel <target>` means.

    Accepts the stable id (`mgd list` shows it; GUIs and scripts should use
    it), the output/state file path, or the 1-based number from `mgd list`.
    Numbers shift whenever another download starts or finishes, so they are
    only safe for a human reading the list right now."""
    target = str(target).strip()
    for p in states:
        if _state_id(p) == target:
            return p
    if target.isdigit():
        idx = int(target)
        if not 1 <= idx <= len(states):
            print(f"error: id {idx} out of range (1..{len(states)})"
                  if states else f"error: no unfinished downloads to {verb}",
                  file=sys.stderr)
            return None
        return states[idx - 1]
    want = os.path.abspath(os.path.expanduser(target))
    for p in states:
        full = os.path.abspath(str(p))
        if want in (full, full[:-len(STATE_SUFFIX)]):
            return p
    print(f"error: no download matches {target!r} - {verb} takes an id or "
          "number from 'mgd list'", file=sys.stderr)
    return None


def cmd_list(args) -> int:
    states = _find_states()
    if getattr(args, "as_json", False):
        print(json.dumps({"items": _state_items(states)}))
        return 0
    if not states:
        print("no unfinished downloads")
        return 0
    for i, p in enumerate(states, 1):
        st = _read_state(p)
        if st is None:
            continue
        total = st.get("size")
        done = _state_done(st)
        pct = f"{done * 100 // total}%" if total else "?"
        status = "running" if _state_pid_alive(st) else "paused"
        print(f"  {i}. [{status}] {p.name[:-len(STATE_SUFFIX)]}  ({_state_id(p)})")
        print(f"     {st.get('source_url') or st.get('url')}")
        print(f"     {pct} ({done}/{total} bytes)"
              + (f" - pause ok: mgd pause {i} | resume: mgd resume {i} | cancel: mgd cancel {i}"
                 if status == "running" else
                 f" - resume: mgd resume {i} | delete: mgd cancel {i}"))
    return 0


def _kill_pid_of(st_path: pathlib.Path, st: dict) -> bool:
    """Return True if a running download process was signalled."""
    if not _state_pid_alive(st):
        return False
    try:
        os.kill(int(st["pid"]), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _wait_pid_gone(st: dict, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _state_pid_alive(st, fresh=True):
            return True
        time.sleep(0.1)
    return not _state_pid_alive(st, fresh=True)


def cmd_pause(args) -> int:
    st_path = _resolve_target(args.target, _find_states(), "pause")
    if st_path is None:
        return 2
    st = _read_state(st_path)
    if st is None:
        return 2
    name = st_path.name[:-len(STATE_SUFFIX)]
    sid = _state_id(st_path)
    if _kill_pid_of(st_path, st):
        # give the process a moment to flush its state
        _wait_pid_gone(st, 3.0)
        print(f"paused {name} - state saved, resume with: mgd resume {sid}")
        return 0
    print(f"{name} is not running (already paused) - resume with: mgd resume {sid}")
    return 0


def cmd_cancel(args) -> int:
    st_path = _resolve_target(args.target, _find_states(), "cancel")
    if st_path is None:
        return 2
    st = _read_state(st_path)
    if st is None:
        return 2
    if _kill_pid_of(st_path, st):
        # the dying process saves its state one last time: wait for it to be
        # gone before deleting, or that save would resurrect a state file
        # with no data behind it
        if not _wait_pid_gone(st, 5.0):
            try:
                os.kill(int(st["pid"]), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError, ValueError):
                pass
            _wait_pid_gone(st, 2.0)
    removed = []
    if st.get("kind") == TORRENT_STATE_KIND:
        # remove only the recorded partial files + aria2 control files
        d = pathlib.Path(st.get("dir") or st.get("out_path") or ".")
        for f in st.get("files", []):
            p = d / f.get("path", "")
            for cand in (p, pathlib.Path(str(p) + ".aria2")):
                try:
                    if cand.is_file():
                        cand.unlink()
                        removed.append(cand.name)
                except OSError:
                    pass
    base = str(st_path)[:-len(STATE_SUFFIX)]
    for suffix in (STATE_SUFFIX, PART_SUFFIX):
        part = pathlib.Path(base + suffix)
        if part.exists():
            try:
                part.unlink()
                removed.append(part.name)
            except OSError as err:
                print(f"error: cannot remove {part}: {err}", file=sys.stderr)
                return 1
    print(f"cancelled {pathlib.Path(base).name}"
          + (f" (removed {', '.join(removed)})" if removed else ""))
    return 0


def cmd_doctor(_args) -> int:
    """Show what engines and setup mgd can use right now."""
    cfg = _cfg()
    if getattr(_args, "as_json", False):
        print(json.dumps({
            "python": sys.version.split()[0],
            "config": str(categorize.config_path()),
            "root": str(categorize.default_root(cfg)),
            "tools": {name: shutil.which(binary) for name, binary in (
                ("yt-dlp", "yt-dlp"), ("ffmpeg", "ffmpeg"),
                ("aria2c", "aria2c"), ("webtorrent", "webtorrent"))},
            "unfinished": len(_find_states()),
        }))
        return 0
    ok, warn = "OK", "--"
    print("mgd doctor")
    print(f"  engine        {ok}  python {sys.version.split()[0]}, "
          f"config: {categorize.config_path()}")
    root = categorize.default_root(cfg)
    print(f"  download root {ok}  {root}")
    dl = shutil.which("yt-dlp")
    print(f"  yt-dlp        {ok if dl else warn}  {dl or 'install: brew install yt-dlp'}")
    if dl:
        imp = subprocess.run(["yt-dlp", "--list-impersonate-targets"],
                             capture_output=True, text=True, timeout=30)
        targets = len((imp.stdout or "").strip().splitlines()) - 3
        print(f"  impersonation {ok if targets > 0 else warn}  "
              f"{max(0, targets)} browser targets (Cloudflare-protected sites)")
    ff = shutil.which("ffmpeg")
    print(f"  ffmpeg        {ok if ff else warn}  {ff or 'install: brew install ffmpeg'}")
    a2 = shutil.which("aria2c")
    print(f"  aria2c        {ok if a2 else warn}  "
          + (f"{a2} (torrents & magnets)" if a2 else "install: brew install aria2 (torrent support)"))
    wt = shutil.which("webtorrent")
    print(f"  webtorrent    {ok if wt else warn}  "
          + (f"{wt} (torrent streaming)" if wt
             else "install: brew install webtorrent-cli (torrent streaming)"))
    print(f"  cookie source {ok if cfg.get('cookies_browser') else warn}  "
          + (f"preferred: {cfg['cookies_browser']} (set with: mgd set-default cookies-browser chrome)"
             if cfg.get("cookies_browser") else "none chosen; auto-detect on first platform download"))
    print(f"  unfinished    {len(_find_states())} download(s) (see: mgd list)")
    return 0


def _probe_size(url: str, referer: str | None, proxy: str | None,
                timeout: float = 10.0):
    """(size, accept_ranges) for one media url; (None, False) on failure."""
    from .engine import probe as engine_probe, DEFAULT_HEADERS
    headers = dict(DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        pr = engine_probe(url, proxy, timeout, headers=headers)
        return pr.size, pr.accept_ranges
    except Exception:
        return None, False


def cmd_page_media(args) -> int:
    """Everything a batch UI needs from a page, in one JSON document:

    {"url":..., "items":[{url,kind,title,quality,poster,size}], "error":?}

    Fetches the page, extracts the media catalog (KVS flashvars, <video>
    posters + sources, direct links), then probes every item's size in
    parallel (ranged GET, no body transfer)."""
    url = args.url
    cfg = _cfg()
    proxy = args.proxy or cfg.get("proxy") or os.environ.get("HTTPS_PROXY")
    headers = {"User-Agent": net.DEFAULT_UA}
    if args.user_agent:
        headers["User-Agent"] = args.user_agent
    out = {"url": url, "items": [], "error": None}
    _use_cookies(getattr(args, "cookies", None))
    try:
        html = _fetch_page(url, headers, proxy)
    except (HttpError, OSError) as err:
        out["error"] = f"{type(err).__name__}: {err}"
        print(json.dumps(out))
        return 1
    if html is None:
        # plain file, not a page: single item, itself
        out["items"] = [{"url": url, "kind": "direct", "title": "",
                         "quality": "", "poster": "", "size": None}]
        size, _ = _probe_size(url, None, proxy)
        out["items"][0]["size"] = size
        print(json.dumps(out))
        return 0
    catalog = grab.extract_media_catalog(html, url)
    if args.limit:
        catalog = catalog[:args.limit]
    if not args.no_size and catalog:
        # parallel size probing; referer makes tiered cdns (KVS/erome) answer
        results: dict[str, int | None] = {}
        lock = threading.Lock()

        def worker(item: dict) -> None:
            size, _ = _probe_size(item["url"], url, proxy)
            with lock:
                results[item["url"]] = size

        threads = [threading.Thread(target=worker, args=(it,), daemon=True)
                   for it in catalog]
        for t in threads[:8]:
            t.start()
            remaining = threads[8:]
        running = threads[:8]
        # simple sliding window of 8 workers
        pending = list(threads[8:])
        while True:
            running = [t for t in running if t.is_alive()]
            while len(running) < 8 and pending:
                t = pending.pop(0)
                t.start()
                running.append(t)
            if not running:
                break
            time.sleep(0.02)
        for it in catalog:
            it["size"] = results.get(it["url"])
    else:
        for it in catalog:
            it["size"] = None
    out["items"] = catalog
    print(json.dumps(out))
    return 0


def _handoff_path() -> pathlib.Path:
    return categorize.config_path().parent / "handoff.json"


def _write_handoff(urls: list[str], referer, destination, titles: dict,
                   kinds: dict | None = None, cookies: str | None = None) -> None:
    """Queue a batch for the MediaGrab app (it reads + deletes the file when
    `mediagrab://handoff` opens it).

    A batch that arrives before the app consumed the previous one is merged
    into it rather than replacing it - two quick right-click downloads must
    both survive. Each item keeps its own referer and suggested name;
    the top-level urls/referer/destination keys are what older app builds
    read."""
    path = _handoff_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    items: list[dict] = []
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        fresh = time.time() - path.stat().st_mtime < 120
        if fresh and isinstance(old, dict):
            for it in old.get("items") or [
                    {"url": x, "referer": old.get("referer")}
                    for x in old.get("urls") or []]:
                if isinstance(it, dict) and it.get("url"):
                    items.append(it)
    except (OSError, ValueError):
        pass
    known = {it["url"] for it in items}
    for u_ in urls:
        if u_ in known:
            continue
        known.add(u_)
        kind = (kinds or {}).get(u_)
        items.append({"url": u_, "referer": referer or None,
                      "name": _clean_out_name(titles.get(u_)) or None,
                      # "file" | "stream" | "page" (a page goes through the
                      # app's normal quality-picker flow)
                      "kind": kind if kind in ("file", "stream", "page") else None,
                      # path of a private cookies.txt, never the cookies
                      "cookies": cookies or None})
    handoff = {"urls": [it["url"] for it in items],
               "referer": referer or None,
               "destination": destination or None,
               "items": items}
    categorize.atomic_write_text(path, json.dumps(handoff))


def cmd_native_host(_args) -> int:
    """Chrome native-messaging host (stdio, 4-byte LE length + JSON).

    Messages (from the MediaGrab Chrome extension):
      {"command":"ping"}                                  -> {"ok":true,...}
      {"command":"probe","urls":[...],"referer":?}        -> {"sizes":{url:size}}
      {"command":"download","urls":[...],"destination":?} -> {"ok":...,"spawned":n}
    Downloads are spawned detached (one at a time, sequential queue) so the
    browser never waits on them."""
    import struct

    def read_message() -> dict | None:
        raw = sys.stdin.buffer.read(4)
        if len(raw) < 4:
            return None
        (length,) = struct.unpack("<I", raw)
        if not 0 < length <= 16 * 1024 * 1024:
            return None
        body = sys.stdin.buffer.read(length)
        if len(body) < length:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError:
            return None

    def write_message(obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        sys.stdout.buffer.write(struct.pack("<I", len(body)))
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.flush()

    mgd_bin = str(pathlib.Path(__file__).resolve().parent.parent / "bin" / "mgd")
    while True:
        msg = read_message()
        if msg is None:
            return 0
        command = msg.get("command")
        if command == "ping":
            write_message({"ok": True, "app": "mgd", "version": "1.3"})
            continue
        if command == "probe":
            urls = [str(x) for x in msg.get("urls", [])][:64]
            referer = msg.get("referer")
            sizes: dict[str, int | None] = {}
            net.set_cookie_jar(None)
            try:
                _use_cookies(net.write_cookie_file(msg.get("cookies") or [], _cookie_dir()))
            except OSError:
                pass
            for chunk_start in range(0, len(urls), 8):
                chunk = urls[chunk_start:chunk_start + 8]
                results: dict[str, int | None] = {}
                lock = threading.Lock()

                def worker(u_: str) -> None:
                    size, _ = _probe_size(u_, referer, None)
                    with lock:
                        results[u_] = size

                ts = [threading.Thread(target=worker, args=(u_,), daemon=True)
                      for u_ in chunk]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()
                sizes.update(results)
            net.set_cookie_jar(None)
            write_message({"ok": True, "sizes": sizes})
            continue
        if command == "download":
            urls = [str(x) for x in msg.get("urls", []) if x][:128]
            destination = msg.get("destination")
            referer = msg.get("referer")
            # optional {url: suggested file name} from the page scan
            raw_titles = msg.get("titles")
            titles = raw_titles if isinstance(raw_titles, dict) else {}
            raw_kinds = msg.get("kinds")
            kinds = raw_kinds if isinstance(raw_kinds, dict) else {}
            # the site login the extension read from the browser (only the
            # cookies the browser itself would send to these urls)
            cookies_path = None
            try:
                cookies_path = net.write_cookie_file(msg.get("cookies") or [], _cookie_dir())
            except OSError:
                cookies_path = None
            log_path = categorize.config_path().parent / "native-host.log"

            def spawn_all():
                """Headless fallback: detached mgd grab per url."""
                spawned = 0
                for u_ in urls:
                    cmd = [sys.executable, mgd_bin, "grab", u_]
                    if destination:
                        cmd += ["-d", str(destination)]
                    if referer:
                        # tiered CDNs (erome etc.) 403 media requests without
                        # the source page as Referer
                        cmd += ["--referer", str(referer)]
                    name = _clean_out_name(titles.get(u_))
                    if name:
                        cmd += ["--out-name", name]
                    if cookies_path:
                        cmd += ["--cookies", cookies_path]
                    try:
                        log_path.parent.mkdir(parents=True, exist_ok=True)
                        err_sink = open(log_path, "a", encoding="utf-8")
                        print(f"{time.strftime('%F %T')} grab spawn: {u_}",
                              file=err_sink)
                        err_sink.flush()
                        subprocess.Popen(
                            cmd, stdout=subprocess.DEVNULL, stderr=err_sink,
                            stdin=subprocess.DEVNULL, start_new_session=True)
                        err_sink.close()
                        spawned += 1
                    except OSError:
                        pass
                return spawned

            # FDM-style hand-off: instead of headless grabs, hand the batch
            # to the MediaGrab app, which opens the rename/destination sheet
            # and downloads after the user confirms (mgd set-default
            # open-app off keeps downloads headless).
            if urls and _cfg().get("open_app", "on") != "off":
                handed = False
                try:
                    _write_handoff(urls, referer, destination, titles, kinds,
                                   cookies_path)
                    if os.environ.get("MGD_NO_OPEN"):
                        write_message({"ok": True, "handoff": True,
                                       "spawned": len(urls)})
                        continue
                    subprocess.Popen(
                        ["/usr/bin/open", "mediagrab://handoff"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        stdin=subprocess.DEVNULL)
                    handed = True
                except OSError:
                    pass
                if handed:
                    write_message({"ok": True, "handoff": True,
                                   "spawned": len(urls)})
                    continue
            write_message({"ok": True, "spawned": spawn_all()})
            continue
        if command == "query":
            _PID_CACHE.clear()       # the host can outlive several queries
            write_message({"ok": True, "items": _state_items(_find_states())[:64]})
            continue
        if command == "open":
            try:
                subprocess.Popen(["/usr/bin/open", "-a", "MediaGrab"],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,
                                  stdin=subprocess.DEVNULL)
                write_message({"ok": True})
            except OSError as err:
                write_message({"ok": False, "error": str(err)})
            continue
        write_message({"ok": False, "error": f"unknown command {command!r}"})


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def _watch_extract_urls(text: str) -> list[str]:
    """URLs worth downloading from a piece of text (clipboard)."""
    out, seen = [], set()
    for m in _URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?")
        if u not in seen and len(u) > 12:
            seen.add(u)
            out.append(u)
    return out


def _watch_worth_grabbing(url: str, all_types: bool) -> bool:
    """Direct media files and platform pages are grabbed by default; with
    --all-types any http(s) URL is."""
    if all_types:
        return True
    from mgd.grab import VIDEO_EXTS, AUDIO_EXTS, DOWNLOADABLE_EXTS
    if any(p.search(url) for p in (VIDEO_EXTS, AUDIO_EXTS, DOWNLOADABLE_EXTS)):
        return True
    return bool(PLATFORM_RE.match(url) or X_RE.match(url))


def cmd_watch(args) -> int:
    """FDM-style clipboard monitor: watch the pasteboard and auto-download
    any link you copy (media files, platform URLs; --all-types for anything).

    macOS only (uses pbpaste). Ctrl+C to stop."""
    if sys.platform != "darwin":
        print("error: mgd watch currently supports macOS (pbpaste)",
              file=sys.stderr)
        return 2
    interval = max(0.5, args.interval)
    seen: set[str] = set()
    print(f"watching the clipboard every {interval}s — copy a link and it "
          "downloads. Ctrl+C to stop.")
    print(_arrow("press Cmd+C on any URL in any app"))
    while True:
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nbye")
            return 0
        try:
            out = subprocess.run(["pbpaste"], capture_output=True,
                                 timeout=5).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            continue
        for url in _watch_extract_urls(out):
            if url in seen or not _watch_worth_grabbing(url, args.all_types):
                continue
            seen.add(url)
            cmd = [sys.executable,
                   str(pathlib.Path(__file__).resolve().parent.parent
                       / "bin" / "mgd"), "grab", url]
            if args.dir:
                cmd += ["-d", args.dir]
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL,
                                 start_new_session=True)
                print(_arrow(f"clipboard -> downloading {url}"))
            except OSError as err:
                print(f"error: could not start download: {err}", file=sys.stderr)


def cmd_qualities(args) -> int:
    """Machine-readable quality menu for a URL (NDJSON-ish single JSON line).

    Source: 'xd' for X/Twitter posts (bridge resolution ladder), 'ytdlp' for
    platforms (yt-dlp -J), 'direct' when there is nothing to choose.
    Responds {"source":..., "title":..., "items":[{label, quality}], "error":?}"""
    url = args.url
    out = {"source": None, "title": None, "items": [], "error": None}
    cfg = _cfg()
    if X_RE.match(url):
        out["source"] = "xd"
        try:
            st = _xd_resolve(url, ".mp4")
        except (RuntimeError, OSError) as err:
            out["error"] = str(err)
            print(json.dumps(out))
            return 1
        out["title"] = st.get("title")
        out["items"] = _xd_quality_entries(st)
        print(json.dumps(out))
        return 0

    # Quality probing only makes sense for platform pages; probing a direct
    # file with yt-dlp -J can download the whole file during extraction.
    if not PLATFORM_RE.match(url):
        out["source"] = "direct"
        print(json.dumps(out))
        return 0

    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        out["error"] = "yt-dlp not installed (brew install yt-dlp)"
        print(json.dumps(out))
        return 1
    proxy_args = []
    envp = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") \
        or os.environ.get("https_proxy") or os.environ.get("http_proxy")
    if args.proxy:
        proxy_args = ["--proxy", args.proxy]
    elif envp:
        proxy_args = ["--proxy", envp]
    cookies = []
    if getattr(args, "cookies", None):
        cookies = ["--cookies", args.cookies]
    elif getattr(args, "cookies_from_browser", None):
        cookies = ["--cookies-from-browser", args.cookies_from_browser]
    else:
        preferred = cfg.get("cookies_browser")
        for b in [preferred, "chrome", "edge", "brave", "firefox", "safari"]:
            if b:
                cookies = ["--impersonate", "chrome",
                           "--cookies-from-browser", b]
                break
    strategies = [
        ("chrome UA", _ytdlp_base_args() + [
            "--user-agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"]),
        ("impersonate chrome", _ytdlp_base_args() + ["--impersonate", "chrome"]),
        ("cookies", _ytdlp_base_args() + cookies) if cookies else None,
    ]
    strategies = [s for s in strategies if s]
    info, probe_strategy = _probe_ytdlp_info(ytdlp, strategies, url,
                                             ["--no-playlist", *proxy_args])
    if info is None:
        out["error"] = ("yt-dlp could not read formats for this link "
                        "(bot check? open/update your preferred browser once, or pass --cookies)")
        print(json.dumps(out))
        return 1
    out["source"] = "ytdlp"
    out["title"] = info.get("title")
    out["items"] = _curate_qualities(info)
    print(json.dumps(out))
    return 0


def cmd_resume(args) -> int:
    states = _find_states()
    if not states:
        print("no unfinished downloads to resume")
        return 0
    if args.target == "all":
        chosen = states
    else:
        one = _resolve_target(args.target, states, "resume")
        if one is None:
            return 2
        chosen = [one]
    as_json = getattr(args, "json_progress", False)
    failures = 0
    for st_path in chosen:
        st = _read_state(st_path)
        if st is None:
            print(f"error: cannot read {st_path}", file=sys.stderr)
            failures += 1
            continue
        if _state_pid_alive(st):
            # two processes writing one part file would corrupt it
            msg = f"{st_path.name[:-len(STATE_SUFFIX)]} is already downloading"
            if as_json:
                print(json.dumps({"type": "error", "message": msg}), flush=True)
            else:
                print(f"error: {msg}", file=sys.stderr)
            failures += 1
            continue
        out_path = pathlib.Path(st.get("out_path") or "")
        if not out_path.name:
            out_path = st_path.parent / (st.get("filename") or "download")
        if st.get("kind") == TORRENT_STATE_KIND:
            code = _resume_torrent(st, st_path, as_json)
            if code == 130:
                return 130
            failures += 1 if code else 0
            continue
        # resume from the link the user asked for when we know it: signed
        # CDN urls (the redirect target stored as "url") expire
        resume_url = st.get("source_url") or st.get("url", "")
        net.set_cookie_jar(None)
        cookies_file = _use_cookies(st.get("cookies"))
        dl = Downloader(resume_url, out_path,
                        proxy=_cfg().get("proxy") or os.environ.get("HTTPS_PROXY"),
                        referer=st.get("referer"),
                        mirror_hosts=_mirror_hosts_for(st.get("url", "")),
                        cookies_file=cookies_file,
                        resume_state=st_path.read_bytes())
        dl.pid = os.getpid()
        if as_json:
            print(json.dumps(_start_event(resume_url, out_path)), flush=True)
        else:
            print(_arrow(f"resuming {out_path.name} ({st.get('size', '?')} bytes total)"))

        plain_progress = None if as_json else _make_progress()

        def _cb(done, total, _json=as_json, _plain=plain_progress):
            if _json:
                print(json.dumps({"type": "progress", "done": done,
                                  "total": total}), flush=True)
            else:
                _plain(done, total)

        try:
            result = dl.run(on_progress=_cb)
            if as_json:
                print(json.dumps({"type": "done", "path": str(result.path),
                                  "size": result.size}), flush=True)
            else:
                print()
                print(_ok(f"{result.path} {_size_str(result.size)}"
                          + (" [was resumed]" if result.resumed else "")))
        except KeyboardInterrupt:
            dl.abort()
            if as_json:
                print(json.dumps({"type": "paused"}), flush=True)
            else:
                print("\npaused again (state saved)")
            return 130
        except (DownloadError, HttpError, OSError) as err:
            if as_json:
                print(json.dumps({"type": "error", "message": str(err)}), flush=True)
            else:
                print(f"FAIL {st_path}: {err}", file=sys.stderr)
            failures += 1
    return 0 if not failures else 1


# ── main ─────────────────────────────────────────────────────────────────
def _sigterm_to_interrupt(signum, frame):
    raise KeyboardInterrupt()


def main(argv=None) -> int:
    try:
        signal.signal(signal.SIGTERM, _sigterm_to_interrupt)
    except (ValueError, OSError):
        pass
    args = build_parser().parse_args(argv)
    handlers = {
        "grab": cmd_grab, "get": cmd_grab, "dl": cmd_grab, "fetch": cmd_grab,
        "grab-page": cmd_grab_page, "links": cmd_grab_page, "scan": cmd_grab_page,
        "set-default": cmd_set_default,
        "defaults": cmd_defaults, "config": cmd_defaults,
        "list": cmd_list, "jobs": cmd_list, "ls": cmd_list,
        "resume": cmd_resume, "cont": cmd_resume,
        "pause": cmd_pause,
        "cancel": cmd_cancel, "rm": cmd_cancel, "del": cmd_cancel,
        "doctor": cmd_doctor,
        "qualities": cmd_qualities, "formats": cmd_qualities,
        "page-media": cmd_page_media, "catalog": cmd_page_media,
        "native-host": cmd_native_host,
        "watch": cmd_watch, "clipboard-watch": cmd_watch,
        "torrent-info": cmd_torrent_info, "torrent-files": cmd_torrent_info,
        "stream": cmd_stream,
    }
    try:
        return handlers[args.cmd](args)
    except HttpError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    import time
    raise SystemExit(main())