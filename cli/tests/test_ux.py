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