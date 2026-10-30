"""Segmented, resumable download engine.

Design notes are drawn from the behavior documented on IDM's own site
(internetdownloadmanager.com/support/segmentation.html):
  - dynamic segmentation: large unclaimed ranges are split in half when a new
    connection claims work, instead of splitting the whole file up front
  - finished connections are reused for the next range without reconnect and
    login stages (one keep-alive connection per worker; we reconnect per range,
    since proxies make long-lived sockets less reliable - see README)
  - file positions are saved repeatedly during download so an interrupted or
    power-lost download can resume and assemble correctly

On top of that: per-range retry with exponential backoff and jitter,
ETag/If-Range staleness validation, size verification, optional SHA-256
verification, and proxy support (HTTP CONNECT, TLS-wrapped proxy, SOCKS5).
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import random
import threading
import time

from . import net
from .net import HttpError

CHUNK = 256 * 1024
STATE_SUFFIX = ".mgdstate"
PART_SUFFIX = ".mgdpart"
SPLIT_THRESHOLD = 4 * 1024 * 1024  # ranges bigger than this get split when claimed again
STATE_SAVE_INTERVAL = 0.5
RETRY_BACKOFF_BASE = 0.5
RETRY_BACKOFF_CAP = 30.0
DEFAULT_HEADERS = {"Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


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
          timeout: float = net.DEFAULT_TIMEOUT) -> ProbeResult:
    """Ranged GET (bytes=0-0): learns size/range support without the body."""
    r = ProbeResult()
    r.url = url
    conn, resp = net.open_request(url, proxy, "GET", headers=DEFAULT_HEADERS,
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
                 timeout: float = net.DEFAULT_TIMEOUT):
        self.url = url
        self.out_path = pathlib.Path(out_path).expanduser()
        self.proxy = proxy
        self.conns = max(1, int(conns))
        self.retries = max(1, int(retries))
        self.stopper = stopper or Stopper()
        self.expected_sha256 = expected_sha256
        self.timeout = timeout
        self._resume_state: bytes | None = resume_state

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
            pr = probe(self.url, self.proxy, self.timeout)
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
        return self._finalize()

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
        if state.get("kind") != "mgd-download-state" or state.get("url") != self.url:
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
        pool = 1 if streaming else self.conns
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
            if not unclaimed:
                return None
            largest = max(unclaimed, key=lambda s: s.span() if s.end >= 0 else 1)
            # IDM-style dynamic segmentation: a fresh claimer splits a large
            # unclaimed range in half and takes the second half.
            if largest.end >= 0 and largest.span() > SPLIT_THRESHOLD:
                mid = largest.start + (largest.span() // 2) - 1
                right = Segment(mid + 1, largest.end)
                largest.end = mid
                largest.owner = None
                right.owner = wid
                return right
            largest.owner = wid
            return largest

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
                    headers=dict(DEFAULT_HEADERS), timeout=self.timeout,
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
        keeps pause and periodic state saves responsive."""
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
            self._write(seg, chunk)
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
        deadline = time.monotonic() + delay + random.uniform(0, 0.2)
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
        name = self._filename or self.out_path.name
        return self.out_path.parent / (name + PART_SUFFIX)

    def _state_path(self) -> pathlib.Path:
        name = self._filename or self.out_path.name
        return self.out_path.parent / (name + STATE_SUFFIX)

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
        try:
            self._state_path().write_text(json.dumps({
                "kind": "mgd-download-state", "version": 1,
                "url": self.url, "filename": self._filename,
                "size": self._size, "etag": self._etag,
                "last_modified": self._last_modified,
                "accept_ranges": self._accept_ranges,
                "pid": getattr(self, "pid", None),
                "out_path": str(self.out_path),
                "segments": [s.to_json() for s in self._segments],
            }), encoding="utf-8")
        except OSError:
            pass


def _close_quietly(conn) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except OSError:
        pass