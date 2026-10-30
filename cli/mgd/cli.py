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

from . import categorize, grab, net
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
    g.add_argument("-d", "--dir", default=None,
                   help="destination directory (skips category mapping)")
    g.add_argument("-t", "--type", default=None, dest="filetype",
                   choices=list(categorize.CATEGORIES), help="force category")
    g.add_argument("--quality", "-Q", default=None,
                   help="best|1080|720|480|360|audio (for YouTube/X; omit and "
                        "mgd asks interactively when run in a terminal)")
    g.add_argument("--proxy", default=None,
                   help="proxy URL: http://h:p, https://h:p or socks5://h:p")
    g.add_argument("--cookies", default=None, help="Netscape cookie file for yt-dlp")
    g.add_argument("--cookies-from-browser", default=None,
                   help="browser to pull cookies from for yt-dlp: chrome, firefox, edge, safari, brave")
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
    sp.add_argument("category", choices=["root", "cookies-browser", *categorize.CATEGORIES])
    sp.add_argument("value", help="path (or browser name for cookies-browser)")

    sub.add_parser("defaults", aliases=["config"], help="show defaults + config")

    lj = sub.add_parser("list", aliases=["jobs", "ls"],
                        help="list unfinished downloads with [running]/[paused] state")
    lj.add_argument("--json", action="store_true", dest="as_json", help="machine-readable list")
    rs = sub.add_parser("resume", aliases=["cont"],
                        help="resume a saved download")
    rs.add_argument("target", help="id from 'mgd list' or 'all'")
    ps = sub.add_parser("pause", help="pause a running download (by id from 'mgd list')")
    ps.add_argument("target")
    cx = sub.add_parser("cancel", aliases=["rm", "del"],
                        help="cancel a download and delete its partial data")
    cx.add_argument("target")
    sub.add_parser("doctor", help="show engines, config and readiness")
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

BOT_CHECK_MARKERS = ("sign in to confirm", "not a bot", "confirm you")


def _ytdlp_base_args() -> list[str]:
    return ["--no-warnings", "--extractor-retries", "5",
            "--socket-timeout", "30", "--no-check-certificates"]


def _run_ytdlp_strategy(ytdlp: str, name: str, cmd: list[str]) -> int:
    print(f"# yt-dlp strategy: {name}")
    try:
        return subprocess.run(cmd).returncode
    except KeyboardInterrupt:
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


def _grab_platform(args, cfg: dict, url: str | None = None) -> int:
    """YouTube/X/Twitter URLs: resolve via yt-dlp's upstream extractors with a
    strategy chain (plain -> impersonate -> browser cookies), then download
    with an interactive quality menu (or --quality)."""
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
    if args.out:
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
    for name, base in strategies:
        cmd = [ytdlp, *base, *base_flags, *dest_args, *proxy_args]
        if selector:
            cmd += ["-f", selector]
        cmd += [*quality_extra, url]
        code = _run_ytdlp_strategy(ytdlp, name, cmd)
        if code == 0:
            return 0
        last_err = f"strategy {name} exited {code}"
    hint = ""
    if getattr(args, "cookies_from_browser", None) is None:
        hint = ("\nTip: open this link once in your browser (solve any "
                "challenge/login), then rerun - or export cookies and use "
                "--cookies cookies.txt. Safari users: grant your terminal "
                "Full Disk Access (System Settings > Privacy & Security).")
    print(f"error: all yt-dlp strategies failed ({last_err}). {hint}",
          file=sys.stderr)
    return 1


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

    if _is_platform(args.url):
        return _grab_platform(args, cfg)

    try:
        page_links = _page_media(args.url, headers, args.proxy)
    except HttpError as err:
        if err.status in (403, 429) and shutil.which("yt-dlp"):
            print(f"# plain fetch blocked by {err.status} - delegating to yt-dlp")
            return _grab_platform(args, cfg, args.url)
        print(f"error: {err} - this host may block automated clients "
              "(try --proxy or a different network)", file=sys.stderr)
        return 1
    if page_links is None:
        targets = [{"url": args.url, "kind": "direct", "title": ""}]
    else:
        links = page_links
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
        if getattr(args, "json_progress", False):
            print(json.dumps({"type": "start", "url": url_i}), flush=True)
        try:
            out_path = _destination(args, cfg, url_i)
            print(_arrow(f"{url_i}{title}"))
            print(f"  saving to {out_path}")
        except (RuntimeError, OSError) as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        _journal_record(out_path)
        dl = Downloader(url_i, out_path, proxy=args.proxy, conns=conns,
                        retries=retries, stopper=stopper,
                        timeout=DEFAULT_TIMEOUT,
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
                print(json.dumps({"type": "done", "path": result.path,
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
                print(json.dumps({"type": "error", "message": str(err)},
                                 flush=True))
            else:
                print(_fail(f"{url_i}: {err}"), file=sys.stderr)
            failures += 1
    if results:
        print(f"{len(results)} downloaded, {failures} failed")
    return 1 if failures and not results else 0


def _page_media(url: str, headers: dict, proxy: str | None):
    """None if url is a plain file; [{kind,url,title}] if it is an HTML page."""
    conn, resp = net.open_request(url, proxy, "GET",
                                  headers=headers or None, timeout=DEFAULT_TIMEOUT)
    close = getattr(conn, "close", None)
    try:
        status = resp.status
        if status >= 400:
            raise HttpError(status)
        body_head = resp.read(4096)
        if not grab.is_html(resp.getheader("Content-Type"), body_head):
            return None
        html = (body_head + resp.read()).decode("utf-8", "replace")
        return grab.extract_links(html, url)
    finally:
        if callable(close):
            try:
                close()
            except OSError:
                pass


def _destination(args, cfg: dict, url: str) -> pathlib.Path:
    from .engine import _url_filename
    if args.out:
        p = pathlib.Path(os.path.expanduser(args.out))
        if p.exists() and p.is_dir():
            p = p / _url_filename(url)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    name = _url_filename(url)
    category = args.filetype or categorize.categorize(name, None)
    d = pathlib.Path(os.path.expanduser(args.dir)) if args.dir \
        else categorize.category_dir(cfg, category)
    d.mkdir(parents=True, exist_ok=True)
    return categorize.unique_path(d, name)


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
    else:
        cfg.setdefault("defaults", {})[args.category] = args.value
    categorize.save_config(cfg)
    if args.category != "cookies-browser":
        print(f"saved: {args.category} -> {args.value}")
    return 0


def cmd_defaults(_args) -> int:
    cfg = _cfg()
    root = categorize.default_root(cfg)
    print(f"root: {root}")
    for cat in categorize.CATEGORIES:
        v = cfg.get("defaults", {}).get(cat, "")
        print(f"  {cat:<10} {v if v else '-'}")
    return 0


# ── list / resume ────────────────────────────────────────────────────────
def _journal_path() -> pathlib.Path:
    return categorize.config_path().parent / "journal.json"


def _journal_record(out_path: pathlib.Path) -> None:
    """Remember where downloads live so list/pause/resume can find them."""
    try:
        jp = _journal_path()
        jp.parent.mkdir(parents=True, exist_ok=True)
        entries: list[str] = []
        if jp.exists():
            try:
                entries = json.loads(jp.read_text(encoding="utf-8"))
            except ValueError:
                entries = []
        sp = str(out_path.expanduser().parent / (out_path.name + STATE_SUFFIX))
        if sp not in entries:
            entries.append(sp)
            jp.write_text(json.dumps(entries[-500:]), encoding="utf-8")
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
    for line in entries:
        p = pathlib.Path(line)
        if p.exists() and p not in seen:
            seen.add(p)
            out.append(p)
    return sorted(out)


def _state_pid_alive(st: dict) -> bool:
    pid = st.get("pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError, TypeError, ValueError):
        return False


def cmd_list(args) -> int:
    states = _find_states()
    if getattr(args, "as_json", False):
        items = []
        for p in states:
            try:
                st = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            done = sum(s.get("pos", 0) for s in st.get("segments", [])
                       if s.get("pos", 0) != -2)
            items.append({"url": st.get("url"), "filename": st.get("filename"),
                          "out_path": st.get("out_path"), "size": st.get("size"),
                          "done": done, "state": "running" if _state_pid_alive(st) else "paused"})
        print(json.dumps({"items": items}))
        return 0
    if not states:
        print("no unfinished downloads")
        return 0
    for i, p in enumerate(states, 1):
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        segs = st.get("segments", [])
        total = st.get("size")
        done = sum(s.get("pos", 0) for s in segs if s.get("pos", 0) != -2)
        pct = f"{done * 100 // total}%" if total else "?"
        status = "running" if _state_pid_alive(st) else "paused"
        print(f"  {i}. [{status}] {p.name[:-len(STATE_SUFFIX)]}")
        print(f"     {st.get('url')}")
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


def cmd_pause(args) -> int:
    states = _find_states()
    try:
        idx = int(args.target)
    except ValueError:
        print("error: pause target must be an id from 'mgd list'", file=sys.stderr)
        return 2
    if not 1 <= idx <= len(states):
        print(f"error: id {idx} out of range (1..{len(states)})", file=sys.stderr)
        return 2
    st_path = states[idx - 1]
    try:
        st = json.loads(st_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return 2
    if _kill_pid_of(st_path, st):
        print(f"pause signal sent to download {idx} - state saved, resume with: mgd resume {idx}")
        # give the process a moment to flush its state
        for _ in range(20):
            time.sleep(0.1)
            if not _state_pid_alive(st):
                break
        return 0
    print(f"download {idx} is not running (already paused) - resume with: mgd resume {idx}")
    return 0


def cmd_cancel(args) -> int:
    states = _find_states()
    try:
        idx = int(args.target)
    except ValueError:
        print("error: cancel target must be an id from 'mgd list'", file=sys.stderr)
        return 2
    if not 1 <= idx <= len(states):
        print(f"error: id {idx} out of range (1..{len(states)})", file=sys.stderr)
        return 2
    st_path = states[idx - 1]
    try:
        st = json.loads(st_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return 2
    _kill_pid_of(st_path, st)
    time.sleep(0.4)
    removed = []
    for suffix in (STATE_SUFFIX, PART_SUFFIX):
        part = st_path.with_name(st_path.name.replace(STATE_SUFFIX, suffix))
        if part.exists():
            try:
                part.unlink()
                removed.append(part.name)
            except OSError as err:
                print(f"error: cannot remove {part}: {err}", file=sys.stderr)
                return 1
    print(f"cancelled download {idx}"
          + (f" (removed {', '.join(removed)})" if removed else ""))
    return 0


def cmd_doctor(_args) -> int:
    """Show what engines and setup mgd can use right now."""
    cfg = _cfg()
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
    print(f"  ffmpeg        {ok if ff else warn}  {ff or 'needed for MP3 conversion (' if True else ''}"
          + ("" if ff else "brew install ffmpeg)"))
    print(f"  cookie source {ok if cfg.get('cookies_browser') else warn}  "
          + (f"preferred: {cfg['cookies_browser']} (set with: mgd set-default cookies-browser chrome)"
             if cfg.get("cookies_browser") else "none chosen; auto-detect on first platform download"))
    paused = len(_find_states())
    print(f"  paused items  {paused}")
    return 0


def cmd_resume(args) -> int:
    states = _find_states()
    if not states:
        print("no unfinished downloads to resume")
        return 0
    if args.target == "all":
        chosen = states
    else:
        try:
            idx = int(args.target)
        except ValueError:
            print("error: resume target must be an id or 'all'", file=sys.stderr)
            return 2
        if not 1 <= idx <= len(states):
            print(f"error: id {idx} out of range (1..{len(states)})", file=sys.stderr)
            return 2
        chosen = [states[idx - 1]]
    failures = 0
    for st_path in chosen:
        try:
            st = json.loads(st_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as err:
            print(f"error: cannot read {st_path}: {err}", file=sys.stderr)
            failures += 1
            continue
        out_path = pathlib.Path(st.get("out_path") or "")
        if not out_path.name:
            out_path = st_path.parent / (st.get("filename") or "download")
        dl = Downloader(st.get("url", ""), out_path,
                        proxy=_cfg().get("proxy") or os.environ.get("HTTPS_PROXY"),
                        resume_state=st_path.read_bytes())
        dl.pid = os.getpid()
        print(_arrow(f"resuming {out_path.name} ({st.get('size', '?')} bytes total)"))
        try:
            result = dl.run(on_progress=_make_progress())
            print()
            print(_ok(f"{result.path} {_size_str(result.size)}"
                      + (" [was resumed]" if result.resumed else "")))
        except KeyboardInterrupt:
            dl.abort()
            print("\npaused again (state saved)")
            return 130
        except (DownloadError, HttpError, OSError) as err:
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