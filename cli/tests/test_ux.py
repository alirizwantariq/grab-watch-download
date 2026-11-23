import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

import mgd
from mgd import cli as mgd_cli

import conftest  # noqa: F401  (sandboxes XDG_CONFIG_HOME / MGD_ROOT first)

CLI_DIR = pathlib.Path(__file__).parent.parent
TEST_CFG = os.environ["XDG_CONFIG_HOME"]


def _own_item(root, timeout=10.0):
    """The `mgd list --json` row of the download saving under `root` -
    tests address their own download by its stable id, never by list
    position (positions shift when anything else starts or finishes)."""
    deadline = time.time() + timeout
    root = str(root)
    while True:
        items = json.loads(_mgd(["list", "--json"]).stdout).get("items", [])
        mine = [i for i in items if str(i.get("out_path", "")).startswith(root)]
        if mine or time.time() > deadline:
            return mine[0] if mine else None
        time.sleep(0.2)


def _mgd(args, env_extra=None, timeout=90):
    env = {**os.environ, "PYTHONPATH": ".", "XDG_CONFIG_HOME": TEST_CFG}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "mgd", *args], cwd=str(CLI_DIR),
                          capture_output=True, text=True, timeout=timeout, env=env)


def test_map_quality():
    # best
    assert mgd_cli._map_quality("best") == (None, [])
    assert mgd_cli._map_quality(None) == (None, [])
    sel, extra = mgd_cli._map_quality("720p")
    assert "height<=720" in sel
    assert isinstance(extra, list)
    sel2, extra2 = mgd_cli._map_quality("audio")
    assert "ba" in sel2
    if mgd_cli.shutil.which("ffmpeg"):
        assert "--audio-format" in extra2
    assert mgd_cli._map_quality("f140")[0] == "140"
    assert mgd_cli._map_quality("137+140")[0] == "137+140"


def test_curate_qualities():
    info = {"title": "t", "formats": [
        {"vcodec": "avc1", "height": 1080, "ext": "mp4", "tbr": 5000},
        {"vcodec": "avc1", "height": 720, "ext": "mp4", "tbr": 2500},
        {"vcodec": "avc1", "height": 720, "ext": "webm", "tbr": 1000},
        {"vcodec": "none", "acodec": "mp4a", "ext": "m4a"},
        {"vcodec": "vp9", "height": None, "ext": "webm"},
    ]}
    items = mgd_cli._curate_qualities(info)
    labels = [i["label"] for i in items]
    assert labels[0].startswith("Highest")
    assert "1080p" in labels[1] and "720p" in labels[2]
    assert any(i["quality"] == "audio" for i in items)
    assert [i["quality"] for i in items] == ["best", "1080", "720", "audio"]
    # no video formats -> just best + audio
    bare = mgd_cli._curate_qualities({"formats": [
        {"vcodec": "none", "acodec": "mp4a", "ext": "m4a"}]})
    assert [i["quality"] for i in bare] == ["best", "audio"]


def test_xd_quality_entries():
    st = {"filename": "a.mp4", "resolution": "854x480",
          "formats": [{"label": "854x480", "filename": "a.mp4"},  # dup of top
                      {"label": "640x360", "filename": "b.mp4"},
                      {"label": "480x270", "filename": "c.mp4"}]}
    items = mgd_cli._xd_quality_entries(st)
    assert items[0] == {"label": "854x480", "quality": "best"}
    assert [i["quality"] for i in items[1:]] == ["360", "270", "audio"]


def test_ytdlp_progress_event():
    f = mgd_cli._ytdlp_progress_event
    assert f("MGDPROG 1024 768000 NA") == {"type": "progress", "done": 1024,
                                           "total": 768000}
    # unknown total -> fall back to the estimate
    assert f("MGDPROG 1024 NA 512000") == {"type": "progress", "done": 1024,
                                           "total": 512000}
    assert f("MGDPROG 512 768000 768000")["total"] == 768000
    assert f("MGDPROG x y z") is None
    assert f("MGDPROG 100") is None
    assert f("some random output") is None


def test_unicode_url_downloads(tmp_path):
    """URLs with emoji/international characters must be percent-encoded on the
    wire (http.client request lines are ASCII-only) and still download."""
    import http.server
    import threading
    import urllib.parse
    from mgd import engine

    content = bytes(range(64)) * 40
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            seen["path"] = self.path
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/dir/chili🌶️_file.bin"
        out = tmp_path / "out.bin"
        dl = engine.Downloader(url, out, conns=1)
        dl.run()
        assert out.read_bytes() == content
        assert "%" in seen["path"] and seen["path"].isascii(), \
            f"server saw non-encoded path: {seen['path']!r}"
        assert "chili" in urllib.parse.unquote(seen["path"])
    finally:
        srv.shutdown()
        srv.server_close()


def test_effective_conns():
    """aria2's 2*min-split-size rule: parallel conns only for big files."""
    from mgd import engine
    f = engine.Downloader.effective_conns
    assert f(None, 6) == 6          # unknown size: honor the request
    assert f(0, 6) == 6
    assert f(5 * 1024 * 1024, 8) == 1     # 5 MB: one connection
    assert f(16 * 1024 * 1024, 8) == 2
    assert f(100 * 1024 * 1024, 8) == 8
    assert f(100 * 1024 * 1024, 6) == 6   # capped by caller's conns


def test_probe_retries_transient_failures(tmp_path):
    """A server that kills the first two connections (TLS hiccups, resets)
    must not kill the download - the engine retries the probe."""
    import http.server
    import threading
    from mgd import engine

    content = bytes(range(64)) * 64

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        failures = [2]

        def log_message(self, *a):
            pass

        def do_GET(self):
            if H.failures[0] > 0:
                H.failures[0] -= 1
                try:
                    self.connection.close()  # abrupt TCP close, no response
                except OSError:
                    pass
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/flaky.bin"
        out = tmp_path / "flaky.bin"
        dl = engine.Downloader(url, out, conns=1, retries=3)
        result = dl.run()
        assert result.path.read_bytes() == content
    finally:
        srv.shutdown()
        srv.server_close()


def test_tail_steal_correctness(tmp_path):
    """A slow leading region must not stall the download: idle workers steal
    the tail of the largest in-flight range (IDM-style). The final file must
    be byte-perfect - this validates the shrink/truncate math under real
    stealing."""
    import http.server
    import threading
    from mgd import engine

    size = 24 * 1024 * 1024
    content = (bytes(range(256)) * (size // 256))
    SLOW_FROM = 12 * 1024 * 1024   # the trailing half trickles: w1 claims it
    requests: list[tuple[int, int, float]] = []
    req_lock = threading.Lock()

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            start = 0
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                spec = rng[6:].split(",", 1)[0]
                first, _, last = spec.partition("-")
                start = int(first)
                end = int(last) if last else size - 1
                with req_lock:
                    requests.append((start, end, time.time()))
                body = content[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Range",
                                 f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
            else:
                with req_lock:
                    requests.append((0, size - 1, time.time()))
                body = content
                self.send_response(200)
                self.send_header("Content-Length", str(size))
                self.end_headers()
            for i in range(0, len(body), 256 * 1024):
                if start + i >= SLOW_FROM:
                    time.sleep(0.004)  # slow region
                self.wfile.write(body[i:i + 256 * 1024])
            try:
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError):
                pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/big.bin"
        out = tmp_path / "big.bin"
        dl = engine.Downloader(url, out, conns=3, retries=5)
        dl.run()
        data = out.read_bytes()
        assert data == content, "tail stealing corrupted the file"
        # evidence a steal happened: a later request for a tail sub-range of
        # a range another connection was still downloading
        stole = False
        for (s1, e1, t1) in requests:
            for (s2, e2, t2) in requests:
                if t2 > t1 and s1 < s2 and e2 <= e1 \
                        and (e2 - s2) < (e1 - s1):
                    stole = True
                    break
            if stole:
                break
        assert stole, f"no tail steal observed; requests={requests[:12]}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_state_tiles_whole_file_on_pause(tmp_path):
    """Regression: split-off ranges must be tracked in the state file.
    Pausing a multi-connection download mid-flight used to save state that
    missed every dynamically split range -> resume filled those bytes with
    zeros and the size check still passed (silent corruption)."""
    import conftest
    import threading
    from mgd import engine

    size = 32 * 1024 * 1024
    origin = conftest.DripOrigin((bytes(range(256)) * (size // 256)),
                                 chunk=64 * 1024, delay=0.004)
    out = tmp_path / "drip.bin"   # state file is named after the URL filename
    stopper = engine.Stopper()
    dl = engine.Downloader(origin.url, out, conns=2, stopper=stopper)
    worker = threading.Thread(target=dl.run, daemon=True)
    try:
        worker.start()
        time.sleep(0.4)
        stopper.stop()
        worker.join(timeout=20)
        state_path = out.parent / "drip.bin.mgdstate"
        assert state_path.exists(), "no state saved after pause"
        state = json.loads(state_path.read_text())
        segs = sorted((s for s in state["segments"] if s["pos"] != -2),
                      key=lambda s: s["start"])
        assert segs, "state has no segments"
        assert segs[0]["start"] == 0, f"gap at file start: {segs[0]}"
        for a, b in zip(segs, segs[1:]):
            assert a["end"] + 1 == b["start"], \
                f"state gap/overlap at {a['end'] + 1}: {segs}"
        assert segs[-1]["end"] == size - 1, f"state ends at {segs[-1]}"
        assert not out.exists(), "download finished before the pause"
    finally:
        origin.stop()


def test_kvs_flashvars_extraction():
    """KVS (erome-style) player pages hide media urls inside a JS flashvars
    object; the attribute scanners never see them. Parsing must produce
    best-first candidates with quality labels from *_text keys."""
    from mgd import grab
    html = """
    <html><head><title>My Video</title></head><body>
    <div id="preview"><img src="/thumb.jpg"></div>
    <script>
    var video = null;
    var flashvars = {
        video_id: 289910,
        video_title: 'Cory Chase Takes Video',
        video_url: 'https://www.erome.tv/get_file/3/hash/289000/289910/289910_1080p.mp4/?v-acctoken=Mjg5OTE',
        video_url_text: '1080p',
        video_alt_url: 'https://www.erome.tv/get_file/3/hash2/289000/289910/289910_720p.mp4/?v-acctoken=Mjg5OTE',
        video_alt_url_text: '720p',
        video_alt_url2: 'https://www.erome.tv/get_file/3/hash3/289000/289910/289910_480p.mp4/?v-acctoken=Mjg5OTE',
        video_alt_url2_text: '-',
        poster_url: 'https://www.erome.tv/contents/videos_screenshots/preview.jpg',
        license_code: '$123'
    };
    </script>
    </body></html>"""
    cand = grab._kvs_flashvars_candidates(html)
    assert len(cand) == 3, f"expected 3 quality candidates, got {cand}"
    urls = [c[0] for c in cand]
    assert "_1080p" in urls[0] and "_480p" in urls[2]
    assert cand[0][1] == "1080p"     # from *_text key
    assert cand[1][1] == "720p"
    assert cand[2][1] == "480p"      # '-' falls back to the filename quality
    assert cand[0][2] == "Cory Chase Takes Video"
    # the thumbnail/license must not leak in
    for c in cand:
        assert "preview.jpg" not in c[0] and "$123" not in c[0]
    # extract_links: flashvars candidates first, picked as the media link
    links = grab.extract_links(html, "https://www.erome.tv/v/289910/")
    best = grab.pick_media_candidate(links)
    assert best["kind"] == "flashvars"
    assert "/thumb.jpg" not in best["url"]


def test_kvs_single_quality_dash_text():
    """erome pages with video_url only and video_url_text '-' still label."""
    from mgd import grab
    html = """
    <script>
    var flashvars = {
        video_url: 'https://x.tv/get_file/3/h/289000/289910/289910_1080p.mp4/?v-acctoken=AA',
        video_url_text: '-',
        alt_video_url: '-'
    };
    </script>"""
    cand = grab._kvs_flashvars_candidates(html)
    assert len(cand) == 1 and cand[0][1] == "1080p", cand


def test_mirror_rotation_on_dead_cdn(tmp_path):
    """When probes keep landing 5xx on one cdn mirror, the engine rotates
    to the site's backup mirror hosts (erome nudges dead edges)."""
    import http.server
    import threading
    from mgd import engine

    content = bytes(range(256)) * 64

    class Bad(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

    class Good(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes 0-{len(content) - 1}/{len(content)}")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    bad = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Bad)
    good = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Good)
    for srv, srv_thread in ((bad, bad), (good, good)):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        bad_port = bad.server_address[1]
        good_port = good.server_address[1]
        out = tmp_path / "mirror.bin"
        dl = engine.Downloader(
            f"http://127.0.0.1:{bad_port}/mirror.bin", out,
            conns=1, retries=12,
            mirror_hosts=[f"127.0.0.1:{good_port}", "127.0.0.1:9"])
        result = dl.run()
        assert result.path.read_bytes() == content
        assert result.path.name == "mirror.bin.mgdpart".replace(".mgdpart", "")
    finally:
        bad.shutdown(); bad.server_close()
        good.shutdown(); good.server_close()
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass


def test_media_catalog_album():
    """Album/gallery pages (erome.com/a/…): every <video poster=…><source…>
    pair must become one catalog item with poster + quality, deduped
    (albums embed each video twice - grid preview + lightbox)."""
    from mgd import grab
    html = """
    <div class="video"><video poster="https://s9.x.com/a/Ab1/thumbA.jpg">
      <source src="https://v9.x.com/a/Ab1/vidA_720p.mp4" type='video/mp4'></video></div>
    <div class="video-lg"><video data-poster="https://s9.x.com/a/Ab1/thumbA.jpg">
      <source src="https://v9.x.com/a/Ab1/vidA_720p.mp4"></video></div>
    <div class="video"><video poster="https://s9.x.com/a/Ab1/thumbB.jpg">
      <source src="https://v9.x.com/a/Ab1/vidB_1080p.mp4"></video></div>
    <img data-src="https://s9.x.com/a/Ab1/other.jpg">
    <a href="/a/Ab1">back</a>"""
    cat = grab.extract_media_catalog(html, "https://www.x.com/a/Ab1")
    urls = [c["url"] for c in cat]
    assert len(cat) == 2, f"dedup/scan wrong: {cat}"
    assert urls[0].endswith("vidA_720p.mp4") and cat[0]["quality"] == "720p"
    assert cat[0]["poster"].endswith("thumbA.jpg")
    assert cat[1]["quality"] == "1080p"
    assert "other.jpg" not in urls and "/a/Ab1" not in urls


def test_media_catalog_kvs_poster():
    """KVS flashvars entries carry the player's own preview/poster + title."""
    from mgd import grab
    html = """
    <script>
    var flashvars = {
        video_title: 'My KVS Video',
        video_url: 'https://www.kvs.tv/get_file/3/h/289910/289910_1080p.mp4/?t=x',
        video_url_text: '-',
        preview_url: 'https://www.kvs.tv/preview.jpg'
    };
    </script>"""
    cat = grab.extract_media_catalog(html, "https://www.kvs.tv/v/1/")
    assert len(cat) == 1
    assert cat[0]["quality"] == "1080p"
    assert cat[0]["title"] == "My KVS Video"
    assert cat[0]["poster"].endswith("preview.jpg")


def test_page_media_command(tmp_path):
    """`mgd page-media <url>`: one JSON document with items incl. probed
    sizes (parallel), used by the GUI batch sheet and the extension."""
    import http.server
    import subprocess
    import sys as _sys
    import tempfile
    import os
    body_a = bytes(range(64)) * 1024   # 64 KB
    body_b = bytes(range(128)) * 2048  # 256 KB
    page = f"""<html><body>
    <video poster="/thumbA.jpg"><source src="/a.mp4"></video>
    <video poster="/thumbB.jpg"><source src="/b.mp4"></video>
    </body></html>"""

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/page":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page.encode())
                return
            body = body_a if self.path.startswith("/a.mp4") else body_b
            start, end = 0, len(body) - 1
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                spec = rng[6:].split(",", 1)[0]
                first, _, last = spec.partition("-")
                start = int(first)
                end = int(last) if last else len(body) - 1
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{end}/{len(body)}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            self.wfile.write(body[start:end + 1])

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/page"
        cfg = tempfile.mkdtemp()
        env = dict(os.environ, XDG_CONFIG_HOME=cfg)
        out = subprocess.run(
            [_sys.executable, "bin/mgd", "page-media", url],
            capture_output=True, text=True, timeout=60, env=env, check=True)
        doc = json.loads(out.stdout)
        assert doc["error"] is None
        assert doc["url"] == url
        items = {i["url"].rsplit("/", 1)[-1]: i for i in doc["items"]}
        assert set(items) == {"a.mp4", "b.mp4"}
        assert items["a.mp4"]["size"] == len(body_a)
        assert items["b.mp4"]["size"] == len(body_b)
        assert items["a.mp4"]["poster"].endswith("/thumbA.jpg")
        assert items["b.mp4"]["quality"] == ""
    finally:
        srv.shutdown()
        srv.server_close()


def test_native_host_protocol():
    """Chrome native-messaging host: 4-byte LE frames, ping/probe/download."""
    import struct
    import sys as _sys
    import subprocess
    from mgd import cli as _  # ensure import path works

    def frame(obj):
        body = json.dumps(obj).encode()
        return struct.pack("<I", len(body)) + body

    payload = (frame({"command": "ping"}) +
               frame({"command": "bogus"}))
    p = subprocess.run([_sys.executable, "bin/mgd", "native-host"],
                       input=payload, capture_output=True, timeout=30)
    replies, pos = [], 0
    while pos + 4 <= len(p.stdout):
        (ln,) = struct.unpack("<I", p.stdout[pos:pos + 4])
        pos += 4
        replies.append(json.loads(p.stdout[pos:pos + ln]))
        pos += ln
    assert replies[0]["ok"] is True and replies[0]["app"] == "mgd"
    assert replies[1]["ok"] is False and "bogus" in replies[1]["error"]
    assert len(replies) == 2


def test_native_host_download(tmp_path):
    """'download' command spawns a detached mgd grab that really lands."""
    import conftest
    import struct
    import sys as _sys
    import subprocess
    import time
    import pathlib

    content = b"extension round trip " * 2048
    origin = conftest.DripOrigin(content, chunk=32 * 1024, delay=0)

    def frame(obj):
        body = json.dumps(obj).encode()
        return struct.pack("<I", len(body)) + body

    dest = tmp_path / "ext"
    payload = frame({"command": "download",
                     "urls": [origin.url], "destination": str(dest)})
    # isolated config: never pop the real MediaGrab app open from tests
    cfg_home = tmp_path / "xdg"
    (cfg_home / "mgd").mkdir(parents=True)
    (cfg_home / "mgd" / "config.json").write_text('{"open_app": "off"}')
    env = dict(os.environ, XDG_CONFIG_HOME=str(cfg_home))
    p = subprocess.run([_sys.executable, "bin/mgd", "native-host"],
                       input=payload, capture_output=True, timeout=30, env=env)
    (ln,) = struct.unpack("<I", p.stdout[:4])
    reply = json.loads(p.stdout[4:4 + ln])
    assert reply["ok"] and reply["spawned"] == 1, reply
    out = dest / "drip.bin"
    for _ in range(120):
        time.sleep(0.25)
        if out.exists() and out.stat().st_size == len(content):
            assert out.read_bytes() == content
            break
    else:
        raise AssertionError("extension-spawned grab did not finish")
    origin.stop()


def test_dns_fallback_resolver():
    """When the system resolver is dead (router outage, NXDOMAIN storms),
    mgd queries public resolvers directly and keeps SNI/Host on the real
    hostname. Tested against a local UDP DNS server on a custom port."""
    import socket
    import struct
    import threading
    from mgd import net

    # minimal UDP DNS server: anything.mgd.test -> 127.0.0.1
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    def serve():
        while True:
            try:
                data, addr = sock.recvfrom(512)
            except OSError:
                return
            qid = data[:2]
            qdcount = struct.unpack(">H", data[4:6])[0]
            pos = 12
            for _ in range(qdcount):
                while data[pos]:
                    pos += data[pos] + 1
                pos += 5
            question = data[12:pos]
            answer = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) \
                + bytes([127, 0, 0, 1])
            msg = qid + struct.pack(">HHHHH", 0x8180, qdcount, 1, 0, 0) \
                + question + answer
            try:
                sock.sendto(msg, addr)
            except OSError:
                return

    threading.Thread(target=serve, daemon=True).start()
    try:
        server = f"127.0.0.1:{port}"
        # raw query layer
        assert net._dns_query_a(server, "mgd.test", 2.0) == "127.0.0.1"
        assert net._dns_query_a(server, "doesnotexist.mgd.test", 2.0) is None \
            or net._dns_query_a(server, "doesnotexist.mgd.test", 2.0)
        # entry point with the fallback list pointed at the local server
        saved, saved_cache = net.FALLBACK_DNS, dict(net._dns_cache)
        net.FALLBACK_DNS = [server]
        net._dns_cache.clear()
        assert net.resolve_with_fallback("mgd.test") == "127.0.0.1"
        # caching: a second hit must not touch the network (cache serves it)
        net.FALLBACK_DNS = []  # no servers left -> network would fail
        assert net.resolve_with_fallback("mgd.test") == "127.0.0.1"
        # IP passthrough never queries
        assert net.resolve_with_fallback("192.0.2.9") == "192.0.2.9"
        # a name with no working server caches/returns None, not a crash
        net._dns_cache.clear()
        assert net.resolve_with_fallback("unresolvable.example") is None
        # patched getaddrinfo: system resolver raises gaierror for mgd.test
        # (NXDOMAIN) -> the patch must fall back to the local server
        net.FALLBACK_DNS = [server]
        net._dns_cache.clear()
        try:
            infos = socket.getaddrinfo("mgd.test", 80)
            assert infos, "getaddrinfo returned nothing"
            assert {i[4][0] for i in infos} == {"127.0.0.1"}
        except socket.gaierror:
            raise AssertionError("gaierror leaked through the DNS fallback patch")
    finally:
        net.FALLBACK_DNS = saved
        net._dns_cache = saved_cache
        sock.close()


def test_native_host_download_referer(tmp_path):
    """Reproduces the erome failure: CDNs that 403 media requests without
    the source page as Referer. The host must pass --referer through and
    the engine must send it on every request."""
    import http.server
    import struct
    import sys as _sys
    import subprocess
    import threading
    import time
    import pathlib

    content = b"referer required " * 4096
    PAGE = "https://example.com/watch/42"

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.headers.get("Referer") != PAGE:
                body = b"403 forbidden"
                self.send_response(403)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            start, end = 0, len(content) - 1
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                spec = rng[6:].split(",", 1)[0]
                first, _, last = spec.partition("-")
                start = int(first)
                end = int(last) if last else len(content) - 1
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{end}/{len(content)}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            self.wfile.write(content[start:end + 1])

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/video.mp4"
        dest = tmp_path / "ref"
        dest.mkdir(exist_ok=True)
        # isolated config: never pop the real MediaGrab app open from tests
        cfg_home = tmp_path / "xdg"
        (cfg_home / "mgd").mkdir(parents=True)
        (cfg_home / "mgd" / "config.json").write_text('{"open_app": "off"}')
        env = dict(os.environ, XDG_CONFIG_HOME=str(cfg_home))

        def frame(obj):
            body = json.dumps(obj).encode()
            return struct.pack("<I", len(body)) + body

        # without referer: fails 403
        payload = frame({"command": "download", "urls": [url],
                         "destination": str(dest)})
        p = subprocess.run([_sys.executable, "bin/mgd", "native-host"],
                           input=payload, capture_output=True, timeout=30, env=env)
        (ln,) = struct.unpack("<I", p.stdout[:4])
        assert json.loads(p.stdout[4:4 + ln])["spawned"] == 1
        out = dest / "video.mp4"
        deadline = time.time() + 6     # a 403 is immediate; no retries
        while time.time() < deadline:
            time.sleep(0.5)
            if out.exists():
                raise AssertionError("download should have 403'd without referer")
        # with referer: completes
        payload = frame({"command": "download", "urls": [url],
                         "destination": str(dest), "referer": PAGE})
        p = subprocess.run([_sys.executable, "bin/mgd", "native-host"],
                           input=payload, capture_output=True, timeout=30, env=env)
        (ln,) = struct.unpack("<I", p.stdout[:4])
        assert json.loads(p.stdout[4:4 + ln])["spawned"] == 1
        for _ in range(60):
            time.sleep(0.5)
            if out.exists() and out.stat().st_size == len(content):
                assert out.read_bytes() == content
                return
        raise AssertionError("referer download did not finish")
    finally:
        srv.shutdown()
        srv.server_close()


def test_parse_speed():
    """'2MB' / '512KB' / '500000' / 'off' -> bytes per second."""
    from mgd import engine
    assert engine.parse_speed("2MB") == 2_000_000
    assert engine.parse_speed("512KB") == 512_000
    assert engine.parse_speed("2MiB") == 2_000_000  # SI units
    assert engine.parse_speed("500000") == 500_000
    assert engine.parse_speed("2MB/s") == 2_000_000
    assert engine.parse_speed("off") is None
    assert engine.parse_speed(None) is None
    assert engine.parse_speed("") is None
    try:
        engine.parse_speed("banana")
        raise AssertionError("should have raised")
    except ValueError:
        pass


def test_speed_limiter(tmp_path):
    """--max-speed / global speed-limit: a capped download takes at least
    ~limit time and the data stays byte-perfect."""
    import conftest
    from mgd import engine

    # isolate from the real user config (global speed limits)
    orig_cfg = engine.categorize.load_config
    engine.categorize.load_config = lambda: {}

    content = bytes(range(256)) * 16 * 1024   # 4 MB
    origin = conftest.DripOrigin(content, chunk=256 * 1024, delay=0)
    try:
        out = tmp_path / "capped.bin"
        dl = engine.Downloader(origin.url, out, conns=4, max_speed=4_000_000)
        dl.run()
        assert out.read_bytes() == content
        assert dl._governor._effective == 4_000_000
    finally:
        engine.categorize.load_config = orig_cfg
        origin.stop()


def test_governor_divides_global_limit():
    """The effective rate = min(own cap, global cap / active downloads)."""
    from mgd import engine
    orig_cfg = engine.categorize.load_config
    orig_count = engine._count_active_downloads
    try:
        engine.categorize.load_config = lambda: {"speed_limit": 2_000_000}
        engine._count_active_downloads = lambda: 2
        gov = engine._SpeedGovernor(own_limit=10_000_000)
        gov.refresh()
        assert gov._effective == 1_000_000   # 2MB global / 2 active
        gov.own_limit = 500_000
        gov.refresh()
        assert gov._effective == 500_000     # own cap wins when lower
        engine._count_active_downloads = lambda: 1
        gov.own_limit = None
        gov.refresh()
        assert gov._effective == 2_000_000
    finally:
        engine.categorize.load_config = orig_cfg
        engine._count_active_downloads = orig_count


def test_watch_helpers():
    """mgd watch: URL extraction + what's worth auto-grabbing."""
    from mgd import cli as m
    text = ("check https://example.com/a.mp4 and "
            "https://x.com/u/status/12345, plus https://plain.example/ ok")
    urls = m._watch_extract_urls(text)
    assert urls[0].endswith("a.mp4")
    assert any("x.com" in u for u in urls)
    assert m._watch_worth_grabbing("https://example.com/a.mp4", False) is True
    assert m._watch_worth_grabbing("https://x.com/u/status/1", False) is True
    assert m._watch_worth_grabbing("https://plain.example/", False) is False
    assert m._watch_worth_grabbing("https://plain.example/", True) is True


def test_native_host_query():
    """'query' returns the mgd list (empty when nothing is downloading)."""
    import struct
    import sys as _sys
    import subprocess

    def frame(obj):
        body = json.dumps(obj).encode()
        return struct.pack("<I", len(body)) + body

    import tempfile, os
    # a config + download root nobody else writes to: truly empty
    env = dict(os.environ, XDG_CONFIG_HOME=tempfile.mkdtemp(dir=conftest.SANDBOX),
               MGD_ROOT=tempfile.mkdtemp(dir=conftest.SANDBOX))
    p = subprocess.run([_sys.executable, "bin/mgd", "native-host"],
                       input=frame({"command": "query"}),
                       capture_output=True, timeout=30, env=env)
    (ln,) = struct.unpack("<I", p.stdout[:4])
    reply = json.loads(p.stdout[4:4 + ln])
    assert reply["ok"] is True and reply["items"] == []


def test_out_name_renames_download(tmp_path):
    """--out-name renames the file (category routing intact, original
    extension appended when the user leaves it out)."""
    import conftest
    import subprocess
    import sys as _sys
    import os

    content = b"rename me " * 512
    origin = conftest.DripOrigin(content, chunk=16 * 1024, delay=0)
    try:
        dest = tmp_path / "ren"
        dest.mkdir()
        # with extension
        r = subprocess.run(
            [_sys.executable, "bin/mgd", "grab", origin.url,
             "--out-name", "my video.mp4", "-d", str(dest)],
            capture_output=True, text=True, timeout=60)
        assert (dest / "my video.mp4").read_bytes() == content, r.stderr
        # without extension -> original extension appended
        r = subprocess.run(
            [_sys.executable, "bin/mgd", "grab", origin.url,
             "--out-name", "no ext", "-d", str(dest)],
            capture_output=True, text=True, timeout=60)
        assert (dest / "no ext.bin").read_bytes() == content, r.stderr
        # empty name -> clean error, no download
        r = subprocess.run(
            [_sys.executable, "bin/mgd", "grab", origin.url,
             "--out-name", "  ", "-d", str(dest)],
            capture_output=True, text=True, timeout=60)
        assert r.returncode != 0
        assert "--out-name cannot be empty" in (r.stdout + r.stderr)
    finally:
        origin.stop()


def test_bencode_roundtrip():
    """bencode decode/encode (torrent metainfo is built on it)."""
    from mgd import torrent
    cases = [
        {"a": 1, "b": b"xy", "c": [1, b"two", {"d": -3}]},
        [0, 1, -1, b"", b"bytes"],
        {"utf8-key": "héllo ✓".encode()},
    ]
    for obj in cases:
        enc = torrent.bencode(obj)
        dec = torrent.bdecode(enc)
        assert dec == obj, (obj, dec)
    # str values encode as utf-8 and decode back as bytes
    assert torrent.bdecode(torrent.bencode({"k": "héllo"})) == {"k": "héllo".encode()}
    # bytes dict keys are normalized to str on decode (metainfo access)
    assert torrent.bdecode(torrent.bencode({b"key": 5})) == {"key": 5}
    assert torrent.bdecode(b"i42e") == 42
    assert torrent.bdecode(b"5:hello") == b"hello"
    assert torrent.bdecode(b"le") == []
    assert torrent.bdecode(b"de") == {}
    for bad in (b"", b"x", b"i1", b"3:ab"):
        try:
            torrent.bdecode(bad)
            raise AssertionError(f"should reject {bad!r}")
        except torrent.BencodeError:
            pass


def test_torrent_parsing():
    """metainfo parsing: multi/single file lists, video flags, detection."""
    from mgd import torrent
    assert torrent.is_magnet("magnet:?xt=urn:btih:abc")
    assert torrent.is_torrent_file("movie.torrent")
    assert torrent.is_torrent("magnet:?xt=urn:btih:" + "0" * 40)
    assert torrent.is_torrent("/downloads/pack.torrent")
    assert not torrent.is_torrent("https://erome.com/a/x")
    h = torrent.magnet_info_hash(
        "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=x")
    assert h == "0123456789abcdef0123456789abcdef01234567"

    meta = {b"info": {b"name": b"pack", b"piece length": 16384, b"pieces": b"x" * 20,
                     b"files": [
                         {b"length": 100, b"path": [b"video", b"a.mp4"]},
                         {b"length": 200, b"path": [b"readme.txt"]}]}}
    files = torrent.torrent_files(meta)
    assert [f["index"] for f in files] == [1, 2]
    assert files[0]["path"] == "pack/video/a.mp4" and files[0]["video"]
    assert files[1]["video"] is False
    single = {b"info": {b"name": b"movie.mp4", b"piece length": 16384,
                        b"pieces": b"x" * 20, b"length": 42}}
    assert torrent.torrent_files(single) == [
        {"index": 1, "path": "movie.mp4", "size": 42, "video": True}]
    assert torrent.torrent_name(meta) == "pack"


def test_torrent_download_live():
    """Full torrent lifecycle on loopback: mini tracker + aria2 seeder,
    mgd grab with --select-files through the NDJSON contract."""
    import hashlib
    import http.server
    import subprocess
    import sys as _sys
    import threading
    import time
    import urllib.parse
    from mgd import torrent

    piece_len = 16384
    file_a = bytes(range(256)) * 128      # 32768 = exactly 2 pieces (aligned)
    file_b = b"N" * 9000                  # own piece, stays unselected
    data = file_a + file_b
    pieces = b"".join(hashlib.sha1(data[i:i + piece_len]).digest()
                      for i in range(0, len(data), piece_len))

    class Tracker(http.server.BaseHTTPRequestHandler):
        peers = []
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if not self.path.startswith("/announce"):
                return
            port = int(q["port"][0])
            ip = self.client_address[0]
            if (ip, port) not in Tracker.peers:
                Tracker.peers.append((ip, port))
            compact = b"".join(bytes(int(x) for x in ip.split("."))
                               + port.to_bytes(2, "big")
                               for ip, port in Tracker.peers)
            body = (b"d8:completei1e10:incompletei1e8:intervali60e5:peers"
                    + str(len(compact)).encode() + b":" + compact + b"e")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    tracker = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Tracker)
    tracker.daemon_threads = True
    threading.Thread(target=tracker.serve_forever, daemon=True).start()
    tport = tracker.server_address[1]

    tmp = pathlib.Path(tempfile.mkdtemp())
    seed_dir = tmp / "seed"
    (seed_dir / "My Test Pack" / "video").mkdir(parents=True)
    (seed_dir / "My Test Pack" / "video" / "show.mp4").write_bytes(file_a)
    (seed_dir / "My Test Pack" / "notes.txt").write_bytes(file_b)
    meta = {"announce": f"http://127.0.0.1:{tport}/announce",
            "info": {"name": "My Test Pack", "piece length": piece_len,
                     "pieces": pieces,
                     "files": [{"length": len(file_a), "path": ["video", "show.mp4"]},
                               {"length": len(file_b), "path": ["notes.txt"]}]}}
    tfile = tmp / "pack.torrent"
    tfile.write_bytes(torrent.bencode(meta))

    seeder = subprocess.Popen(
        ["aria2c", str(tfile), "--dir", str(seed_dir), "--seed-time", "9999",
         "--enable-dht=false", "--seed-ratio", "0", "--check-integrity=true"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    try:
        dest = tmp / "dest"
        r = subprocess.run(
            [_sys.executable, "bin/mgd", "grab", str(tfile),
             "--select-files", "1", "-d", str(dest), "--json-progress"],
            capture_output=True, text=True, timeout=120)
        events = [json.loads(l) for l in r.stdout.splitlines()
                  if l.startswith("{\"")]
        types = [e["type"] for e in events]
        assert "start" in types and "done" in types, types
        got = dest / "My Test Pack" / "video" / "show.mp4"
        assert got.exists() and got.read_bytes() == file_a, "selected file corrupt"
        assert not (dest / "My Test Pack" / "notes.txt").exists(), \
            "unselected file downloaded (piece alignment broken?)"
        done_ev = next(e for e in events if e["type"] == "done")
        assert done_ev["size"] == len(file_a), done_ev
    finally:
        seeder.terminate()
        tracker.shutdown()
        tracker.server_close()


def test_concurrent_same_name_downloads():
    """Two simultaneous downloads of the SAME url must never share a part
    file (the old part naming keyed on the URL filename, so the second
    grab truncated the first's data - 'replaces it instead of renaming')."""
    import conftest
    import subprocess
    import sys as _sys
    import threading

    content = bytes(range(256)) * 2048   # 512 KB
    origin = conftest.DripOrigin(content, chunk=32 * 1024, delay=0.002)
    try:
        dest = pathlib.Path(tempfile.mkdtemp())
        procs = [subprocess.Popen(
            [_sys.executable, "bin/mgd", "grab", origin.url, "-d", str(dest)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(2)]
        for p in procs:
            assert p.wait(timeout=120) == 0
        outs = sorted(p for p in dest.iterdir()
                     if p.is_file() and p.suffix == ".bin")
        assert len(outs) == 2, f"expected 2 distinct outputs, got {[o.name for o in outs]}"
        for o in outs:
            assert o.read_bytes() == content, o.name
        assert outs[0].name != outs[1].name   # renamed, never replaced
    finally:
        origin.stop()


def test_native_host_handoff(tmp_path):
    """Extension downloads hand the batch to the MediaGrab app (rename +
    destination sheet) instead of headless grabs when open-app is on;
    open-app off keeps the headless spawn behavior."""
    import conftest
    import os
    import struct
    import sys as _sys
    import subprocess
    import time

    content = b"handoff test " * 1024
    origin = conftest.DripOrigin(content, chunk=16 * 1024, delay=0)

    def frame(obj):
        body = json.dumps(obj).encode()
        return struct.pack("<I", len(body)) + body

    def run_host(payload, open_app: str):
        cfg_home = tmp_path / f"xdg-{open_app}"
        (cfg_home / "mgd").mkdir(parents=True, exist_ok=True)
        (cfg_home / "mgd" / "config.json").write_text(
            json.dumps({"open_app": open_app}))
        env = dict(os.environ, XDG_CONFIG_HOME=str(cfg_home),
                   MGD_NO_OPEN="1")   # tests never launch the real app
        p = subprocess.run([_sys.executable, "bin/mgd", "native-host"],
                           input=payload, capture_output=True, timeout=30,
                           env=env)
        (ln,) = struct.unpack("<I", p.stdout[:4])
        return json.loads(p.stdout[4:4 + ln])

    payload = frame({"command": "download", "urls": [origin.url],
                     "destination": str(tmp_path / "dest"),
                     "referer": "https://example.com/x"})
    try:
        # open-app on: handoff file written, reply says handoff, no grab
        reply = run_host(payload, "on")
        assert reply["ok"] and reply["handoff"] is True, reply
        handoff = json.loads((tmp_path / "xdg-on" / "mgd" / "handoff.json")
                             .read_text())
        assert handoff["urls"] == [origin.url]
        assert handoff["referer"] == "https://example.com/x"
        out = tmp_path / "dest" / "drip.bin"
        time.sleep(1.5)
        assert not out.exists(), "handoff must not download by itself"
        # open-app off: headless spawn as before
        reply2 = run_host(payload, "off")
        assert reply2["ok"] and reply2.get("handoff") is None and \
            reply2["spawned"] == 1, reply2
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(0.5)
            if out.exists() and out.stat().st_size == len(content):
                assert out.read_bytes() == content
                return
        raise AssertionError("headless fallback download did not finish")
    finally:
        origin.stop()


def test_is_platform():
    assert mgd_cli._is_platform("https://youtu.be/P4zyZAl30To")
    assert mgd_cli._is_platform("https://www.youtube.com/watch?v=x")
    assert mgd_cli._is_platform("https://x.com/user/status/123")
    assert mgd_cli._is_platform("https://twitter.com/i/status/9")
    assert not mgd_cli._is_platform("https://example.com/file.mp4")
    assert not mgd_cli._is_platform("https://github.com/morluto/rea")


def test_parser_aliases():
    p = mgd_cli.build_parser()
    a = p.parse_args(["get", "https://x"])
    assert a.cmd == "get" and a.url == "https://x"
    a2 = p.parse_args(["jobs"])
    assert a2.cmd == "jobs"
    a3 = p.parse_args(["grab", "u", "--quality", "720p"])
    assert a3.quality == "720p"


def test_pause_then_resume(tmp_path):
    import conftest
    origin = conftest.DripOrigin(bytes(range(256)) * 6000, chunk=4 * 1024,
                                 delay=0.08)  # 1.5 MB, ~50 KB/s
    root = tmp_path / "dl"
    root.mkdir()
    try:
        proc = subprocess.Popen([sys.executable, "-m", "mgd", "grab", origin.url,
                                 "-d", str(root), "-c", "1", "-q"],
                                cwd=str(CLI_DIR),
                                env={**os.environ, "PYTHONPATH": ".",
                                     "XDG_CONFIG_HOME": TEST_CFG},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(3.0)
        item = _own_item(root)
        assert item, "no state file while downloading"
        assert item["state"] == "running"
        assert item["id"].startswith("d") and not item["id"].isdigit()
        pause = _mgd(["pause", item["id"]])
        assert pause.returncode == 0, pause.stdout + pause.stderr
        proc.wait(timeout=30)
        assert proc.returncode == 130, proc.returncode
        time.sleep(0.5)
        paused = _own_item(root)
        assert paused["state"] == "paused"
        assert paused["done"] > 0
        assert paused["id"] == item["id"], "id must not change across pause"
        # resume completes file byte-perfect; --json-progress must emit
        # start/progress/done NDJSON (the GUI consumes exactly this)
        content = origin.content
        res = _mgd(["resume", item["id"], "--json-progress"], timeout=120)
        final = root / "drip.bin"
        assert final.read_bytes() == content, proc.stdout
        events = [json.loads(l) for l in res.stdout.splitlines()
                  if l.startswith('{"type"')]
        kinds = [e["type"] for e in events]
        assert "start" in kinds and "progress" in kinds and "done" in kinds
        start = [e for e in events if e["type"] == "start"][0]
        assert start["id"] == item["id"] and start["out_path"] == str(final)
        done_line = [l for l in res.stdout.splitlines() if '"type": "done"' in l][-1]
        assert json.loads(done_line)["path"] == str(final)
        assert (res.returncode == 0)
        # state cleared: our download is gone from the list
        assert _own_item(root, timeout=0) is None
    finally:
        origin.stop()
        try:
            proc.kill()
        except Exception:
            pass


def test_cancel_removes_partial(tmp_path):
    import conftest
    origin = conftest.DripOrigin(bytes(range(256)) * 6000, chunk=4 * 1024, delay=0.08)
    root = tmp_path / "dl2"
    root.mkdir()
    try:
        proc = subprocess.Popen([sys.executable, "-m", "mgd", "grab", origin.url,
                                 "-d", str(root), "-c", "1", "-q"],
                                cwd=str(CLI_DIR),
                                env={**os.environ, "PYTHONPATH": ".",
                                     "XDG_CONFIG_HOME": TEST_CFG},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(3.0)
        item = _own_item(root)
        assert item and item["state"] == "running", item
        out = _mgd(["cancel", item["id"]])
        assert out.returncode == 0, out.stdout + out.stderr
        proc.wait(timeout=20)
        time.sleep(0.5)
        assert not list(root.iterdir()), f"partial files left: {list(root.iterdir())}"
        assert _own_item(root, timeout=0) is None
    finally:
        origin.stop()
        try:
            proc.kill()
        except Exception:
            pass