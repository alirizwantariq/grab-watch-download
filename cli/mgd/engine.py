"""Segmented, resumable download engine.

Strategy notes, drawn from the two reference implementations of "optimal"
downloading (researched Nov 2026):

aria2 1.37.0 (shipped binary analyzed via REA, arm64 Mach-O
sha256 503c7b58b64e76af21ac9f0f96cca8749617c25f997739b8346cdf7c47fd99e3):
  - "--min-split-size=SIZE: aria2 does not split less than 2*SIZE byte
    range" (default 20M) - parallel connections only pay off for big files;
    we use the same rule with MIN_BYTES_PER_CONN (one conn per ~8 MiB)
  - --max-connection-per-server is capped at 16 (our MAX_CONNS)
  - per-download retry budget with retry-wait backoff (our retries + backoff)

IDM (internetdownloadmanager.com, "Dynamic segmentation"):
  - "segments downloaded files dynamically during download process and
    reuses available connections" - unclaimed ranges are split in half when
    a new connection claims work (our _claim), and idle workers steal the
    tail half of the largest in-flight range (our tail-stealing), which is
    what keeps one slow connection from stalling the last 1% of a file
  - file positions are saved repeatedly so interruptions resume exactly

On top of that: per-range retry with exponential backoff and jitter,
probe retry for transient TLS/connection failures (BAD_SIGNATURE etc.),
ETag/If-Range staleness validation, size verification, optional SHA-256
verification, and proxy support (HTTP CONNECT, TLS-wrapped proxy, SOCKS5).
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import random
import re
import threading
import time
import urllib.parse as u

from . import categorize, net
from .net import HttpError

CHUNK = 256 * 1024
STATE_SUFFIX = ".mgdstate"
PART_SUFFIX = ".mgdpart"
SPLIT_THRESHOLD = 4 * 1024 * 1024  # ranges bigger than this get split when claimed again
STEAL_THRESHOLD = 4 * 1024 * 1024  # in-flight tails bigger than 2x this get stolen
MIN_BYTES_PER_CONN = 8 * 1024 * 1024  # aria2's "2 * min-split-size" rule per connection
MAX_CONNS = 16                       # aria2's --max-connection-per-server cap
STATE_SAVE_INTERVAL = 0.5
RETRY_BACKOFF_BASE = 0.5
RETRY_BACKOFF_CAP = 30.0


def _backoff_scale() -> float:
    """MGD_BACKOFF_SCALE shrinks retry waits (automated tests simulate a
    dropped connection without waiting a minute and a half)."""
    try:
        return max(0.0, float(os.environ.get("MGD_BACKOFF_SCALE", "1")))
    except ValueError:
        return 1.0
DEFAULT_HEADERS = {"Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


def parse_speed(value) -> int | None:
    """'2MB' / '512KB' / '500000' -> bytes per second (None when unset).
    Raises ValueError on garbage."""
    if value in (None, "", "off", "none", "0"):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().upper().replace(" ", "")
    m = re.match(r"^([\d.]+)\s*([KMGT])?(?:I?B)?(?:/S)?$", text)
    if not m:
        raise ValueError(f"bad speed limit: {value!r} (use e.g. 2MB, 512KB, off)")
    num = float(m.group(1))
    mult = {"": 1, "K": 1000, "M": 1_000_000, "G": 1_000_000_000,
            "T": 1_000_000_000_000}[m.group(2) or ""]
    if num <= 0:
        return None
    return int(num * mult)


def _journal_dirs() -> list[pathlib.Path]:
    """Dirs where download state files live (mirror of cli._find_states)."""
    try:
        cfg = categorize.load_config()
    except Exception:
        cfg = {}
    dirs = [categorize.default_root(cfg)]
    for v in (cfg.get("defaults") or {}).values():
        if v:
            dirs.append(pathlib.Path(os.path.expanduser(str(v))))
    try:
        base = os.environ.get("XDG_CONFIG_HOME") or str(pathlib.Path.home() / ".config")
        journal = pathlib.Path(base) / "mgd" / "journal.json"
        entries = json.loads(journal.read_text(encoding="utf-8"))
        for line in entries:
            p = pathlib.Path(str(line))
            if p.exists():
                dirs.append(p.parent)
    except (ValueError, OSError):
        pass
    return dirs


def _count_active_downloads(include_self: bool = True) -> int:
    """Live mgd downloads (state files whose pid is alive), including self
    (self has usually written its state by the time throttling matters).
    include_self=False counts the others only (0 when none)."""
    count = 0
    pids = set()
    for d in _journal_dirs():
        if not d.exists():
            continue
        for st_path in d.glob(f"**/*{STATE_SUFFIX}"):
            try:
                st = json.loads(st_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            pid = st.get("pid")
            if not pid:
                continue
            pid = int(pid)
            if pid in pids:
                continue
            if not include_self and pid == os.getpid():
                continue
            try:
                os.kill(pid, 0)
                pids.add(pid)
            except (ProcessLookupError, PermissionError, TypeError, ValueError):
                continue
    return max(1, len(pids)) if include_self else len(pids)


class _SpeedGovernor:
    """Token-bucket throttle shared by all worker threads of a download.

    The effective rate is min(own cap, global cap / active downloads):
    - own cap: Downloader(max_speed=…) or the grab --max-speed flag
    - global cap: config 'speed_limit' (mgd set-default speed-limit 2MB),
      shared across every running mgd process - each divides it by the
      number of active downloads, approximating an FDM-style global
      traffic limit without inter-process coordination.
    Re-checked every 5 seconds so limits apply mid-download."""

    RECHECK = 5.0

    def __init__(self, own_limit: int | None):
        self.own_limit = own_limit
        self._effective: int | None = None
        self._checked_at = 0.0
        self._lock = threading.Lock()
        self._tokens = 0.0
        self._last = time.monotonic()

    def refresh(self) -> None:
        global_limit = None
        try:
            cfg = categorize.load_config()
            global_limit = parse_speed(cfg.get("speed_limit"))
        except Exception:
            global_limit = None
        eff = None
        if self.own_limit:
            eff = self.own_limit
        if global_limit:
            share = global_limit / max(1, _count_active_downloads())
            eff = share if eff is None else min(eff, share)
        self._effective = eff

    def wait(self, nbytes: int) -> None:
        """Block until `nbytes` may be transferred under the current rate."""
        now = time.monotonic()
        if now - self._checked_at > self.RECHECK:
            self._checked_at = now
            self.refresh()
        limit = self._effective
        if not limit or limit <= 0:
            return
        with self._lock:
            self._tokens = min(self._tokens + (now - self._last) * limit,
                               limit * 2)   # allow a 2s burst
            self._last = now
            if self._tokens < nbytes:
                deficit = nbytes - self._tokens
                self._tokens = 0.0
                time.sleep(min(deficit / limit, 10.0))   # cap a single sleep
                self._last = time.monotonic()
                return
            self._tokens -= nbytes


class DownloadError(Exception):
    pass


class FileChangedError(DownloadError):
    """The remote file changed since the partial download began."""


class Interrupted(Exception):
    """Raised to unwind everything when the user stops the download."""


class Stopper:
    def __init__(self):
        self._event = threading.Event()

    def stop(self):
        self._event.set()

    @property
    def stopped(self) -> bool:
        return self._event.is_set()

    def check(self):
        if self.stopped:
            raise Interrupted()


class ProbeResult:
    def __init__(self):
        self.url = ""
        self.size: int | None = None
        self.accept_ranges = False
        self.etag: str | None = None
        self.last_modified: str | None = None
        self.filename: str | None = None
        self.content_type: str | None = None


def probe(url: str, proxy: str | None = None,
          timeout: float = net.DEFAULT_TIMEOUT,
          headers: dict | None = None) -> ProbeResult:
    """Ranged GET (bytes=0-0): learns size/range support without the body."""
    r = ProbeResult()
    r.url = url
    conn, resp = net.open_request(url, proxy, "GET",
                                  headers=headers or dict(DEFAULT_HEADERS),
                                  timeout=timeout, range_start=0, range_end=0)
    try:
        status = resp.status
        if status == 403:
            raise HttpError(403, "forbidden (a bot-protection layer may block automated clients)")
        if status >= 400:
            raise HttpError(status)
        if status == 206:
            cr = resp.getheader("Content-Range") or ""
            if "/" in cr:
                total = cr.rsplit("/", 1)[1].strip()
                r.size = int(total) if total.isdigit() else None
            r.accept_ranges = True
        elif status == 200:
            cl = resp.getheader("Content-Length")
            r.size = int(cl) if cl and cl.isdigit() else None
        r.etag = resp.getheader("ETag")
        r.last_modified = resp.getheader("Last-Modified")
        r.final_url = getattr(resp, "final_url", None) or url
        r.filename = _disposition_filename(resp.getheader("Content-Disposition")) \
            or _url_filename(r.final_url)
        r.content_type = resp.getheader("Content-Type")
        resp.read(2)
        return r
    except HttpError as exc:
        # a 5xx on the redirect target carries the CDN url the redirect
        # chain landed on - the retry loop uses it for mirror probing
        exc.final_url = getattr(resp, "final_url", None)
        raise
    finally:
        _close_quietly(conn)


def _disposition_filename(disposition: str | None) -> str | None:
    if not disposition:
        return None
    import re
    import urllib.parse as u
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", disposition)
    if m:
        return u.unquote(m.group(1))
    m = re.search(r'filename="?([^";]+)"?', disposition)
    return m.group(1).strip() if m else None


def _url_filename(url: str) -> str:
    import re
    import urllib.parse as u
    p = u.unquote(u.urlsplit(url).path).rstrip("/")
    name = p.split("/")[-1] or "download"
    bad = '<>:"\\|?*' + "".join(chr(c) for c in range(32))
    name = "".join("-" if ch in bad else ch for ch in name).strip(".") or "download"
    return name[:180]


class Segment:
    __slots__ = ("start", "end", "pos", "owner")

    def __init__(self, start: int, end: int, pos: int = 0):
        self.start, self.end, self.pos = start, end, pos
        self.owner: str | None = None

    def span(self) -> int:
        return self.end - self.start + 1 if self.end >= 0 else 1

    def done(self) -> bool:
        if self.end < 0:
            return self.pos == -2
        return self.pos >= self.span()

    def to_json(self) -> dict:
        return {"start": self.start, "end": self.end, "pos": self.pos}


class DownloadResult:
    def __init__(self):
        self.path = ""
        self.size: int | None = None
        self.sha256: str | None = None
        self.resumed = False
        self.streamed = False
        self.url = ""


class Downloader:
    """Download one URL: dynamic segmentation, per-range retries, resume."""

    def __init__(self, url: str, out_path, proxy: str | None = None,
                 conns: int = 6, retries: int = 8, stopper: Stopper | None = None,
                 expected_sha256: str | None = None, resume_state: bytes | None = None,
                 timeout: float = net.DEFAULT_TIMEOUT,
                 referer: str | None = None,
                 mirror_hosts: list[str] | None = None,
                 max_speed: int | None = None,
                 cookies_file: str | None = None,
                 relinked: bool = False):
        self.url = url
        # the link the user asked for; self.url moves to the redirect target
        # once probed (signed CDN urls differ per visit and expire)
        self.source_url = url
        self.out_path = pathlib.Path(out_path).expanduser()
        self.proxy = proxy
        self.conns = max(1, min(MAX_CONNS, int(conns)))
        self.retries = max(1, int(retries))
        self.stopper = stopper or Stopper()
        self.expected_sha256 = expected_sha256
        self.timeout = timeout
        self._resume_state: bytes | None = resume_state
        # pages that hand out expiring/tiered media links (KVS sites) route
        # by Referer; without it the CDN can answer 502 or a bot-check page
        self.referer = referer
        # backup cdn mirror hostnames for the site (see _probe_with_retries)
        self.mirror_hosts = list(mirror_hosts or [])
        # the site login (cookies.txt) this download was started with; the
        # requests themselves pick cookies from net's jar
        self.cookies_file = cookies_file
        # resuming through a different link to the same file (an expired
        # signed url replaced by a fresh one): the caller has already checked
        # size and content, so the saved positions apply to the new url
        self.relinked = relinked
        # traffic shaping (mgd set-default speed-limit / --max-speed)
        self._governor = _SpeedGovernor(own_limit=max_speed)

        self._seg_lock = threading.Lock()
        self._segments: list[Segment] = []
        self._file = None
        self._io_lock = threading.Lock()
        self._done_bytes = 0
        self._last_save = 0.0
        self._restarts = 0
        self._error: Exception | None = None
        self._on_progress = None
        self._last_notify = 0.0

        self._filename = ""
        self._etag: str | None = None
        self._last_modified: str | None = None
        self._accept_ranges = False
        self._size: int | None = None
        self.resumed = False

    # ── public ────────────────────────────────────────────────────────────
    def run(self, on_progress=None) -> DownloadResult:
        self._on_progress = on_progress
        self._last_notify = 0.0
        while True:
            pr = self._probe_with_retries()
            if getattr(pr, "final_url", None) and pr.final_url != pr.url:
                self.url = pr.final_url
                pr.url = pr.final_url
            self._init_for(pr)
            if self._resume_state is not None:
                state = self._parse_resume(self._resume_state, pr)
                if state is not None:
                    self._start_from_state(state)
                self._resume_state = None  # valid only for the first run
            try:
                self._execute()
                break
            except FileChangedError:
                self._restarts += 1
                if self._restarts > 2:
                    raise DownloadError("remote file kept changing during the download") from None
            except Interrupted:
                self.flush()
                self.save_state()
                self._close_part()
                raise
            except (DownloadError, HttpError, OSError):
                # keep exact positions for a later resume: the periodic save
                # can be up to half a second (megabytes) behind
                self.save_state()
                self._close_part()
                raise
        return self._finalize()

    # ── headers ───────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        """Base headers + the source page as Referer when this media link
        was scraped from a page (tiered CDNs route by Referer)."""
        h = dict(DEFAULT_HEADERS)
        if self.referer:
            h["Referer"] = self.referer
        return h

    def _probe_with_retries(self) -> ProbeResult:
        """Probe with bounded retries and backoff: TLS handshake hiccups
        (BAD_SIGNATURE), connection resets and timeouts are transient and
        must not kill the download. 4xx answers are real and fail fast.

        Site resilience: when probes keep hitting 5xx on the redirect
        target, erome-style CDN edges (cdn77) can be down on one mirror
        hostname while siblings still serve. Each 5xx wave rotates
        self.url over the remaining mirror hosts - run()'s post-probe
        step keeps whoever succeeded."""
        attempt = 0
        streak_5xx = 0
        cdn_url: str | None = None
        probed_mirrors: set[str] = {u.urlsplit(self.url).netloc}
        while True:
            attempt += 1
            self.stopper.check()
            try:
                return probe(self.url, self.proxy, self.timeout,
                             headers=self._headers())
            except Interrupted:
                raise
            except HttpError as exc:
                if 400 <= exc.status < 500 and exc.status not in RETRYABLE_STATUS:
                    raise
                cdn_url = getattr(exc, "final_url", None) or cdn_url
                streak_5xx += 1
                if attempt >= self.retries:
                    raise DownloadError(
                        f"server unreachable after {attempt} probes: {exc}") from exc
                self._maybe_rotate_mirror(cdn_url, streak_5xx, probed_mirrors)
                self._backoff_sleep(attempt)
            except (TimeoutError, OSError) as exc:
                if attempt >= self.retries:
                    raise DownloadError(
                        f"could not reach the server after {attempt} probes: "
                        f"{type(exc).__name__}: {exc}") from exc
                self._backoff_sleep(attempt)

    def _maybe_rotate_mirror(self, cdn_url: str | None,
                             streak: int,
                             probed: set[str]) -> None:
        """After several consecutive 5xx probes on one cdn mirror, move the
        probe target to another mirror host for the site (self.mirror_hosts).
        Pure no-op when the caller did not register mirrors or there is
        nothing new to try."""
        if not self.mirror_hosts or not cdn_url or streak < 3:
            return
        p = u.urlsplit(cdn_url)
        if not p.netloc or p.netloc not in probed:
            if p.netloc:
                probed.add(p.netloc)
        for cand in self.mirror_hosts:
            if cand in probed:
                continue
            probed.add(cand)
            self.url = p._replace(netloc=cand).geturl()
            break

    # ── init / resume ─────────────────────────────────────────────────────
    def _init_for(self, pr: ProbeResult) -> None:
        self._filename = pr.filename
        self._etag = pr.etag
        self._last_modified = pr.last_modified
        self._accept_ranges = bool(pr.accept_ranges and pr.size is not None)
        self._size = pr.size
        self._close_part()
        self._segments = []
        self._done_bytes = 0
        self._error = None

    def _parse_resume(self, state_bytes: bytes, pr: ProbeResult) -> dict | None:
        try:
            state = json.loads(state_bytes)
        except ValueError:
            return None
        if state.get("kind") != "mgd-download-state":
            return None
        if self.relinked:
            # a fresh link to the same file: identity was verified by size +
            # content sample before getting here; another CDN node may well
            # report a different etag / last-modified for the same bytes
            if pr.size is None or state.get("size") != pr.size:
                return None
            if not isinstance(state.get("segments"), list):
                return None
            return state
        # same file when either the final url matches, or the link the user
        # originally asked for does (it may redirect to a fresh signed url on
        # every visit - etag/last-modified/size below still guard identity)
        same_final = state.get("url") == self.url
        same_source = bool(state.get("source_url")) and \
            state.get("source_url") == self.source_url
        if not (same_final or same_source):
            return None
        if pr.etag and state.get("etag") and pr.etag != state["etag"]:
            return None
        if pr.last_modified and state.get("last_modified") \
                and pr.last_modified != state["last_modified"]:
            return None
        if pr.size is not None and state.get("size") not in (None, pr.size):
            return None
        if state.get("accept_ranges") and not self._accept_ranges:
            return None
        if not isinstance(state.get("segments"), list):
            return None
        return state

    def _start_from_state(self, state: dict) -> None:
        part = self._part_path()
        if not part.exists():
            return
        self.resumed = True
        self._file = open(part, "r+b")
        if self._size:
            self._file.seek(0, 2)
            if self._file.tell() != self._size:
                self._file.truncate(self._size)
        for s in state["segments"]:
            if s.get("pos") == -2:
                continue
            seg = Segment(s["start"], s["end"], s["pos"])
            self._segments.append(seg)
            self._done_bytes += s["pos"]

    def _start_fresh(self, pr: ProbeResult) -> None:
        part = self._part_path()
        part.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(part, "wb")
        if pr.size:
            self._segments = [Segment(0, pr.size - 1)]
            self._file.truncate(pr.size)
        elif pr.size == 0:
            self._segments = []
        else:
            self._segments = [Segment(0, -1)]

    # ── execution ─────────────────────────────────────────────────────────
    def _execute(self) -> None:
        if self._file is None:
            self.resumed = False
            self._start_fresh(self._current_probe())
        streaming = self._streaming_mode()
        pool = 1 if streaming else self.effective_conns(self._size, self.conns)
        workers = [threading.Thread(target=self._worker, daemon=True)
                   for _ in range(pool)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        if self._error is not None:
            self._close_part()
            err = self._error
            self._error = None
            raise err

    @staticmethod
    def effective_conns(size: int | None, conns: int) -> int:
        """aria2's '2 * min-split-size' rule: only pay for parallel
        connections when the file is big enough for each connection to
        matter. One connection per MIN_BYTES_PER_CONN of file."""
        if not size:
            return conns
        return max(1, min(conns, size // MIN_BYTES_PER_CONN))

    def _streaming_mode(self) -> bool:
        return not self._accept_ranges

    # probe info is set before _execute by run(); keep reference for re-fresh
    def _current_probe(self):
        pr = ProbeResult()
        pr.url = self.url
        pr.size = self._size
        pr.accept_ranges = self._accept_ranges
        pr.etag = self._etag
        pr.last_modified = self._last_modified
        pr.filename = self._filename
        return pr

    # ── workers ───────────────────────────────────────────────────────────
    def _worker(self) -> None:
        wid = f"w{threading.get_ident():x}"
        conn = None
        try:
            while not self.stopper.stopped:
                seg = self._claim(wid)
                if seg is None:
                    return
                self._download_segment(seg)
        except Interrupted:
            self._record_error(Interrupted())
        except Exception as exc:  # noqa: BLE001
            self._record_error(exc)
            self.stopper.stop()
        finally:
            _close_quietly(conn)

    def _record_error(self, exc: Exception) -> None:
        if self._error is None:
            self._error = exc

    def _claim(self, wid: str) -> Segment | None:
        with self._seg_lock:
            unclaimed = [s for s in self._segments if s.owner is None and not s.done()]
            if unclaimed:
                largest = max(unclaimed, key=lambda s: s.span() if s.end >= 0 else 1)
                # IDM-style dynamic segmentation: a fresh claimer splits a
                # large unclaimed range in half and takes the second half.
                if largest.end >= 0 and largest.span() > SPLIT_THRESHOLD:
                    mid = largest.start + (largest.span() // 2) - 1
                    right = Segment(mid + 1, largest.end)
                    largest.end = mid
                    largest.owner = None
                    right.owner = wid
                    self._segments.append(right)  # track for state/resume
                    return right
                largest.owner = wid
                return largest
            # everything is claimed: IDM-style work stealing. Take the tail
            # half of the largest in-flight range so a straggler connection
            # cannot leave the download stuck at 99%. _stream's per-chunk
            # boundary re-check keeps the original owner inside its range.
            inflight = [s for s in self._segments
                        if s.owner is not None and not s.done() and s.end >= 0]
            if not inflight:
                return None
            victim = max(inflight, key=lambda s: s.span() - s.pos)
            remaining = victim.span() - victim.pos
            if remaining < 2 * STEAL_THRESHOLD:
                return None  # too small to be worth stealing; let it finish
            new_end = victim.start + victim.pos + remaining // 2 - 1
            right = Segment(new_end + 1, victim.end)
            victim.end = new_end
            right.owner = wid
            self._segments.append(right)
            return right

    def _download_segment(self, seg: Segment) -> None:
        """Stream the claimed range to completion with bounded retries."""
        attempt = 0
        conn = None
        while True:
            self.stopper.check()
            attempt += 1
            if attempt > self.retries:
                raise DownloadError(
                    f"range {seg.start}..{seg.end} failed after {attempt} attempts")
            _close_quietly(conn)
            conn = None
            try:
                if seg.end >= 0:
                    start = seg.start + seg.pos
                    end = seg.end
                elif seg.pos > 0:
                    start, end = seg.pos, None  # stream resume
                else:
                    start, end = None, None  # full body stream
                conn, resp = net.open_request(
                    self.url, self.proxy, "GET",
                    headers=self._headers(), timeout=self.timeout,
                    range_start=start, range_end=end)
                status = resp.status
                if status >= 400:
                    raise HttpError(status)
                if seg.end >= 0 and seg.pos > 0 and status != 206 and self._etag:
                    # ranged resume produced a whole-body 200: file changed or
                    # the server invalidates resumes across connections
                    raise FileChangedError("ranged resume returned the whole file")
                cap = None if seg.end < 0 else (seg.span() - seg.pos)
                self._stream(seg, resp, cap)
                if seg.end >= 0 and not seg.done():
                    raise DownloadError(
                        f"connection ended early ({seg.pos}/{seg.span()} bytes of range)")
                if seg.end < 0:
                    seg.pos = -2
                _close_quietly(conn)
                return
            except Interrupted:
                _close_quietly(conn)
                raise
            except FileChangedError:
                _close_quietly(conn)
                raise  # triggers global restart
            except (HttpError, DownloadError, OSError) as exc:
                _close_quietly(conn)
                conn = None
                if isinstance(exc, HttpError) and 400 <= exc.status < 500 \
                        and exc.status not in RETRYABLE_STATUS:
                    self._record_error(exc)
                    raise DownloadError(str(exc)) from exc
                self._backoff_sleep(attempt)

    def _stream(self, seg: Segment, resp, cap: int | None) -> None:
        """cap: maximum bytes this response may contribute (span - pos at start).
        Uses read1 so slow/drip servers don't block past a chunk boundary -
        keeps pause and periodic state saves responsive. Re-checks seg.end
        every chunk: a tail-stealing worker may shrink this segment's range
        mid-response (IDM-style), and this owner must stop at the new edge
        without overwriting the stolen bytes."""
        left = cap
        while True:
            self.stopper.check()
            try:
                chunk = resp.read1(CHUNK)  # type: ignore[attr-defined]
            except AttributeError:
                chunk = resp.read(CHUNK)
            if not chunk:
                return
            if left is not None:
                if left <= 0:
                    return
                if len(chunk) > left:
                    chunk = chunk[:left]
                left -= len(chunk)
            if seg.end >= 0:
                # boundary may have shrunk (tail stolen) while streaming
                room = seg.span() - seg.pos
                if room <= 0:
                    return
                if len(chunk) > room:
                    chunk = chunk[:room]  # rest of this response belongs to the stealer
            self._write(seg, chunk)
            self._governor.wait(len(chunk))   # traffic shaping
            if seg.end >= 0 and seg.pos >= seg.span():
                return
            if self._save_due():
                self.flush()
                self.save_state()

    def _write(self, seg: Segment, data: bytes) -> None:
        with self._io_lock:
            if self._file is None or self._file.closed:
                raise DownloadError("part file already closed")
            self._file.seek(seg.start + seg.pos)
            self._file.write(data)
            seg.pos += len(data)
            self._done_bytes += len(data)
            cb = self._on_progress
            if cb:
                now = time.monotonic()
                if now - self._last_notify >= 0.15:
                    self._last_notify = now
                    try:
                        cb(self._done_bytes, self._size)
                    except Exception:  # noqa: BLE001
                        pass

    def _backoff_sleep(self, attempt: int) -> None:
        delay = min(RETRY_BACKOFF_CAP, RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
        scale = _backoff_scale()
        deadline = time.monotonic() + (delay + random.uniform(0, 0.2)) * scale
        while time.monotonic() < deadline:
            self.stopper.check()
            time.sleep(min(0.25, max(0.01, deadline - time.monotonic())))
        self.stopper.check()

    def flush(self) -> None:
        f = self._file
        if f is not None and not f.closed:
            f.flush()

    def _save_due(self) -> bool:
        now = time.monotonic()
        if now - self._last_save >= STATE_SAVE_INTERVAL:
            self._last_save = now
            return True
        return False

    def abort(self) -> None:
        """Stop from the main thread (KeyboardInterrupt path): flush and persist."""
        self.stopper.stop()
        try:
            with self._io_lock:
                if self._file is not None and not self._file.closed:
                    self._file.flush()
        except Exception:  # noqa: BLE001
            pass
        self.save_state()

    # ── finalize ──────────────────────────────────────────────────────────
    def _finalize(self) -> DownloadResult:
        self._close_part()
        if self.stopper.stopped:
            self.save_state()
            raise Interrupted()
        part = self._part_path()
        if not part.exists():
            raise DownloadError("no downloaded data present")
        if self._size is not None and part.stat().st_size != self._size:
            raise DownloadError(f"size mismatch: have {part.stat().st_size}, want {self._size}")
        result = DownloadResult()
        result.url = self.url
        result.resumed = self.resumed
        result.streamed = self._streaming_mode()
        result.size = self._size
        result.path = self.out_path
        if self.expected_sha256:
            digest = self._sha256(part)
            if digest != self.expected_sha256.lower():
                raise DownloadError(
                    f"SHA-256 mismatch: computed {digest} vs expected {self.expected_sha256.lower()}")
            result.sha256 = digest
        state_path = self._state_path()
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, self.out_path)
        try:
            state_path.unlink()
        except FileNotFoundError:
            pass
        return result

    def _sha256(self, path: pathlib.Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    # ── paths ─────────────────────────────────────────────────────────────
    def _part_path(self) -> pathlib.Path:
        # keyed to the (already uniquified) out_path: two downloads of the
        # same URL must never share a part file, or the last one wins and
        # replaces the other's data
        return pathlib.Path(str(self.out_path) + PART_SUFFIX)

    def _state_path(self) -> pathlib.Path:
        return pathlib.Path(str(self.out_path) + STATE_SUFFIX)

    def _close_part(self) -> None:
        f = self._file
        self._file = None
        if f is not None and not f.closed:
            try:
                f.flush()
                f.close()
            except OSError:
                pass

    def save_state(self) -> None:
        """Persist segment positions. Written atomically (tmp + rename): the
        state is re-saved twice a second while `mgd list` / the GUI poll read
        it, and a torn read would make the download vanish from the list."""
        path = self._state_path()
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            tmp.write_text(json.dumps({
                "kind": "mgd-download-state", "version": 1,
                "url": self.url, "source_url": self.source_url,
                "filename": self._filename,
                "size": self._size, "etag": self._etag,
                "last_modified": self._last_modified,
                "accept_ranges": self._accept_ranges,
                "pid": getattr(self, "pid", None),
                "out_path": str(self.out_path),
                "referer": self.referer,
                "cookies": self.cookies_file,
                "segments": [s.to_json() for s in self._segments],
            }), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass


def _close_quietly(conn) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except OSError:
        pass