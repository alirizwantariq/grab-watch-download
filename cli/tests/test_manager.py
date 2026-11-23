"""Download-manager behaviour the GUI and the browser extension rely on:
stable download ids, safe pause/resume/cancel, file naming, stream
(HLS/DASH) routing, yt-dlp fallback and extension hand-offs.

Every test builds its own config dir + download root inside the sandbox,
so none of them can see another test's (or the user's) downloads.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import pathlib
import signal
import stat
import struct
import subprocess
import sys
import threading
import time

import conftest
from mgd import categorize, cli as m
from mgd.engine import Downloader

CLI_DIR = pathlib.Path(__file__).parent.parent


# ── helpers ──────────────────────────────────────────────────────────────

def _env(tmp_path, **extra):
    env = dict(os.environ,
               PYTHONPATH=".",
               XDG_CONFIG_HOME=str(tmp_path / "cfg"),
               MGD_ROOT=str(tmp_path / "root"),
               MGD_NO_OPEN="1")
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _mgd(tmp_path, args, timeout=60, **extra):
    return subprocess.run([sys.executable, "bin/mgd", *args], cwd=str(CLI_DIR),
                          capture_output=True, text=True, timeout=timeout,
                          env=_env(tmp_path, **extra))


def _fake_state(directory: pathlib.Path, name: str, pid=None,
                size: int = 1000, pos: int = 400) -> pathlib.Path:
    """A paused (or, with a live pid, 'running') download on disk."""
    directory.mkdir(parents=True, exist_ok=True)
    out = directory / name
    (directory / (name + ".mgdpart")).write_bytes(b"x" * size)
    state = {"kind": "mgd-download-state", "version": 1,
             "url": f"http://example.invalid/{name}",
             "source_url": f"http://example.invalid/{name}",
             "filename": name, "size": size, "etag": None,
             "last_modified": None, "accept_ranges": True, "pid": pid,
             "out_path": str(out), "referer": None,
             "segments": [{"start": 0, "end": size - 1, "pos": pos}]}
    st_path = directory / (name + ".mgdstate")
    st_path.write_text(json.dumps(state))
    return st_path


def _items(tmp_path):
    return json.loads(_mgd(tmp_path, ["list", "--json"]).stdout)["items"]


FAKE_YTDLP = r'''#!/usr/bin/env python3
"""Stand-in for yt-dlp: records how it was called, then plays a role."""
import json, os, sys, time
log = os.environ.get("FAKE_YTDLP_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\n")
mode = os.environ.get("FAKE_YTDLP_MODE", "ok")
if mode == "sleep":
    with open(os.environ["FAKE_YTDLP_PIDFILE"], "w") as fh:
        fh.write(str(os.getpid()))
    print("[download] Destination: /nowhere/clip.mp4", flush=True)
    time.sleep(120)
    sys.exit(0)
if mode == "fail":
    print("ERROR: Unsupported URL", file=sys.stderr)
    sys.exit(1)
out = os.environ.get("FAKE_YTDLP_OUT")
if out:
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(b"v" * 2048)
    print("MGDPROG 1024 2048 NA", flush=True)
    print("MGDFINAL " + out, flush=True)
sys.exit(0)
'''


def _install_fake_ytdlp(tmp_path) -> dict:
    bindir = tmp_path / "fakebin"
    bindir.mkdir(parents=True, exist_ok=True)
    exe = bindir / "yt-dlp"
    exe.write_text(FAKE_YTDLP)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / "ytdlp-calls.jsonl"
    return {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_YTDLP_LOG": str(log)}


def _ytdlp_calls(tmp_path) -> list[list[str]]:
    log = tmp_path / "ytdlp-calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class _Page:
    """Tiny HTTP server answering every GET with one HTML document."""

    def __init__(self, html: str):
        body = html.encode()

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/watch/123"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# ── stable ids ───────────────────────────────────────────────────────────

def test_sandbox_is_active():
    conftest.assert_sandboxed()
    assert os.environ.get("MGD_NO_OPEN") == "1"


def test_ids_are_stable_while_the_list_shifts(tmp_path):
    """The number `mgd list` prints for a download changes when another
    one finishes; its id must not - that is what the GUI addresses."""
    video = tmp_path / "root" / "video"
    _fake_state(video, "a-first.mp4")
    _fake_state(video, "b-second.mp4")
    _fake_state(video, "c-third.mp4")
    before = {pathlib.Path(i["out_path"]).name: i for i in _items(tmp_path)}
    assert list(before) == ["a-first.mp4", "b-second.mp4", "c-third.mp4"]
    ids = {i["id"] for i in before.values()}
    assert len(ids) == 3
    assert all(i.startswith("d") and not i.isdigit() for i in ids)
    assert all(i["state"] == "paused" and i["done"] == 400 and
               i["kind"] == "file" for i in before.values())
    # the first download "finishes": everything below it moves up one row
    (video / "a-first.mp4.mgdstate").unlink()
    after = {pathlib.Path(i["out_path"]).name: i for i in _items(tmp_path)}
    assert after["b-second.mp4"]["id"] == before["b-second.mp4"]["id"]
    assert after["c-third.mp4"]["id"] == before["c-third.mp4"]["id"]


def test_cancel_by_id_only_touches_that_download(tmp_path):
    """Regression: cancelling by list position after the list shifted
    deleted the wrong download's partial data."""
    video = tmp_path / "root" / "video"
    _fake_state(video, "a-first.mp4")
    _fake_state(video, "b-second.mp4")
    _fake_state(video, "c-third.mp4")
    target = [i for i in _items(tmp_path)
              if i["out_path"].endswith("c-third.mp4")][0]
    # the list shifts between "look" and "act"
    (video / "a-first.mp4.mgdstate").unlink()
    (video / "a-first.mp4.mgdpart").unlink()
    out = _mgd(tmp_path, ["cancel", target["id"]])
    assert out.returncode == 0, out.stdout + out.stderr
    left = sorted(p.name for p in video.iterdir())
    assert left == ["b-second.mp4.mgdpart", "b-second.mp4.mgdstate"], left


def test_targets_accept_number_path_and_reject_unknown(tmp_path):
    video = tmp_path / "root" / "video"
    st_a = _fake_state(video, "a.mp4")
    _fake_state(video, "b.mp4")
    # unknown id: nothing happens, clear error
    bad = _mgd(tmp_path, ["cancel", "dffffffffff"])
    assert bad.returncode == 2 and "no download matches" in bad.stderr
    assert len(_items(tmp_path)) == 2
    # out of range number
    oob = _mgd(tmp_path, ["pause", "9"])
    assert oob.returncode == 2 and "out of range" in oob.stderr
    # output path works as a target
    by_path = _mgd(tmp_path, ["cancel", str(video / "b.mp4")])
    assert by_path.returncode == 0, by_path.stderr
    assert [pathlib.Path(i["out_path"]).name for i in _items(tmp_path)] == ["a.mp4"]
    # ...and so does the classic number for terminal use
    by_number = _mgd(tmp_path, ["cancel", "1"])
    assert by_number.returncode == 0, by_number.stderr
    assert not st_a.exists() and _items(tmp_path) == []


def _stand_in_process(*argv) -> subprocess.Popen:
    """A harmless process that stays alive for a minute."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *argv])


def test_resume_refuses_a_download_that_is_already_running(tmp_path):
    """Two processes writing one part file would corrupt it."""
    video = tmp_path / "root" / "video"
    owner = _stand_in_process("mgd", "grab", "http://example.invalid/live.mp4")
    try:
        _fake_state(video, "live.mp4", pid=owner.pid)
        item = _items(tmp_path)[0]
        assert item["state"] == "running"
        out = _mgd(tmp_path, ["resume", item["id"], "--json-progress"])
        assert out.returncode == 1
        events = [json.loads(l) for l in out.stdout.splitlines() if l.startswith("{")]
        assert events and events[-1]["type"] == "error"
        assert "already downloading" in events[-1]["message"]
    finally:
        owner.kill()
        owner.wait()


def test_a_recycled_pid_does_not_make_a_dead_download_look_alive(tmp_path):
    """After a crash the pid in the state file can belong to another
    program. That download is paused (resumable), not 'running' forever."""
    video = tmp_path / "root" / "video"
    stranger = _stand_in_process("some", "other", "program")
    try:
        _fake_state(video, "crashed.mp4", pid=stranger.pid)
        assert _items(tmp_path)[0]["state"] == "paused"
    finally:
        stranger.kill()
        stranger.wait()


def test_an_exited_but_unreaped_process_is_not_running(tmp_path):
    video = tmp_path / "root" / "video"
    gone = subprocess.Popen([sys.executable, "-c", "pass", "mgd"])
    time.sleep(1.0)                  # exited; still a zombie until we wait()
    try:
        _fake_state(video, "zombie.mp4", pid=gone.pid)
        assert _items(tmp_path)[0]["state"] == "paused"
    finally:
        gone.wait()


def test_pause_reports_not_running(tmp_path):
    video = tmp_path / "root" / "video"
    _fake_state(video, "idle.mp4")
    item = _items(tmp_path)[0]
    out = _mgd(tmp_path, ["pause", item["id"]])
    assert out.returncode == 0 and "not running" in out.stdout


def test_state_file_is_never_half_written(tmp_path):
    """save_state runs twice a second while `mgd list` reads the file; a
    torn read made downloads flicker out of the GUI list."""
    dl = Downloader("http://example.invalid/big.bin", tmp_path / "big.bin")
    dl._size = 10_000_000
    from mgd.engine import Segment
    dl._segments = [Segment(i * 1000, i * 1000 + 999, 500) for i in range(4000)]
    st_path = pathlib.Path(str(tmp_path / "big.bin") + ".mgdstate")
    dl.save_state()
    stop = threading.Event()
    torn: list[str] = []

    def reader():
        while not stop.is_set():
            try:
                json.loads(st_path.read_text(encoding="utf-8"))
            except ValueError as err:
                torn.append(str(err))
            except OSError:
                pass

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    for _ in range(150):
        dl.save_state()
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert not torn, f"{len(torn)} torn reads, e.g. {torn[0]}"
    assert not list(tmp_path.glob(".*.tmp")), "temp file left behind"


def test_journal_forgets_finished_downloads(tmp_path):
    cfg_home = tmp_path / "cfg"
    old = os.environ["XDG_CONFIG_HOME"]
    os.environ["XDG_CONFIG_HOME"] = str(cfg_home)
    try:
        alive = _fake_state(tmp_path / "elsewhere", "keep.bin")
        journal = cfg_home / "mgd" / "journal.json"
        journal.parent.mkdir(parents=True)
        journal.write_text(json.dumps([str(tmp_path / "gone.bin.mgdstate"),
                                       str(alive)]))
        m._journal_record(tmp_path / "elsewhere" / "new.bin")
        entries = json.loads(journal.read_text())
        assert entries == [str(alive),
                           str(tmp_path / "elsewhere" / "new.bin.mgdstate")]
    finally:
        os.environ["XDG_CONFIG_HOME"] = old


# ── naming ───────────────────────────────────────────────────────────────

def test_clean_out_name():
    assert m._clean_out_name("  my/clip:final\x07  ") == "my-clip-final-"
    assert m._clean_out_name("../../etc/passwd") == "-..-etc-passwd"
    assert m._clean_out_name("...hidden") == "hidden"
    assert m._clean_out_name(None) == ""
    assert m._clean_out_name("   ") == ""


def test_typed_extension():
    assert m._typed_extension("movie.mkv") == "mkv"
    assert m._typed_extension("notes.final.PDF") == "pdf"
    assert m._typed_extension("Dr. Strange trailer") == ""    # punctuation
    assert m._typed_extension("take 1.2") == ""               # not an ext
    assert m._typed_extension("plain name") == ""
    assert m._typed_extension("data.xyz") == ""
    assert m._typed_extension("data.xyz", ".xyz") == "xyz"    # the source's own


def test_destination_keeps_the_real_extension(tmp_path):
    def dest(url, out_name):
        args = argparse.Namespace(out=None, out_name=out_name, filetype=None,
                                  dir=str(tmp_path / "d"))
        return m._destination(args, {}, url).name

    url = "https://cdn.example/v/vid_720p.mp4?rs=720000&secure=abc%3D%3D"
    assert dest(url, "Dr. Strange trailer") == "Dr. Strange trailer.mp4"
    assert dest(url, "holiday") == "holiday.mp4"
    assert dest(url, "holiday.mkv") == "holiday.mkv"          # explicit choice
    assert dest(url, "a/b") == "a-b.mp4"                      # no traversal
    assert dest(url, None) == "vid_720p.mp4"                  # no query junk
    # a url with no extension must not invent one from the whole file name
    assert dest("https://example.com/download", "report") == "report"
    try:
        dest(url, "   ")
    except RuntimeError as err:
        assert "cannot be empty" in str(err)
    else:
        raise AssertionError("a blank name must be rejected")


# ── streams + yt-dlp routing ─────────────────────────────────────────────

def test_stream_manifest_detection():
    yes = ["https://cdn.x/hls/master.m3u8",
           "https://cdn.x/hls/index.M3U8?token=1",
           "https://cdn.x/dash/manifest.mpd#t=3"]
    no = ["https://cdn.x/video.mp4", "https://cdn.x/watch?file=a.m3u8",
          "https://cdn.x/m3u8/video.mp4", "https://cdn.x/page"]
    assert all(m._is_stream_manifest(u) for u in yes)
    assert not any(m._is_stream_manifest(u) for u in no)


def test_hls_link_is_assembled_by_ytdlp_not_saved_as_a_playlist(tmp_path):
    """Regression: an .m3u8 link used to be 'downloaded' as a few-KB text
    playlist. It must go to yt-dlp with the page as Referer and the name
    the user typed (minus the playlist extension)."""
    fake = _install_fake_ytdlp(tmp_path)
    final = tmp_path / "out" / "My Clip.mp4"
    out = _mgd(tmp_path,
               ["grab", "http://127.0.0.1:9/hls/master.m3u8",
                "--referer", "https://site.example/watch/1",
                "--out-name", "My Clip.m3u8", "-d", str(tmp_path / "out"),
                "--json-progress"],
               FAKE_YTDLP_OUT=final, **fake)
    assert out.returncode == 0, out.stdout + out.stderr
    calls = _ytdlp_calls(tmp_path)
    assert len(calls) == 1, calls
    argv = calls[0]
    assert argv[-2:] == ["--", "http://127.0.0.1:9/hls/master.m3u8"]
    assert argv[argv.index("--add-headers") + 1] == "Referer:https://site.example/watch/1"
    template = argv[argv.index("-o") + 1]
    assert template == str(tmp_path / "out" / "My Clip.%(ext)s"), template
    events = [json.loads(l) for l in out.stdout.splitlines() if l.startswith('{"type"')]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and "progress" in kinds and kinds[-1] == "done"
    assert events[-1]["path"] == str(final) and events[-1]["size"] == 2048
    # nothing was saved as a playlist
    assert not list((tmp_path / "root").rglob("*.m3u8"))


def test_percent_in_typed_name_survives_the_ytdlp_template(tmp_path):
    fake = _install_fake_ytdlp(tmp_path)
    out = _mgd(tmp_path, ["grab", "https://www.youtube.com/watch?v=abc",
                          "--out-name", "100% real", "-d", str(tmp_path / "o"),
                          "--quality", "720", "--json-progress"], **fake)
    assert out.returncode == 0, out.stderr
    argv = _ytdlp_calls(tmp_path)[0]
    assert argv[argv.index("-o") + 1] == str(tmp_path / "o" / "100%% real.%(ext)s")


def test_video_downloads_prefer_formats_every_player_opens(tmp_path):
    """yt-dlp's own ranking favours VP9/Opus (.webm), which QuickTime cannot
    play. Within the chosen resolution mgd asks for H.264 + AAC (.mp4)."""
    fake = _install_fake_ytdlp(tmp_path)
    url = "https://www.youtube.com/watch?v=abc"

    def flags(*quality):
        (tmp_path / "ytdlp-calls.jsonl").unlink(missing_ok=True)
        out = _mgd(tmp_path, ["grab", url, *quality, "--json-progress"], **fake)
        assert out.returncode == 0, out.stderr
        return _ytdlp_calls(tmp_path)[0]

    for quality in (["--quality", "720"], ["--quality", "best"]):
        argv = flags(*quality)
        assert argv[argv.index("-S") + 1] == "res,fps,vcodec:h264,acodec:m4a", argv
    assert "-S" not in flags("--quality", "audio")       # audio extraction
    assert "-S" not in flags("--quality", "f137+140")    # explicit format ids


def test_page_without_direct_media_falls_back_to_ytdlp(tmp_path):
    """Players that build their source in JS leave nothing for the plain
    page scan; yt-dlp's site extractors get a go before giving up."""
    page = _Page("<html><head><title>t</title></head><body>"
                 "<div id='player'></div></body></html>")
    try:
        fake = _install_fake_ytdlp(tmp_path)
        final = tmp_path / "o" / "clip.mp4"
        ok = _mgd(tmp_path, ["grab", page.url, "-d", str(tmp_path / "o"),
                             "--json-progress"], FAKE_YTDLP_OUT=final, **fake)
        assert ok.returncode == 0, ok.stdout + ok.stderr
        calls = _ytdlp_calls(tmp_path)
        assert len(calls) == 1 and calls[0][-1] == page.url
        assert '"type": "done"' in ok.stdout

        # unsupported site: fails fast (two cookie-less tries, no walk over
        # every browser's cookie store) with a clear message
        (tmp_path / "ytdlp-calls.jsonl").unlink()
        bad = _mgd(tmp_path, ["grab", page.url, "--json-progress"],
                   FAKE_YTDLP_MODE="fail", **fake)
        assert bad.returncode == 1
        assert len(_ytdlp_calls(tmp_path)) == 2
        errors = [json.loads(l) for l in bad.stdout.splitlines()
                  if l.startswith('{"type": "error"')]
        assert errors and "no downloadable media" in errors[-1]["message"]
    finally:
        page.stop()


def test_stream_playlist_picked_from_a_page_goes_to_ytdlp_with_the_page_as_referer(tmp_path):
    page = _Page('<html><body><video><source src="/hls/master.m3u8">'
                 '</video></body></html>')
    try:
        fake = _install_fake_ytdlp(tmp_path)
        final = tmp_path / "o" / "picked.mp4"
        out = _mgd(tmp_path, ["grab", page.url, "--pick", "1", "-d", str(tmp_path / "o")],
                   FAKE_YTDLP_OUT=final, **fake)
        assert out.returncode == 0, out.stdout + out.stderr
        calls = _ytdlp_calls(tmp_path)
        assert len(calls) == 1, calls
        argv = calls[0]
        assert argv[-1].endswith("/hls/master.m3u8"), argv[-1]
        assert argv[argv.index("--add-headers") + 1] == f"Referer:{page.url}"
        assert not list(tmp_path.rglob("*.m3u8")), "the playlist text must not be saved"
    finally:
        page.stop()


def test_page_with_a_direct_file_does_not_need_ytdlp(tmp_path):
    content = b"direct media " * 4096
    origin = conftest.DripOrigin(content, chunk=64 * 1024, delay=0)
    page = _Page(f'<html><body><video src="{origin.url}"></video></body></html>')
    try:
        fake = _install_fake_ytdlp(tmp_path)
        out = _mgd(tmp_path, ["grab", page.url, "-d", str(tmp_path / "o"), "-q"],
                   FAKE_YTDLP_MODE="fail", **fake)
        assert out.returncode == 0, out.stdout + out.stderr
        assert (tmp_path / "o" / "drip.bin").read_bytes() == content
        assert _ytdlp_calls(tmp_path) == []
    finally:
        page.stop()
        origin.stop()


def test_pausing_a_ytdlp_download_stops_ytdlp(tmp_path):
    """Regression: the GUI pauses by signalling mgd. yt-dlp used to keep
    downloading in the background, and mgd moved on to its next strategy
    instead of stopping."""
    fake = _install_fake_ytdlp(tmp_path)
    pidfile = tmp_path / "ytdlp.pid"
    proc = subprocess.Popen(
        [sys.executable, "bin/mgd", "grab", "https://www.youtube.com/watch?v=abc",
         "--quality", "720", "--json-progress"],
        cwd=str(CLI_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
        env=_env(tmp_path, FAKE_YTDLP_MODE="sleep", FAKE_YTDLP_PIDFILE=pidfile,
                 **fake))
    child = None
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not pidfile.exists():
            time.sleep(0.1)
        assert pidfile.exists(), "fake yt-dlp never started"
        time.sleep(0.3)
        child = int(pidfile.read_text())
        assert _pid_alive(child)
        proc.send_signal(signal.SIGTERM)
        stdout, _ = proc.communicate(timeout=30)
        assert proc.returncode == 130, proc.returncode
        deadline = time.time() + 10
        while time.time() < deadline and _pid_alive(child):
            time.sleep(0.1)
        assert not _pid_alive(child), "yt-dlp survived the pause"
        assert len(_ytdlp_calls(tmp_path)) == 1, "moved on to another strategy"
        assert '{"type": "paused"}' in stdout
    finally:
        if proc.poll() is None:
            proc.kill()
        if child and _pid_alive(child):
            os.kill(child, signal.SIGKILL)


# ── extension hand-off ───────────────────────────────────────────────────

def _host(tmp_path, *messages, **extra):
    payload = b""
    for msg in messages:
        body = json.dumps(msg).encode()
        payload += struct.pack("<I", len(body)) + body
    p = subprocess.run([sys.executable, "bin/mgd", "native-host"],
                       cwd=str(CLI_DIR), input=payload, capture_output=True,
                       timeout=30, env=_env(tmp_path, **extra))
    replies, pos = [], 0
    while pos + 4 <= len(p.stdout):
        (ln,) = struct.unpack("<I", p.stdout[pos:pos + 4])
        replies.append(json.loads(p.stdout[pos + 4:pos + 4 + ln]))
        pos += 4 + ln
    return replies


def test_handoff_keeps_names_and_merges_quick_batches(tmp_path):
    """Two downloads sent in quick succession (before the app picked up the
    first) must both reach the app, each with its own page + name."""
    first = _host(tmp_path, {
        "command": "download", "urls": ["https://cdn.a/v/1.mp4?sig=x"],
        "referer": "https://a.example/page",
        "titles": {"https://cdn.a/v/1.mp4?sig=x": "Holiday / part 1"},
        "kinds": {"https://cdn.a/v/1.mp4?sig=x": "file"}})
    assert first == [{"ok": True, "handoff": True, "spawned": 1}]
    second = _host(tmp_path, {
        "command": "download",
        "urls": ["https://cdn.b/v/2.mp4", "https://cdn.a/v/1.mp4?sig=x"],
        "referer": "https://b.example/other"})
    assert second[0]["handoff"] is True
    handoff = json.loads((tmp_path / "cfg" / "mgd" / "handoff.json").read_text())
    assert handoff["urls"] == ["https://cdn.a/v/1.mp4?sig=x", "https://cdn.b/v/2.mp4"]
    assert handoff["items"] == [
        {"url": "https://cdn.a/v/1.mp4?sig=x", "referer": "https://a.example/page",
         "name": "Holiday - part 1", "kind": "file"},
        {"url": "https://cdn.b/v/2.mp4", "referer": "https://b.example/other",
         "name": None, "kind": None}]


def test_native_host_query_lists_ids(tmp_path):
    _fake_state(tmp_path / "root" / "video", "clip.mp4")
    ping, query = _host(tmp_path, {"command": "ping"}, {"command": "query"})
    assert ping["ok"] and ping["version"] == "1.3"
    assert query["ok"] and len(query["items"]) == 1
    item = query["items"][0]
    assert item["id"] == _items(tmp_path)[0]["id"]
    assert item["state"] == "paused" and item["done"] == 400


def test_native_host_log_stays_in_the_config_dir(tmp_path):
    """The host log used to be written to the real ~/.config/mgd whatever
    the configured config dir was."""
    (tmp_path / "cfg" / "mgd").mkdir(parents=True)
    (tmp_path / "cfg" / "mgd" / "config.json").write_text('{"open_app": "off"}')
    reply = _host(tmp_path, {"command": "download",
                             "urls": ["http://127.0.0.1:9/nothing.bin"],
                             "destination": str(tmp_path / "d")})
    assert reply == [{"ok": True, "spawned": 1}]
    log = tmp_path / "cfg" / "mgd" / "native-host.log"
    assert log.exists() and "grab spawn: http://127.0.0.1:9/nothing.bin" in log.read_text()


# ── settings for the GUI ─────────────────────────────────────────────────

def test_defaults_and_doctor_json(tmp_path):
    _mgd(tmp_path, ["set-default", "speed-limit", "2MB"])
    cfg = json.loads(_mgd(tmp_path, ["defaults", "--json"]).stdout)
    assert cfg["speed_limit"] == 2_000_000
    assert cfg["root"] == str(tmp_path / "root")
    _mgd(tmp_path, ["set-default", "speed-limit", "off"])
    assert json.loads(_mgd(tmp_path, ["defaults", "--json"]).stdout)["speed_limit"] is None
    doc = json.loads(_mgd(tmp_path, ["doctor", "--json"]).stdout)
    assert set(doc["tools"]) == {"yt-dlp", "ffmpeg", "aria2c", "webtorrent"}
    assert doc["unfinished"] == 0 and doc["root"] == str(tmp_path / "root")
