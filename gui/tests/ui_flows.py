#!/usr/bin/env python3
"""End-to-end checks of the MediaGrab app: the real (debug-built) app, the
real mgd engine and a local test site, all inside a throwaway sandbox.

    python3 gui/tests/ui_flows.py            # build + run every flow
    python3 gui/tests/ui_flows.py queue      # only flows whose name matches
    python3 gui/tests/ui_flows.py --keep     # keep the sandbox (snapshots, dumps)

Each flow hands the app a JSON script (see Sources/MediaGrab/UITestHooks.swift,
compiled into debug builds only). The app runs it off-screen without taking
focus, writes window snapshots + state dumps, and quits. Nothing here reads
or writes ~/.config/mgd, ~/Downloads or the installed app's history.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time

GUI = pathlib.Path(__file__).resolve().parent.parent
REPO = GUI.parent
MGD = REPO / "cli" / "bin" / "mgd"
SLOW_SIZE = 6 * 1024 * 1024
FAST_SIZE = 3 * 1024 * 1024


def blob(name: str, size: int) -> bytes:
    seed = hashlib.sha256(name.encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


class Site(http.server.BaseHTTPRequestHandler):
    """/slow/<kbps>/<name>, /fast/<name>, /gated/<name> (needs the page as
    Referer), /page-one, /album, anything else 404."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _html(self, body: str):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _empty(self, status: int):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/page-one":
            return self._html('<html><head><title>One clip page</title></head><body>'
                              '<video src="/gated/single_clip_720p.mp4"></video></body></html>')
        if path == "/album":
            vids = "".join(f'<video><source src="/fast/album_{i}_720p.mp4"></video>'
                           for i in range(1, 5))
            return self._html(f"<html><head><title>Album</title></head><body>{vids}</body></html>")
        logged_in = "session=letmein" in (self.headers.get("Cookie") or "")
        if path.startswith("/members/"):
            if not logged_in:
                return self._empty(403)
            if path == "/members/page":
                return self._html('<html><head><title>Members page</title></head><body>'
                                  '<video src="/members/page_clip.mp4"></video></body></html>')
            path = "/fast/" + path.rsplit("/", 1)[-1]
        parts = path.strip("/").split("/")
        rate = None
        if parts[0] == "slow" and len(parts) == 3:
            rate, name, size = int(parts[1]) * 1024, parts[2], SLOW_SIZE
        elif parts[0] in ("fast", "gated") and len(parts) == 2:
            name, size = parts[1], FAST_SIZE
            if parts[0] == "gated" and "/page-one" not in (self.headers.get("Referer") or ""):
                return self._empty(403)
        else:
            return self._empty(404)
        body = blob(name, size)
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            first, _, last = rng[6:].split(",")[0].partition("-")
            first = int(first)
            last = int(last) if last else size - 1
            chunk = body[first:last + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
        else:
            chunk = body
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        try:
            if rate is None:
                self.wfile.write(chunk)
                return
            step = max(1024, rate // 10)
            for i in range(0, len(chunk), step):
                self.wfile.write(chunk[i:i + step])
                self.wfile.flush()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Run:
    """One launch of the app with a script; `box` persists across launches
    of the same flow (relaunch tests)."""

    def __init__(self, app: pathlib.Path, box: pathlib.Path):
        self.app, self.box = app, box
        self.out = box / "out"
        self.env = dict(os.environ,
                        MEDIAGRAB_MGD=str(MGD),
                        XDG_CONFIG_HOME=str(box / "cfg"),
                        MGD_ROOT=str(box / "root"),
                        MGD_NO_OPEN="1",
                        MEDIAGRAB_DATA_DIR=str(box / "data"),
                        MEDIAGRAB_UITEST_OUT=str(self.out))

    def play(self, steps: list, timeout: float = 120) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        script = self.box / "script.json"
        script.write_text(json.dumps(steps))
        env = dict(self.env, MEDIAGRAB_UITEST_SCRIPT=str(script))
        with open(self.box / "app.log", "ab") as log:
            proc = subprocess.Popen([str(self.app)], env=env, stdout=log, stderr=log)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise AssertionError(f"app did not finish its script within {timeout}s")

    def dump(self, name: str) -> dict:
        return json.loads((self.out / f"{name}.json").read_text())

    def jobs(self, name: str) -> dict:
        return {j["title"]: j for j in self.dump(name)["jobs"]}

    def mgd(self, *args, timeout=60) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(MGD), *args], env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def spawn_mgd(self, *args) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, str(MGD), *args], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def files(self) -> list[str]:
        root = self.box / "root"
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


# ── flows ────────────────────────────────────────────────────────────────

def flow_name_pause_resume(run: Run, base: str):
    """Paste a link → name sheet → download → pause → resume → identical file."""
    url = f"{base}/slow/700/holiday_480p.mp4?rs=480000&secure=XlMW%3D%3D"
    run.play([["submit", url], ["waitsheet", "rename", 20], ["dump", "sheet"],
              ["rename", "Dr. Strange trailer"], ["confirm"],
              ["waitphase", "Strange", "running", 15], ["wait", 2.5], ["dump", "running"],
              ["pause", "Strange"], ["waitphase", "Strange", "paused", 15], ["wait", 1],
              ["dump", "paused"], ["snap", "paused"],
              ["resume", "Strange"], ["waitphase", "Strange", "done", 60], ["wait", 1],
              ["dump", "done"], ["snap", "done"], ["quit"]])
    assert run.dump("sheet")["sheet"] == "rename"
    running, paused, done = (run.dump(n)["jobs"][0] for n in ("running", "paused", "done"))
    assert running["phase"] == "running" and running["owned"]
    assert running["engineID"].startswith("d"), running
    assert paused["phase"] == "paused" and 0 < paused["done"] < paused["total"], paused
    assert done["phase"] == "done" and done["engineID"] == running["engineID"] == paused["engineID"]
    final = pathlib.Path(done["finishedPath"])
    assert final.name == "Dr. Strange trailer.mp4", final.name      # dot in the name ≠ extension
    assert final.read_bytes() == blob("holiday_480p.mp4", SLOW_SIZE), "resumed file differs"
    assert run.files() == ["video/Dr. Strange trailer.mp4"], run.files()


def flow_queue(run: Run, base: str):
    """Only N downloads run at once; the rest wait and then start by themselves."""
    urls = [f"{base}/slow/2500/queue_{c}.mp4" for c in "abc"]
    run.play([["ask", "off"], ["max", 1],
              ["submit", urls[0]], ["waitphase", "queue_a", "running", 15],
              ["submit", urls[1]], ["wait", 0.6], ["submit", urls[2]], ["wait", 0.6],
              ["dump", "start"], ["snap", "queued"],
              ["waitphase", "queue_a", "done", 40], ["wait", 0.8], ["dump", "next"],
              ["waitphase", "queue_c", "done", 60], ["dump", "end"], ["quit"]])
    start, nxt, end = run.jobs("start"), run.jobs("next"), run.jobs("end")
    phases = lambda d: sorted(j["phase"] for j in d.values())
    assert phases(start) == ["queued", "queued", "running"], phases(start)
    assert nxt["queue_a.mp4"]["phase"] == "done"
    assert phases(nxt).count("running") == 1, phases(nxt)        # exactly one took the slot
    assert nxt["queue_b.mp4"]["phase"] == "running", "oldest queued download goes first"
    assert phases(end) == ["done", "done", "done"], phases(end)
    assert len(run.files()) == 3


def flow_cancel(run: Run, base: str):
    """Cancel removes the row and every partial file; neighbours are untouched."""
    run.play([["ask", "off"],
              ["submit", f"{base}/slow/600/keep_me.mp4"], ["wait", 0.5],
              ["submit", f"{base}/slow/600/drop_me.mp4"],
              ["waitphase", "drop_me", "running", 15], ["waitphase", "keep_me", "running", 15],
              ["wait", 2], ["cancel", "drop_me"], ["waitgone", "drop_me", 15], ["wait", 1.5],
              ["dump", "after"], ["pause", "keep_me"], ["waitphase", "keep_me", "paused", 15],
              ["wait", 0.5], ["quit"]])
    after = run.jobs("after")
    assert list(after) == ["keep_me.mp4"] and after["keep_me.mp4"]["phase"] == "running", after
    assert run.files() == ["video/keep_me.mp4.mgdpart", "video/keep_me.mp4.mgdstate"], run.files()


def flow_adopt_external(run: Run, base: str):
    """A download started outside the app (terminal / extension headless mode)
    shows up, can be paused and resumed from the app, and finishes."""
    ext = run.spawn_mgd("grab", f"{base}/slow/900/from_terminal.mp4", "-q")
    try:
        run.play([["wait", 4], ["dump", "seen"], ["snap", "adopted"],
                  ["pause", "from_terminal"], ["waitphase", "from_terminal", "paused", 15],
                  ["dump", "paused"],
                  ["resume", "from_terminal"], ["waitphase", "from_terminal", "done", 60],
                  ["dump", "done"], ["quit"]])
    finally:
        if ext.poll() is None:
            ext.kill()
    seen = run.jobs("seen")["from_terminal.mp4"]
    assert seen["phase"] == "running" and seen["adopted"] and not seen["owned"], seen
    assert 0 < seen["done"] < seen["total"]
    assert ext.returncode == 130, f"the terminal download should have been paused: {ext.returncode}"
    paused = run.jobs("paused")["from_terminal.mp4"]
    assert paused["phase"] == "paused" and paused["engineID"] == seen["engineID"]
    done = run.jobs("done")["from_terminal.mp4"]
    assert done["phase"] == "done"
    assert pathlib.Path(done["finishedPath"]).read_bytes() == blob("from_terminal.mp4", SLOW_SIZE)
    assert len(run.dump("done")["jobs"]) == 1, "one download must be one row"


def _host(run: Run, message: dict) -> dict:
    body = json.dumps(message).encode()
    p = subprocess.run([sys.executable, str(MGD), "native-host"], env=run.env,
                       input=struct.pack("<I", len(body)) + body, capture_output=True, timeout=30)
    (length,) = struct.unpack("<I", p.stdout[:4])
    return json.loads(p.stdout[4:4 + length])


def flow_browser_handoff(run: Run, base: str):
    """What the extension sends arrives in one sheet with names, per-item
    referer and a way to leave items out; a second batch joins the open sheet."""
    page = f"{base}/page-one"
    a, b, c = (f"{base}/gated/handoff_{x}.mp4?sig=1" for x in "abc")
    reply = _host(run, {"command": "download", "urls": [a, b], "referer": page,
                        "titles": {a: "Beach day 720p", b: "Beach day 480p"},
                        "kinds": {a: "file", b: "file"}})
    assert reply.get("handoff") is True, reply

    def late_batch():
        time.sleep(4)
        _host(run, {"command": "download", "urls": [c], "referer": page,
                    "titles": {c: "Late arrival"}})

    threading.Thread(target=late_batch, daemon=True).start()
    run.play([["handoff"], ["waitsheet", "handoff", 10], ["snap", "sheet"], ["dump", "first"],
              ["wait", 5], ["handoff"], ["wait", 0.5],
              ["renameitem", "handoff_b", "Renamed: by/user"], ["snap", "merged"],
              ["confirm"], ["waitphase", "Late arrival", "done", 40],
              ["waitphase", "Beach day 720p", "done", 40], ["wait", 1.5], ["dump", "done"], ["quit"]])
    assert run.dump("first")["sheet"] == "handoff"
    done = run.jobs("done")
    assert sorted(done) == ["Beach day 720p", "Late arrival"], sorted(done)   # invalid name left out
    assert all(j["phase"] == "done" for j in done.values()), done
    assert all("--referer" in j["args"] and page in j["args"] for j in done.values())
    assert run.files() == ["video/Beach day 720p.mp4", "video/Late arrival.mp4"], run.files()
    assert not (run.box / "cfg" / "mgd" / "handoff.json").exists(), "hand-off must be consumed"


BROWSER_COOKIES = [{"name": "session", "value": "letmein", "domain": "127.0.0.1",
                    "hostOnly": True, "path": "/", "secure": False, "session": True}]


def flow_site_login(run: Run, base: str):
    """Members-only media sent from the browser downloads with the user's
    login - a file, and a page the app scans itself."""
    clip = f"{base}/members/vip_clip.mp4"
    assert _host(run, {"command": "download", "urls": [clip], "referer": f"{base}/members/page",
                       "titles": {clip: "VIP clip"}, "kinds": {clip: "file"},
                       "cookies": BROWSER_COOKIES})["handoff"]
    run.play([["handoff"], ["waitsheet", "handoff", 10], ["snap", "sheet"], ["confirm"],
              ["waitphase", "VIP clip", "done", 30], ["wait", 0.5], ["quit"]])
    page = f"{base}/members/page"
    assert _host(run, {"command": "download", "urls": [page], "titles": {page: "Members page"},
                       "kinds": {page: "page"}, "cookies": BROWSER_COOKIES})["handoff"]
    run.play([["ask", "off"], ["handoff"], ["waitphase", "Members page", "done", 30],
              ["wait", 0.5], ["dump", "done"], ["quit"]])
    done = run.jobs("done")
    assert done["VIP clip"]["phase"] == "done" and done["Members page"]["phase"] == "done", done
    assert all("--cookies" in j["args"] for j in done.values())
    assert run.files() == ["video/Members page.mp4", "video/VIP clip.mp4"], run.files()
    assert pathlib.Path(done["VIP clip"]["finishedPath"]).read_bytes() == blob("vip_clip.mp4", FAST_SIZE)
    history = (run.box / "data" / "history.json").read_text()
    assert "letmein" not in history, "cookie values must never reach the history file"


def flow_failure_and_retry(run: Run, base: str):
    """A dead link fails with a readable reason and can be tried again."""
    run.play([["ask", "off"], ["submit", f"{base}/gone/missing.mp4"],
              ["waitphase", "missing", "failed", 30], ["dump", "failed"], ["snap", "failed"],
              ["resume", "missing"], ["wait", 0.3], ["dump", "retrying"],
              ["waitphase", "missing", "failed", 30], ["dump", "failed-again"], ["quit"]])
    failed = run.jobs("failed")["missing.mp4"]
    assert failed["phase"] == "failed" and "404" in failed["error"], failed
    assert run.jobs("retrying")["missing.mp4"]["phase"] in ("running", "queued", "failed")
    assert run.jobs("failed-again")["missing.mp4"]["phase"] == "failed"
    assert not [f for f in run.files() if "missing" in f], run.files()


def flow_pages(run: Run, base: str):
    """A page with one video downloads it with the page as Referer; a page
    with several opens the picker."""
    run.play([["ask", "off"], ["submit", f"{base}/page-one"],
              ["waitphase", "single_clip", "done", 40], ["dump", "one"],
              ["ask", "on"], ["submit", f"{base}/album"], ["waitsheet", "batch", 30],
              ["snap", "batch"], ["confirm"], ["waitphase", "album_4", "done", 40],
              ["waitphase", "album_1", "done", 40], ["wait", 1], ["dump", "album"], ["quit"]])
    one = run.dump("one")["jobs"][0]
    assert one["phase"] == "done" and "--referer" in one["args"], one   # the CDN 403s without it
    album = run.jobs("album")
    assert sum(1 for t in album if t.startswith("album_")) == 4, sorted(album)
    assert all(j["phase"] == "done" for j in album.values())
    assert len(run.files()) == 5, run.files()


def flow_relaunch(run: Run, base: str):
    """Quitting mid-download loses nothing: finished rows come back, the
    unfinished one comes back paused and resumes to an identical file."""
    run.play([["ask", "off"], ["submit", f"{base}/fast/finished_before.mp4"],
              ["waitphase", "finished_before", "done", 30],
              ["submit", f"{base}/slow/500/in_flight.mp4"],
              ["waitphase", "in_flight", "running", 15], ["wait", 2.5],
              ["pause", "in_flight"], ["waitphase", "in_flight", "paused", 15], ["wait", 0.6],
              ["quit"]])
    history = json.loads((run.box / "data" / "history.json").read_text())
    assert [h["title"] for h in history] == ["finished_before.mp4"], history
    run.play([["wait", 4], ["dump", "relaunched"], ["snap", "relaunched"],
              ["resume", "in_flight"], ["waitphase", "in_flight", "done", 90],
              ["wait", 1], ["dump", "done"], ["quit"]])
    back = run.jobs("relaunched")
    assert back["finished_before.mp4"]["phase"] == "done"
    assert back["in_flight.mp4"]["phase"] == "paused" and back["in_flight.mp4"]["done"] > 0, back
    assert len(back) == 2, "no duplicate rows after a relaunch"
    done = run.jobs("done")["in_flight.mp4"]
    assert done["phase"] == "done"
    assert pathlib.Path(done["finishedPath"]).read_bytes() == blob("in_flight.mp4", SLOW_SIZE)


def flow_snapshots(run: Run, base: str):
    """Every sheet and list state renders (snapshots for visual review)."""
    steps = [["seed"], ["snap", "list"], ["select", "Family"], ["select", "Mountain"],
             ["details", "Members"], ["snap", "selection"],
             ["search", "zzz-no-match"], ["snap", "no-match"], ["search", ""],
             ["sheet", "missingtools"], ["snap", "warning"]]
    for sheet in ("quality", "qualityerror", "rename", "batch", "torrent",
                  "diagnostics", "extension"):
        steps += [["sheet", sheet], ["snap", f"sheet-{sheet}"], ["closesheet"], ["wait", 0.6]]
    # picking a quality must swap the quality sheet for the name sheet
    steps += [["sheet", "quality"], ["wait", 0.8], ["confirm"], ["waitsheet", "rename", 5],
              ["wait", 1.2], ["snap", "after-quality"], ["dump", "after-quality"], ["closesheet"],
              ["wait", 0.6]]
    steps += [["settingsview"], ["snap", "settings"], ["quit"]]
    run.play(steps)
    shots = sorted(p.name for p in run.out.glob("*.png"))
    assert len(shots) >= 14, shots
    assert run.dump("after-quality")["sheet"] == "rename"
    log = (run.out / "uitest.log").read_text()
    # the sheet actually on screen is the 500pt-wide name sheet, not the
    # 440pt quality sheet it replaced
    assert "wrote after-quality-sheet.png (500x" in log, log[-600:]


FLOWS = [flow_name_pause_resume, flow_queue, flow_cancel, flow_adopt_external,
         flow_browser_handoff, flow_site_login, flow_failure_and_retry, flow_pages, flow_relaunch,
         flow_snapshots]


def main() -> int:
    args = sys.argv[1:]
    keep = "--keep" in args
    only = [a for a in args if not a.startswith("-")]
    print("building the debug app…", flush=True)
    build = subprocess.run(["swift", "build", "-c", "debug"], cwd=GUI,
                           capture_output=True, text=True)
    if build.returncode != 0:
        print(build.stdout[-3000:], build.stderr[-3000:])
        return 1
    bin_path = subprocess.run(["swift", "build", "-c", "debug", "--show-bin-path"], cwd=GUI,
                              capture_output=True, text=True).stdout.strip()
    app = pathlib.Path(bin_path) / "MediaGrab"

    class QuietServer(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass      # clients hanging up mid-transfer is what pause/cancel does

    server = QuietServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    sandbox = pathlib.Path(tempfile.mkdtemp(prefix="mediagrab-ui-"))
    passed, failed = 0, []
    try:
        for flow in FLOWS:
            name = flow.__name__[5:]
            if only and not any(o in name for o in only):
                continue
            box = sandbox / name
            box.mkdir()
            t0 = time.monotonic()
            try:
                flow(Run(app, box), base)
                passed += 1
                print(f"PASS {name}  ({time.monotonic() - t0:.0f}s)", flush=True)
            except Exception as err:  # noqa: BLE001
                failed.append(name)
                print(f"FAIL {name}: {type(err).__name__}: {err}", flush=True)
                log = box / "out" / "uitest.log"
                if log.exists():
                    print("     " + "\n     ".join(log.read_text().splitlines()[-6:]))
    finally:
        server.shutdown()
        subprocess.run(["defaults", "delete", "mediagrab.uitest"], capture_output=True)
        if keep or failed:
            print(f"sandbox kept: {sandbox}")
        else:
            shutil.rmtree(sandbox, ignore_errors=True)
    print(f"\n{passed} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
