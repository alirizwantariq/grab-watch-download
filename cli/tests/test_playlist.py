"""`mgd playlist URL`: a playlist's or channel's videos for the app's picker.

yt-dlp is replaced by a small fake that records how it was called and
prints a flat-playlist document shaped like YouTube's.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

CLI_DIR = pathlib.Path(__file__).parent.parent

FLAT = {
    "_type": "playlist",
    "title": "Channel - Videos",
    "uploader": "Channel",
    "playlist_count": 40,
    "entries": [
        {"_type": "url", "ie_key": "Youtube", "id": "vid00000001",
         "url": "https://www.youtube.com/watch?v=vid00000001", "title": "First video",
         "duration": 61.0, "channel": "Channel",
         "thumbnails": [{"url": "https://i.ytimg.com/vi/vid00000001/small.jpg", "width": 120},
                        {"url": "https://i.ytimg.com/vi/vid00000001/mq.jpg", "width": 320},
                        {"url": "https://i.ytimg.com/vi/vid00000001/hq.jpg", "width": 480}]},
        {"_type": "url", "ie_key": "Youtube", "id": "private0001",
         "url": "https://www.youtube.com/watch?v=private0001", "title": "[Private video]"},
        # a nested list (a channel's tabs) is flattened in order
        {"_type": "playlist", "title": "Shorts", "entries": [
            {"_type": "url", "ie_key": "Youtube", "id": "short000001",
             "url": "https://www.youtube.com/shorts/short000001", "title": "A short",
             "duration": 30, "live_status": "not_live"},
        ]},
        # no url: built from the id
        {"_type": "url", "ie_key": "Youtube", "id": "noUrl000001", "title": "No url",
         "live_status": "is_upcoming"},
        # a link to another tab is not a video
        {"_type": "url", "ie_key": "YoutubeTab", "url": "https://www.youtube.com/@chan/streams"},
        # a repeat
        {"_type": "url", "ie_key": "Youtube", "id": "vid00000001",
         "url": "https://www.youtube.com/watch?v=vid00000001", "title": "First video"},
        {"_type": "url", "ie_key": "Youtube", "id": "last0000001",
         "url": "https://www.youtube.com/watch?v=last0000001", "title": "Last",
         "thumbnail": "https://i.ytimg.com/vi/last0000001/default.jpg"},
    ],
}


def _fake_ytdlp(tmp: pathlib.Path, fail: bool = False) -> pathlib.Path:
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fixture = tmp / "flat.json"
    fixture.write_text(json.dumps(FLAT))
    script = bin_dir / "yt-dlp"
    script.write_text(f"""#!/usr/bin/env python3
import json, sys
with open({str(tmp / 'calls.jsonl')!r}, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if {fail!r}:
    print("ERROR: This playlist does not exist", file=sys.stderr)
    sys.exit(1)
sys.stdout.write(open({str(fixture)!r}).read())
""")
    script.chmod(0o755)
    return bin_dir


def _playlist(tmp: pathlib.Path, *args: str, fail: bool = False):
    bin_dir = _fake_ytdlp(tmp, fail=fail)
    env = dict(os.environ, PYTHONPATH=".", XDG_CONFIG_HOME=str(tmp / "cfg"),
               MGD_ROOT=str(tmp / "root"), MGD_NO_OPEN="1",
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
    proc = subprocess.run([sys.executable, "bin/mgd", "playlist", *args], cwd=str(CLI_DIR),
                          capture_output=True, text=True, timeout=60, env=env)
    calls = [json.loads(line) for line in (tmp / "calls.jsonl").read_text().splitlines()] \
        if (tmp / "calls.jsonl").exists() else []
    return proc, json.loads(proc.stdout or "{}"), calls


def test_playlist_lists_the_videos_in_order(tmp_path):
    proc, out, calls = _playlist(tmp_path, "https://www.youtube.com/@chan")
    assert proc.returncode == 0, proc.stderr
    # a channel's main page means its videos
    assert out["url"] == "https://www.youtube.com/@chan/videos"
    call = calls[0]
    assert call[-1] == "https://www.youtube.com/@chan/videos", call
    assert "--flat-playlist" in call and "--yes-playlist" in call and "--no-playlist" not in call
    assert call[call.index("--playlist-end") + 1] == "501"
    assert out["title"] == "Channel - Videos" and out["uploader"] == "Channel"
    assert out["count"] == 40 and out["truncated"] is False
    ids = [i["id"] for i in out["items"]]
    assert ids == ["vid00000001", "short000001", "noUrl000001", "last0000001"], ids
    first, short, no_url, last = out["items"]
    assert first["duration"] == 61.0 and first["uploader"] == "Channel"
    assert first["thumbnail"].endswith("/mq.jpg")             # smallest >= 240 px
    assert short["url"] == "https://www.youtube.com/shorts/short000001"
    assert no_url["url"] == "https://www.youtube.com/watch?v=noUrl000001"
    assert no_url["live"] is True and first["live"] is False
    assert no_url["duration"] is None
    assert last["thumbnail"].endswith("/default.jpg")
    root = tmp_path / "root"
    assert out["folders"] == {"video": str(root / "video"), "audio": str(root / "audio")}


def test_playlist_limit_and_plain_playlist_links(tmp_path):
    url = "https://www.youtube.com/playlist?list=PLabc"
    proc, out, calls = _playlist(tmp_path, url, "--limit", "2")
    assert proc.returncode == 0, proc.stderr
    assert calls[0][-1] == url                                # not rewritten
    assert calls[0][calls[0].index("--playlist-end") + 1] == "3"
    assert [i["id"] for i in out["items"]] == ["vid00000001", "short000001"]
    assert out["truncated"] is True


def test_playlist_that_cannot_be_read(tmp_path):
    proc, out, calls = _playlist(tmp_path, "https://www.youtube.com/playlist?list=PLgone",
                                 fail=True)
    assert proc.returncode == 1
    assert out["items"] == [] and "could not read this playlist" in out["error"]
    assert len(calls) >= 2      # every strategy was tried
