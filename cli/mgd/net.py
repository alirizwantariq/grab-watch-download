"""HTTP(S) connections with optional proxy support (stdlib only).

Supports:
  - direct connections
  - HTTP proxies: forward proxy for http:// targets, CONNECT tunnel for https://
  - SOCKS5 proxies (no auth, or username:password auth) for http:// and https://
"""
from __future__ import annotations

import http.client
import socket
import ssl
import urllib.parse as u

DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) mgd/1.0"
DEFAULT_TIMEOUT = 30.0


class ProxyError(Exception):
    pass


def parse_proxy(proxy_url: str):
    """-> (scheme, host, port, username, password)"""
    if proxy_url.startswith("socks5://"):
        scheme = "socks5"
    elif proxy_url.startswith("http://"):
        scheme = "http"
    elif proxy_url.startswith("https://"):
        scheme = "https"  # https-to-https proxy means TLS-wrapped CONNECT
    else:
        raise ProxyError(f"unsupported proxy URL: {proxy_url}")
    p = u.urlsplit(proxy_url)
    if not p.hostname:
        raise ProxyError(f"proxy URL missing host: {proxy_url}")
    port = p.port or (1080 if scheme == "socks5" else 8080)
    return scheme, p.hostname, port, p.username, p.password


def _tls_wrap(sock, host: str, server_hostname: str | None = None) -> socket.socket:
    ctx = ssl.create_default_context()
    ssl_hostname = server_hostname or host
    return ctx.wrap_socket(sock, server_hostname=ssl_hostname if ssl_hostname else None)


class Socks5Connection:
    """Raw TCP socket that negotiated SOCKS5 CONNECT with a target host:port."""

    @staticmethod
    def connect(host: str, port: int, proxy: tuple, timeout: float) -> socket.socket:
        _scheme, phost, pport, user, pwd = proxy
        sock = socket.create_connection((phost, pport), timeout=timeout)
        try:
            # greeting: offer no-auth and user/pass
            sock.sendall(b"\x05\x02\x00\x02")
            resp = _recv_exact(sock, 2)
            if len(resp) != 2 or resp[0] != 5:
                raise ProxyError(f"bad SOCKS5 greeting: {resp!r}")
            if resp[1] == 2:
                if not user:
                    raise ProxyError("SOCKS5 proxy requires auth, no user given")
                sub = b"\x01" + bytes([len(user)]) + user.encode() + bytes([len(pwd or "")]) + (pwd or "").encode()
                sock.sendall(sub)
                arep = _recv_exact(sock, 2)
                if arep[1] != 0:
                    raise ProxyError("SOCKS5 auth failed")
            elif resp[1] != 0:
                raise ProxyError(f"SOCKS5 proxy rejected auth method {resp[1]}")
            # request CONNECT (domain name)
            addr = host.encode()
            req = b"\x05\x01\x00\x03" + bytes([len(addr)]) + addr + int(port).to_bytes(2, "big")
            sock.sendall(req)
            rep = _recv_exact(sock, 4)
            if rep[3] == 1:
                _recv_exact(sock, 4 + 2)  # ipv4 + port
            elif rep[3] == 3:
                ln = _recv_exact(sock, 1)[0]
                _recv_exact(sock, ln + 2)
            elif rep[3] == 4:
                _recv_exact(sock, 16 + 2)
            else:
                _recv_exact(sock, 12)
            if rep[1] != 0:
                codes = {1: "general failure", 2: "not allowed", 3: "network unreachable",
                         4: "host unreachable", 5: "connection refused",
                         6: "TTL expired", 7: "command not supported", 8: "address type not supported"}
                raise ProxyError(f"SOCKS5 CONNECT failed: {codes.get(rep[1], rep[1])}")
            return sock
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise


def _recv_exact(sock, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ProxyError("proxy closed connection mid-handshake")
        data += chunk
    return data


class DirectHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port=None, timeout=DEFAULT_TIMEOUT, **kw):
        self.rw_timeout = timeout
        super().__init__(host, port, timeout=timeout, **kw)
        self.default_port = 80


class DirectHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port=None, timeout=DEFAULT_TIMEOUT, **kw):
        self.rw_timeout = timeout
        super().__init__(host, port, timeout=timeout, **kw)
        self.default_port = 443


class HttpProxyHTTPConnection(DirectHTTPConnection):
    """http:// target through an HTTP forward proxy (absolute-form request line)."""

    def __init__(self, proxy: tuple, host, port=None, timeout=DEFAULT_TIMEOUT):
        super().__init__(proxy[1], proxy[2], timeout=timeout)
        _s, self._user, self._pwd = proxy[0], proxy[3], proxy[4]
        self._target = f"{host}:{port or 80}"


class HttpProxyHTTPSConnection(DirectHTTPSConnection):
    """https:// target through an HTTP proxy via CONNECT tunnel."""

    def __init__(self, proxy: tuple, host, port=None, timeout=DEFAULT_TIMEOUT):
        super().__init__(proxy[1], proxy[2], timeout=timeout)
        self._target_host, self._target_port = host, port or 443
        self._user, self._pwd = proxy[3], proxy[4]

    def connect(self):
        self.sock = socket.create_connection((self._tunnel_host or self.host, self.port), timeout=self.timeout)
        self._tunnel()
        self.sock = _tls_wrap(self.sock, self._target_host)


class HttpsProxyHTTPSConnection(DirectHTTPSConnection):
    """https:// target through a TLS-wrapped HTTP proxy via CONNECT."""

    def __init__(self, proxy: tuple, host, port=None, timeout=DEFAULT_TIMEOUT):
        super().__init__(proxy[1], proxy[2], timeout=timeout)
        self._target_host, self._target_port = host, port or 443

    def connect(self):
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock = _tls_wrap(raw, self.host)
        self._tunnel()
        self.sock = _tls_wrap(self.sock, self._target_host)


def _proxy_auth_header(user, pwd) -> str:
    import base64
    token = base64.b64encode(f"{user}:{pwd or ''}".encode()).decode()
    return f"Basic {token}"


def make_connection(url: str, proxy: str | None, timeout: float = DEFAULT_TIMEOUT):
    """Create an http.client connection appropriate for url and proxy.
    Caller must set Host and, for plain-http-through-proxy, the absolute request URI."""
    p = u.urlsplit(url)
    host = p.hostname
    port = p.port
    if not host:
        raise ValueError(f"URL has no host: {url}")
    if not proxy:
        if p.scheme == "http":
            return DirectHTTPConnection(host, port, timeout=timeout), False
        if p.scheme == "https":
            return DirectHTTPSConnection(host, port, timeout=timeout), False
        raise ValueError(f"unsupported scheme: {p.scheme}")
    parsed = parse_proxy(proxy)
    if parsed[0] == "socks5":
        # emulate a direct connection over a SOCKS5 socket
        if p.scheme == "http":
            conn = DirectHTTPConnection(host, port, timeout=timeout)
            conn.connect = lambda: _socks_connect(conn, host, port or 80, parsed, timeout, tls=False)  # noqa: E731
        elif p.scheme == "https":
            conn = DirectHTTPSConnection(host, port, timeout=timeout)
            conn.connect = lambda: _socks_connect(conn, host, port or 443, parsed, timeout, tls=True)  # noqa: E731
        else:
            raise ValueError(f"unsupported scheme: {p.scheme}")
        return conn, False
    if p.scheme == "http" and parsed[0] == "http":
        return HttpProxyHTTPConnection(parsed, host, port, timeout=timeout), True
    if p.scheme == "https" and parsed[0] == "http":
        conn = HttpProxyHTTPSConnection(parsed, host, port, timeout=timeout)
        conn.set_tunnel(host, port or 443)
        return conn, False
    if p.scheme == "https" and parsed[0] == "https":
        conn = HttpsProxyHTTPSConnection(parsed, host, port, timeout=timeout)
        conn.set_tunnel(host, port or 443)
        return conn, False
    raise ProxyError(f"proxy {parsed[0]}:// cannot serve {p.scheme}:// target")


def _socks_connect(conn, host, port, proxy, timeout, tls: bool):
    sock = Socks5Connection.connect(host, port, proxy, timeout)
    if tls:
        conn.sock = _tls_wrap(sock, host)
    else:
        conn.sock = sock


def _close_quietly(conn) -> None:
    if conn is None:
        return
    try:
        if hasattr(conn, "close"):
            conn.close()
    except OSError:
        pass


def open_request(url: str, proxy: str | None, method: str = "GET",
                 headers: dict | None = None, timeout: float = DEFAULT_TIMEOUT,
                 range_start: int | None = None, range_end: int | None = None,
                 if_range: str | None = None, follow: int = 8):
    """Send method request to url (handles proxy absolute-form for http) and
    follows up to `follow` redirects (Location may be relative). Returns
    (conn, response); response.final_url is the URL the response actually
    came from."""
    seen: set[str] = set()
    current = url
    for hop in range(follow + 1):
        conn, resp = _open_request_once(current, proxy, method, headers,
                                        timeout, range_start, range_end, if_range)
        status = resp.status
        if 300 <= status < 400 and hop < follow and status != 304:
            loc = resp.getheader("Location")
            if loc:
                import urllib.parse as u2
                new_url = u2.urljoin(current, loc)
                _close_quietly(conn)
                if new_url in seen:
                    resp.final_url = current  # redirect loop; serve as-is
                    return conn, resp
                seen.add(new_url)
                current = new_url
                continue
        resp.final_url = current  # attribute for callers
        return conn, resp
    resp.final_url = current
    return conn, resp


def _open_request_once(url: str, proxy: str | None, method: str = "GET",
                       headers: dict | None = None, timeout: float = DEFAULT_TIMEOUT,
                       range_start: int | None = None, range_end: int | None = None,
                       if_range: str | None = None):
    """Send method request to url (handles proxy absolute-form for http). Returns (conn, response)."""
    p = u.urlsplit(url)
    conn, via_forward_proxy = make_connection(url, proxy, timeout=timeout)
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    if via_forward_proxy:
        path = f"{p.scheme}://{p.netloc}{path}"
    h = {
        "User-Agent": DEFAULT_UA,
        "Accept-Encoding": "identity",
        "Host": p.netloc,
    }
    if p.username:  # https://user:pass@host/file
        import base64
        h["Authorization"] = "Basic " + base64.b64encode(
            f"{u.unquote(p.username)}:{u.unquote(p.password or '')}".encode()).decode()
    if headers:
        h.update(headers)
    if range_start is not None:
        if range_end is None:
            h["Range"] = f"bytes={range_start}-"
        else:
            h["Range"] = f"bytes={range_start}-{range_end}"
        if if_range:
            h["If-Range"] = if_range
    conn.request(method, path, headers=h)
    resp = conn.getresponse()
    if resp.getheader("Content-Encoding") == "gzip":
        import gzip
        wrapped = gzip.GzipFile(fileobj=resp)  # rare given identity; defensive only
        resp.read_all = wrapped.read  # pragma: no cover
    return conn, resp


class HttpError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(f"HTTP {status}{': ' + message if message else ''}")
        self.status = status