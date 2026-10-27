import importlib.util
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


def _pkg(name):
    spec = importlib.util.spec_from_file_location(
        name, str(pathlib.Path(__file__).parent.parent / "mgd" / f"{name}.py"))
    return spec


# simple import path: tests import the package directly
import mgd  # noqa: E402,F401
from mgd import engine, net  # noqa: E402

import conftest  # noqa: E402
import http.server  # noqa: E402,F401
import random  # noqa: E402
import pathlib  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402


def test_basic_segmented_download(tmp_path):
    content = random.randbytes(5 * 1024 * 1024)
    origin = conftest.Origin(content)
    try:
        out = tmp_path / "out" / "test.bin"
        dl = engine.Downloader(origin.url, out, conns=4)
        result = dl.run()
        assert out.read_bytes() == content
        assert result.size == len(content)
        assert engine.STATE_SUFFIX not in out.name
        # ranges were served piecemeal (segmented, more than one)
        assert len(origin.served_ranges) > 1
    finally:
        origin.stop()


def test_resume(tmp_path):
    content = random.randbytes(4 * 1024 * 1024)
    origin = conftest.Origin(content)
    try:
        out = tmp_path / "res.bin"
        stopper = engine.Stopper()
        dl = engine.Downloader(origin.url, out, conns=2, stopper=stopper)
        result_box = {}

        def run():
            try:
                dl.run()
                result_box["done"] = True
            except engine.Interrupted:
                result_box["interrupted"] = True

        t = threading.Thread(target=run, daemon=True)
        t.start()
        import time
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and dl._done_bytes < 1:
            time.sleep(0.05)
        assert dl._done_bytes >= 1, "download produced no bytes before stop"
        stopper.stop()
        t.join(timeout=10)
        assert result_box.get("interrupted") or result_box.get("done")
        if result_box.get("done"):
            return  # finished too fast to test resume
        state_bytes = pathlib.Path(str(out) + engine.STATE_SUFFIX).read_bytes()
        part = pathlib.Path(str(out) + engine.PART_SUFFIX)
        assert part.exists()
        before = dl._done_bytes
        assert before > 0

        dl2 = engine.Downloader(origin.url, out, conns=2,
                                resume_state=state_bytes)
        result = dl2.run()
        assert out.read_bytes() == content
        assert result.resumed
        assert not pathlib.Path(str(out) + engine.STATE_SUFFIX).exists()
    finally:
        origin.stop()


def test_retry_on_dropped_connections(tmp_path):
    content = random.randbytes(2 * 1024 * 1024)
    plan = {"fail_attempts": 3, "drop_after": 100 * 1024}
    origin = conftest.Origin(content, plan=plan)
    try:
        out = tmp_path / "retry.bin"
        dl = engine.Downloader(origin.url, out, conns=3, retries=10)
        dl.run()
        assert out.read_bytes() == content
        # multiple attempts happened (drops were injected)
        attempt_count = len(origin.served_ranges)
        assert attempt_count > 1
    finally:
        origin.stop()


def test_unsupported_range_streaming(tmp_path):
    content = random.randbytes(700_000)

    import http.server  # server module already imported above; class below

    class NoRange(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NoRange)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = tmp_path / "stream.bin"
        dl = engine.Downloader(f"http://127.0.0.1:{srv.server_address[1]}/x.bin", out, conns=2)
        dl.run()
        assert out.read_bytes() == content
    finally:
        srv.shutdown()
        srv.server_close()


def test_sha256_verification(tmp_path):
    content = random.randbytes(512 * 1024)
    origin = conftest.Origin(content)
    try:
        out = tmp_path / "sha.bin"
        dl = engine.Downloader(origin.url, out, conns=2,
                               expected_sha256=conftest.sha(content))
        result = dl.run()
        assert result.sha256 == conftest.sha(content)

        out2 = tmp_path / "sha_bad.bin"
        dl2 = engine.Downloader(origin.url, out2, conns=2,
                                expected_sha256="0" * 64)
        try:
            dl2.run()
            raise AssertionError("should have failed sha verification")
        except engine.DownloadError as err:
            assert "SHA-256 mismatch" in str(err)
    finally:
        origin.stop()


def test_pause_save_state_file(tmp_path):
    content = random.randbytes(4 * 1024 * 1024)
    origin = conftest.Origin(content)
    try:
        out = tmp_path / "p.bin"
        dl = engine.Downloader(origin.url, out, conns=2)
        try:
            import threading, time
            stopper = engine.Stopper()
            dl.stopper = stopper
            t = threading.Thread(target=dl.run, daemon=True)
            t.start()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and dl._done_bytes == 0:
                time.sleep(0.02)
            dl.stopper.stop()
            t.join(timeout=10)
        except engine.Interrupted:
            pass
        try:
            state = json.loads(engine.pathlib.Path(str(out) + engine.STATE_SUFFIX).read_text())
        except FileNotFoundError:
            return  # completed before stop
        assert state["kind"] == "mgd-download-state"
        assert state["url"] == origin.url
    finally:
        origin.stop()


def test_redirect_follow(tmp_path):
    import http.server
    import threading
    content = random.randbytes(1_300_000)
    origin = conftest.Origin(content)

    class Redirect(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", origin.url)
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = tmp_path / "red.bin"
        dl = engine.Downloader(f"http://127.0.0.1:{srv.server_address[1]}/x.bin", out, conns=4)
        dl.run()
        assert out.read_bytes() == content
    finally:
        srv.shutdown()
        srv.server_close()
        origin.stop()
