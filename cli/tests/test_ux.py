import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

import mgd
from mgd import cli as mgd_cli

CLI_DIR = pathlib.Path(__file__).parent.parent
TEST_CFG = tempfile.mkdtemp(prefix="mgdtest-cfg")


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
    p = subprocess.run([_sys.executable, "bin/mgd", "native-host"],
                       input=payload, capture_output=True, timeout=30)
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

        def frame(obj):
            body = json.dumps(obj).encode()
            return struct.pack("<I", len(body)) + body

        # without referer: fails 403
        payload = frame({"command": "download", "urls": [url],
                         "destination": str(dest)})
        p = subprocess.run([_sys.executable, "bin/mgd", "native-host"],
                           input=payload, capture_output=True, timeout=30)
        (ln,) = struct.unpack("<I", p.stdout[:4])
        assert json.loads(p.stdout[4:4 + ln])["spawned"] == 1
        out = dest / "video.mp4"
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(0.5)
            if out.exists():
                raise AssertionError("download should have 403'd without referer")
        # with referer: completes
        payload = frame({"command": "download", "urls": [url],
                         "destination": str(dest), "referer": PAGE})
        p = subprocess.run([_sys.executable, "bin/mgd", "native-host"],
                           input=payload, capture_output=True, timeout=30)
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
        listed = _mgd(["list", "--json"])
        items = json.loads(listed.stdout).get("items", [])
        assert items, "no state file while downloading"
        assert items[0]["state"] == "running"
        pause = _mgd(["pause", "1"])
        assert pause.returncode == 0, pause.stdout + pause.stderr
        proc.wait(timeout=30)
        time.sleep(1.0)
        listed2 = _mgd(["list", "--json"])
        item = json.loads(listed2.stdout)["items"][0]
        assert item["state"] == "paused"
        assert item["done"] > 0
        # resume completes file byte-perfect; --json-progress must emit
        # start/progress/done NDJSON (the GUI consumes exactly this)
        content = origin.content
        res = _mgd(["resume", "1", "--json-progress"], timeout=120)
        final = root / "drip.bin"
        assert final.read_bytes() == content, proc.stdout
        kinds = [json.loads(l)["type"] for l in res.stdout.splitlines()
                 if l.startswith('{"type"')]
        assert "start" in kinds and "progress" in kinds and "done" in kinds
        done_line = [l for l in res.stdout.splitlines() if '"done"' in l][-1]
        assert json.loads(done_line)["path"] == str(final)
        assert (res.returncode == 0)
        # list now empty (state cleared)
        listed3 = _mgd(["list", "--json"])
        assert not json.loads(listed3.stdout)["items"]
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
        out = _mgd(["cancel", "1"])
        assert out.returncode == 0, out.stdout + out.stderr
        proc.wait(timeout=20)
        time.sleep(0.5)
        assert not list(root.iterdir()), f"partial files left: {list(root.iterdir())}"
        listed = _mgd(["list", "--json"])
        assert not json.loads(listed.stdout)["items"]
    finally:
        origin.stop()
        try:
            proc.kill()
        except Exception:
            pass