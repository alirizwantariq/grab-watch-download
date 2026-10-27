import mgd
from mgd import categorize, grab, engine, cli, net
import conftest
import json
import os
import pathlib


def test_categorize_extensions():
    assert categorize.categorize("movie.mp4") == "video"
    assert categorize.categorize("song.mp3") == "audio"
    assert categorize.categorize("photo.PNG") == "image"
    assert categorize.categorize("ball.zip") == "archive"
    assert categorize.categorize("paper.pdf") == "document"
    assert categorize.categorize("setup.exe") == "software"
    assert categorize.categorize("subs.srt") == "subtitle"
    assert categorize.categorize("weird.unknown") == "other"


def test_category_dir_and_unique(tmp_path):
    cfg = {"root": str(tmp_path)}
    d = categorize.category_dir(cfg, "video")
    assert d.is_dir() and d.name == "video"
    d2 = categorize.category_dir(cfg, "video")
    assert d2 == d
    assert categorize.CATEGORIES[0] == "video"


def test_unique_path(tmp_path):
    p = categorize.unique_path(tmp_path, "a.bin")
    p.write_bytes(b"x")
    p2 = categorize.unique_path(tmp_path, "a.bin")
    assert p2.name == "a (1).bin"
    p2.write_bytes(b"y")
    p3 = categorize.unique_path(tmp_path, "a.bin")
    assert p3.name == "a (2).bin"


def test_extract_page_media():
    html = """<html><body>
      <video src="/media/big-video.mp4" poster="/p.jpg"></video>
      <source type="audio/mpeg" src="//cdn.example.net/song.mp3">
      <meta property="og:video:secure_url" content="https://m.example.com/watch/clip.mp4">
      <a href="/files/report.pdf">the report</a>
      <a href="page2.html">not a file</a>
    </body></html>"""
    links = grab.extract_links(html, "https://www.example.com/watch/")
    urls = [l["url"] for l in links]
    assert "https://www.example.com/media/big-video.mp4" in urls
    assert "https://cdn.example.net/song.mp3" in urls
    assert "https://m.example.com/watch/clip.mp4" in urls
    assert "https://www.example.com/files/report.pdf" in urls
    assert not any(u.endswith("page2.html") for u in urls)
    kinds = {l["kind"] for l in links}
    assert kinds >= {"video", "source", "meta"}


def test_pick_candidate():
    links = [{"kind": "link-file", "url": "https://x/w.pdf", "title": ""},
             {"kind": "source", "url": "https://x/v.mp4", "title": ""}]
    assert grab.pick_media_candidate(links)["url"] == "https://x/v.mp4"


def test_net_proxy_url_parse():
    assert net.parse_proxy("socks5://user:pw@127.0.0.1:1080")[0] == "socks5"
    assert net.parse_proxy("http://127.0.0.1:8899")[1] == "127.0.0.1"
    assert net.parse_proxy("http://127.0.0.1")[2] == 8080


def test_download_through_forward_proxy(tmp_path):
    import random as _r
    content = _r.randbytes(1_500_000)
    origin = conftest.Origin(content)
    proxy = conftest.ProxyOrigin()
    try:
        out = tmp_path / "via_proxy.bin"
        dl = engine.Downloader(origin.url, out, proxy=proxy.url, conns=3)
        dl.run()
        assert out.read_bytes() == content
        assert any("/data.bin" in s for s in proxy.seen)
    finally:
        origin.stop()
        proxy.stop()


def test_download_through_socks5_proxy(tmp_path):
    import random as _r
    content = _r.randbytes(900_000)
    origin = conftest.Origin(content)
    proxy = conftest.Socks5ProxyServer()
    try:
        out = tmp_path / "via_socks.bin"
        dl = engine.Downloader(origin.url, out, proxy=proxy.url, conns=2)
        dl.run()
        assert out.read_bytes() == content
        assert any(f"127.0.0.1:{origin.port}" in s for s in proxy.connects)
    finally:
        origin.stop()
        proxy.stop()


def test_cli_grab_end_to_end(tmp_path):
    import subprocess
    import random as _r
    content = _r.randbytes(1_200_000)
    origin = conftest.Origin(content)
    try:
        outdir = tmp_path / "downloads"
        cmd = [os.sys.executable, "-m", "mgd", "grab", origin.url,
               "-d", str(outdir), "--conns", "3", "-r", "4", "-q"]
        proc = subprocess.run(cmd, cwd=str(pathlib.Path(__file__).parent.parent),
                              capture_output=True, text=True, timeout=120, env={**os.environ, "PYTHONPATH": "."})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        files = list(outdir.iterdir())
        assert len(files) == 1
        assert files[0].read_bytes() == content
        assert proc.stdout.startswith("> "), f"unexpected output: {proc.stdout!r}"
    finally:
        origin.stop()