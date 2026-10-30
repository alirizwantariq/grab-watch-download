"""Shared test fixtures: a range-aware HTTP origin with failure injection,
and a trivial HTTP forward proxy.
"""
from __future__ import annotations

import hashlib
import http.client
import os
import socket
import socketserver
import threading
import urllib.parse as u


class RangeOriginHandler:
    """Handler class factory: serves `content` with Range support, optional
    first-N-attempts connection drops after `drop_after` bytes."""

    def make_handler(content: bytes, plan: dict | None = None):
        state = {"attempts": 0}
        served_ranges: list[list[int]] = []
        lock = threading.Lock()
        plan = plan or {}

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # silence
                pass

            def _record(self, s, e):
                with lock:
                    served_ranges.append([s, e])

            def do_GET(self):
                rng = self.headers.get("Range", "")
                total = len(content)
                if rng.startswith("bytes="):
                    spec = rng[6:].split(",", 1)[0]
                    if "-" in spec:
                        first, last = spec.split("-", 1)
                        first = int(first) if first else total - int(last or 1)
                        last = int(last) if last else total - 1
                        last = min(last, total - 1)
                        if first > last or first >= total:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{total}")
                            self.end_headers()
                            return
                        self._record(first, last)
                        body = content[first:last + 1]
                        with lock:
                            state["attempts"] += 1
                            attempt = state["attempts"]
                        drop_after = plan.get("drop_after") if attempt <= plan.get("fail_attempts", 0) else None
                        self.send_response(206)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("ETag", '"test-etag"')
                        self.send_header("Content-Range", f"bytes {first}-{last}/{total}")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        if drop_after is not None:
                            self.wfile.write(body[:drop_after])
                            self.wfile.flush()
                            try:
                                self.connection.close()
                            except OSError:
                                pass
                            self.close_connection = True
                            return
                        self.wfile.write(body)
                        return
                self._record(0, total - 1)
                with lock:
                    state["attempts"] += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("ETag", '"test-etag"')
                self.send_header("Content-Length", str(total))
                self.end_headers()
                self.wfile.write(content)

        Handler.served_ranges = served_ranges
        return Handler


import http.server  # noqa: E402  (after class factory definition)


class Origin:
    def __init__(self, content: bytes, plan: dict | None = None):
        self.content = content
        handler = RangeOriginHandler.make_handler(content, plan)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/data.bin"

    @property
    def served_ranges(self):
        return self.server.RequestHandlerClass.served_ranges

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ForwardProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).seen.append(self.path)
        # absolute-form request URI
        origin = u.urlsplit(self.path)
        origin_url = f"{origin.scheme}://{origin.netloc}{origin.path}"
        if origin.query:
            origin_url += "?" + origin.query
        req_headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "proxy-connection", "connection")}
        conn = http.client.HTTPConnection(origin.hostname, origin.port or 80, timeout=20)
        conn.request("GET", origin_url + "", headers=req_headers)
        resp = conn.getresponse()
        body = resp.read()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in ("content-length", "content-range", "etag", "content-type"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        conn.close()


class ProxyOrigin:
    def __init__(self):
        ForwardProxyHandler.seen = []

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), ForwardProxyHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def seen(self):
        return ForwardProxyHandler.seen

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class Socks5ProxyServer:
    """Minimal RFC1928 SOCKS5 CONNECT proxy (no auth / user-pass), for tests."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.connects: list[str] = []
        self.lock = threading.Lock()

    @property
    def url(self):
        return f"socks5://127.0.0.1:{self.port}"

    def _serve(self):
        import select
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        try:
            head = client.recv(2)
            if len(head) != 2 or head[0] != 5:
                client.sendall(b"\x05\xff"); client.close(); return
            n = head[1]
            methods = b""
            while len(methods) < n:
                got = client.recv(n - len(methods))
                if not got:
                    client.close(); return
                methods += got
            if b"\x00" not in methods:
                client.sendall(b"\x05\xff"); client.close(); return
            client.sendall(b"\x05\x00")  # choose no-auth
            head = client.recv(4)
            if head[:3] != b"\x05\x01\x00":
                client.close(); return
            atyp, alen = head[3], client.recv(1)[0]
            if atyp == 1:
                host = ".".join(str(b) for b in client.recv(alen))
            elif atyp == 3:
                host = client.recv(alen).decode()
            else:
                client.close(); return
            port_b = client.recv(2)
            port = int.from_bytes(port_b, "big")
            with self.lock:
                self.connects.append(f"{host}:{port}")
            try:
                remote = socket.create_connection((host, port), timeout=10)
            except OSError:
                client.sendall(b"\x05\x04\x00\x01" + bytes(6)); client.close(); return
            client.sendall(b"\x05\x00\x00\x01" + bytes(6))
            import select
            socks = [client, remote]
            while True:
                r, _, x = select.select(socks, [], socks, 5)
                if x:
                    break
                if not r:
                    break
                for s in r:
                    other = remote if s is client else client
                    d = s.recv(65536)
                    if not d:
                        raise ConnectionError
                    other.sendall(d)
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def stop(self):
        try:
            self.sock.close()
        except OSError:
            pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DripOrigin:
    """Range-capable origin that trickles data (for pause/kill tests)."""

    def __init__(self, content: bytes, chunk: int = 16 * 1024, delay: float = 0.05):
        self.content = content
        self.chunk = chunk
        self.delay = delay

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                rng = self.headers.get("Range", "")
                total = handler_content.len
                if rng.startswith("bytes="):
                    spec = rng[6:].split(",", 1)[0]
                    first, _, last = spec.partition("-")
                    first = int(first)
                    last = int(last) if last else total - 1
                    body = content[first:last + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {first}-{last}/{total}")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                else:
                    body = content
                    self.send_response(200)
                    self.send_header("Content-Length", str(total))
                    self.end_headers()
                for i in range(0, len(body), handler_content.chunk):
                    self.wfile.write(body[i:i + handler_content.chunk])
                    self.wfile.flush()
                    import time as _t
                    _t.sleep(handler_content.delay)

        handler_content = type("H", (), {"len": len(content), "chunk": chunk,
                                         "delay": delay})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/drip.bin"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()