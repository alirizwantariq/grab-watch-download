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
import subprocess
import sys
import threading
import time
import urllib.parse as u

from . import categorize, grab, net
from .engine import (Downloader, DownloadError, HttpError, Interrupted,
                     Stopper, STATE_SUFFIX)

BANNER = "mgd - media grab & download (stdlib-only segmented downloader)"
DEFAULT_TIMEOUT = 30.0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="mgd", description=BANNER)
    ap.add_argument("-c", "--conns", type=int, default=None,
                    help="max parallel connections")
    ap.add_argument("-r", "--retries", type=int, default=None,
                    help="retry attempts per range")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("grab", help="download a URL (page URLs get media extraction)")
    g.add_argument("url")
    g.add_argument("-o", "--out", default=None,
                   help="exact destination file path (overrides category dirs)")
    g.add_argument("-d", "--dir", default=None,
                   help="destination directory (skips category mapping)")
    g.add_argument("-t", "--type", default=None, dest="filetype",
                   choices=list(categorize.CATEGORIES), help="force category")
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
    g.add_argument("--all", action="store_true", help="download all found media links (cap 20)")
    g.add_argument("--user-agent", default=None)
    g.add_argument("--header", action="append", default=[], metavar="KEY:VALUE",
                   help="extra request header (repeatable)")
    g.add_argument("--quiet", "-q", action="store_true", help="no progress line")

    gp = sub.add_parser("grab-page", help="list media links on an HTML page")
    gp.add_argument("url")
    gp.add_argument("--proxy", default=None)
    gp.add_argument("--json", action="store_true", dest="as_json")

    sp = sub.add_parser("set-default", help="set default dir for a category or root")
    sp.add_argument("category", choices=["root", *categorize.CATEGORIES])
    sp.add_argument("path")

    sub.add_parser("defaults", help="show defaults")

    sub.add_parser("list", help="list unfinished (resumable) downloads")
    rs = sub.add_parser("resume", help="resume a saved download")
    rs.add_argument("target", help="id from 'mgd list' or 'all'")
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

BOT_CHECK_MARKERS = ("sign in to confirm", "not a bot", "confirm you")


def _ytdlp_base_args() -> list[str]:
    return ["--no-playlist", "--no-warnings", "--extractor-retries", "5",
            "--socket-timeout", "30", "--no-check-certificates"]


def _run_ytdlp_strategy(ytdlp: str, name: str, cmd: list[str]) -> int:
    print(f"# yt-dlp strategy: {name}")
    try:
        return subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        print("\nstopped (yt-dlp partials resume on rerun)", file=sys.stderr)
        return 130


def _grab_platform(args, cfg: dict, url: str | None = None) -> int:
    """YouTube-style URLs: delegate to yt-dlp's extractors, trying a chain:
    plain -> impersonate -> cookies from installed browsers (as in the
    GrabNWatch web app's proven strategy runner)."""
    url = url or args.url
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        print("error: YouTube/platform URLs need yt-dlp "
              "(install: brew install yt-dlp  or  pip install yt-dlp)",
              file=sys.stderr)
        return 1
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
        category = args.filetype or "video"
        dest_args = ["-P", str(categorize.category_dir(cfg, category))]
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
        ("impersonate chrome", _ytdlp_base_args() + ["--impersonate", "chrome"]),
    ]
    if explicit_cookies:
        strategies.insert(0, ("caller cookies", _ytdlp_base_args() + explicit_cookies))
    else:
        for browser in ("chrome", "firefox", "safari", "brave", "edge"):
            strategies.append(
                (f"cookies-from-{browser}",
                 _ytdlp_base_args() + ["--impersonate", "chrome",
                                       "--cookies-from-browser", browser]))
    last_err = ""
    for name, base in strategies:
        cmd = [ytdlp, *base, *dest_args, *proxy_args, url]
        code = _run_ytdlp_strategy(ytdlp, name, cmd)
        if code == 0:
            return 0
        last_err = f"strategy {name} exited {code}"
    print(f"error: all yt-dlp strategies failed ({last_err}). "
          "For bot-checked videos, pass cookies once: "
          "mgd grab URL --cookies-from-browser chrome", file=sys.stderr)
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

    if PLATFORM_RE.match(args.url):
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
            links = links[:20]
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
        try:
            out_path = _destination(args, cfg, url_i)
            print(f"> {url_i}{title}")
            print(f"+ {out_path}")
        except (RuntimeError, OSError) as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        dl = Downloader(url_i, out_path, proxy=args.proxy, conns=conns,
                        retries=retries, stopper=stopper,
                        timeout=DEFAULT_TIMEOUT,
                        expected_sha256=args.sha256 if t["kind"] == "direct" else None)
        try:
            result = dl.run(on_progress=None if args.quiet else _make_progress())
            print()
            print(f"DONE {result.path} {_size_str(result.size)}"
                  + (" [resumed]" if result.resumed else ""))
            results.append(result)
        except KeyboardInterrupt:
            dl.abort()
            print("\nstopped by user - state saved; resume with: mgd resume",
                  file=sys.stderr)
            return 130
        except (DownloadError, HttpError, OSError) as err:
            print(f"FAIL {url_i}: {err}", file=sys.stderr)
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
        cfg["root"] = args.path
    else:
        cfg.setdefault("defaults", {})[args.category] = args.path
    categorize.save_config(cfg)
    print(f"saved: {args.category} -> {args.path}")
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
    return sorted(out)


def cmd_list(_args) -> int:
    states = _find_states()
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
        print(f"  {i}. {p.name[:-len(STATE_SUFFIX)]}")
        print(f"     {st.get('url')}")
        print(f"     {pct} done ({done}/{total} bytes) - resume with: mgd resume {i}")
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
        print(f"resuming {out_path.name} ...")
        try:
            result = dl.run(on_progress=_make_progress())
            print()
            print(f"DONE {result.path}")
        except KeyboardInterrupt:
            dl.abort()
            print("\npaused again (state saved)")
            return 130
        except (DownloadError, HttpError, OSError) as err:
            print(f"FAIL {st_path}: {err}", file=sys.stderr)
            failures += 1
    return 0 if not failures else 1


# ── main ─────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "grab": cmd_grab,
        "grab-page": cmd_grab_page,
        "set-default": cmd_set_default,
        "defaults": cmd_defaults,
        "list": cmd_list,
        "resume": cmd_resume,
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