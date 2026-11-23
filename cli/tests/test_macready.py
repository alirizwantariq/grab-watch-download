"""Mac-ready videos: every finished download plays smoothly on a Mac.

Each test makes a small real video with ffmpeg (skipped when ffmpeg is not
installed) and checks what is done to it: nothing, a lossless rewrap, an
audio-only conversion, or a hardware re-encode - and that the original is
never lost.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time

import conftest
from mgd import macready

CLI_DIR = pathlib.Path(__file__).parent.parent
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _env(tmp_path, **extra):
    env = dict(os.environ, PYTHONPATH=".", XDG_CONFIG_HOME=str(tmp_path / "cfg"),
               MGD_ROOT=str(tmp_path / "root"), MGD_NO_OPEN="1")
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _mgd(tmp_path, args, timeout=180, **extra):
    return subprocess.run([sys.executable, "bin/mgd", *args], cwd=str(CLI_DIR),
                          capture_output=True, text=True, timeout=timeout,
                          env=_env(tmp_path, **extra))


def make(path: pathlib.Path, video: list[str], audio: list[str] | None,
         seconds: int = 2, size: str = "320x240", extra: list[str] | None = None) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
           f"testsrc2=size={size}:rate=25:duration={seconds}"]
    if audio is not None:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += video + (audio or []) + (extra or []) + ["-shortest", str(path)]
    subprocess.run(cmd, check=True)
    return path


def streams(path) -> dict:
    info = macready.probe(path) or {}
    out = {"format": (info.get("format") or {}).get("format_name", ""),
           "duration": float((info.get("format") or {}).get("duration") or 0)}
    for s in info.get("streams", []):
        out[s["codec_type"]] = {k: s.get(k) for k in ("codec_name", "codec_tag_string", "pix_fmt", "profile")}
    return out


def video_md5(path) -> str:
    """Fingerprint of the video packets themselves (unchanged = not re-encoded)."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0",
                          "-c", "copy", "-f", "md5", "-"], capture_output=True, text=True)
    return out.stdout.strip()


H264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac", "-b:a", "96k"]
OPUS = ["-c:a", "libopus", "-b:a", "64k"]
VP9 = ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "500k"]
HEVC = ["-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-x265-params", "log-level=error"]


def test_plans_for_each_kind_of_file(tmp_path):
    if not HAVE_FFMPEG:
        return
    cases = {
        "ready.mp4": (H264, AAC, [], "ok"),
        "vp9.webm": (VP9, OPUS, [], "video"),
        "h264_opus.mkv": (H264, OPUS, [], "audio"),
        "hevc.mkv": (HEVC, AAC, [], "remux"),
        "hev1.mp4": (HEVC, AAC, ["-tag:v", "hev1"], "remux"),
        "tenbit.mp4": (["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le"], AAC, [], "video"),
        "silent.mkv": (H264, None, [], "remux"),
    }
    for name, (video, audio, extra, want) in cases.items():
        f = make(tmp_path / name, video, audio, extra=extra)
        plan = macready.plan_for(macready.probe(f))
        assert plan.action == want, (name, plan.action, plan.reasons)
        assert (plan.reasons == []) == (want == "ok"), (name, plan.reasons)
    vp9 = macready.plan_for(macready.probe(tmp_path / "vp9.webm"))
    assert any("VP9" in r for r in vp9.reasons) and any("opus" in r for r in vp9.reasons)
    hev1 = macready.plan_for(macready.probe(tmp_path / "hev1.mp4"))
    assert any("hev1" in r for r in hev1.reasons)
    # not videos: left alone
    assert macready.plan_for(None).action == "ok"
    (tmp_path / "song.mp3").write_bytes(b"ID3")
    assert macready.plan_for(macready.probe(tmp_path / "song.mp3")).action == "ok"


def test_encoder_choice_by_resolution_and_bit_depth():
    def cmd(height, pix="yuv420p", transfer=None, bitrate=None):
        info = {"format": {"format_name": "matroska,webm", "duration": "10",
                           **({"bit_rate": str(bitrate)} if bitrate else {})},
                "streams": [{"codec_type": "video", "codec_name": "vp9", "height": height,
                             "width": height * 16 // 9, "pix_fmt": pix, "avg_frame_rate": "30/1",
                             **({"color_transfer": transfer} if transfer else {})},
                            {"codec_type": "audio", "codec_name": "opus", "channels": 2}]}
        return macready._command("in.webm", "out.mp4", macready.plan_for(info))

    def value(c, flag):
        return c[c.index(flag) + 1]

    hw = macready.hardware_encoder_available()
    c1080 = cmd(1080)
    assert value(c1080, "-c:v") == ("h264_videotoolbox" if hw else "libx264")
    assert value(c1080, "-pix_fmt") == "yuv420p"
    c4k = cmd(2160)
    assert value(c4k, "-c:v") == ("hevc_videotoolbox" if hw else "libx265")
    assert value(c4k, "-tag:v") == "hvc1"
    hdr = cmd(1080, pix="yuv420p10le", transfer="smpte2084")
    assert value(hdr, "-tag:v") == "hvc1" and value(hdr, "-pix_fmt") in ("p010le", "yuv420p10le")
    if hw:
        assert int(value(c4k, "-b:v")) > int(value(c1080, "-b:v"))
        # a low-bitrate source is not blown up to the 8 Mbps default (some
        # headroom is kept: H.264 needs more bits than VP9 for the same picture)
        assert int(value(cmd(1080, bitrate=1_000_000), "-b:v")) < 3_000_000
    assert value(c1080, "-c:a") in ("aac_at", "aac") and value(c1080, "-movflags") == "+faststart"


def test_conversions_keep_quality_where_possible(tmp_path):
    if not HAVE_FFMPEG:
        return
    # ready file: untouched
    ready = make(tmp_path / "ready.mp4", H264, AAC)
    before = (ready.stat().st_mtime, ready.read_bytes())
    out = _mgd(tmp_path, ["convert", str(ready)])
    assert out.returncode == 0 and "already plays smoothly" in out.stdout, out.stdout + out.stderr
    assert (ready.stat().st_mtime, ready.read_bytes()) == before

    # audio only: the video packets are copied as they are
    mkv = make(tmp_path / "h264_opus.mkv", H264, OPUS)
    md5 = video_md5(mkv)
    assert _mgd(tmp_path, ["convert", str(mkv)]).returncode == 0
    mp4 = tmp_path / "h264_opus.mp4"
    s = streams(mp4)
    assert not mkv.exists() and "mp4" in s["format"]
    assert s["audio"]["codec_name"] == "aac" and video_md5(mp4) == md5

    # HEVC in mkv and HEVC tagged hev1: rewrapped losslessly as hvc1
    for name, extra in (("hevc.mkv", []), ("hev1.mp4", ["-tag:v", "hev1"])):
        src = make(tmp_path / name, HEVC, AAC, extra=extra)
        md5 = video_md5(src)
        assert _mgd(tmp_path, ["convert", str(src)]).returncode == 0
        out_file = src.with_suffix(".mp4")
        s = streams(out_file)
        assert s["video"]["codec_tag_string"] == "hvc1", (name, s)
        assert video_md5(out_file) == md5, f"{name} was re-encoded"

    # VP9 + Opus webm: re-encoded to H.264 + AAC, same length
    webm = make(tmp_path / "vp9.webm", VP9, OPUS, seconds=3)
    res = _mgd(tmp_path, ["convert", str(webm)])
    assert res.returncode == 0, res.stdout + res.stderr
    s = streams(tmp_path / "vp9.mp4")
    assert not webm.exists()
    assert s["video"]["codec_name"] == "h264" and s["video"]["pix_fmt"] == "yuv420p"
    assert s["audio"]["codec_name"] == "aac" and abs(s["duration"] - 3) < 0.3

    # 10-bit H.264 (no hardware decoding): becomes 10-bit HEVC, which is
    # hardware-decoded and keeps the 10-bit quality; file name unchanged
    tenbit = make(tmp_path / "tenbit.mp4",
                  ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le"], AAC)
    assert _mgd(tmp_path, ["convert", str(tenbit)]).returncode == 0
    s = streams(tenbit)
    assert s["video"]["codec_name"] == "hevc" and s["video"]["codec_tag_string"] == "hvc1", s
    # everything converted above now counts as Mac-ready by the same rules
    for name in ("h264_opus.mp4", "hevc.mp4", "hev1.mp4", "vp9.mp4", "tenbit.mp4"):
        plan = macready.plan_for(macready.probe(tmp_path / name))
        assert plan.action == "ok", (name, plan.reasons)
    assert not list(tmp_path.glob(".*mgdconvert*")), "temporary files left behind"


def test_check_mode_and_fix_mode_change_nothing_they_should_not(tmp_path):
    if not HAVE_FFMPEG:
        return
    folder = tmp_path / "videos"
    make(folder / "a.webm", VP9, OPUS)
    make(folder / "b.mkv", H264, AAC)
    make(folder / "c.mp4", H264, AAC)
    report = json.loads(_mgd(tmp_path, ["convert", str(folder), "--check", "--json"]).stdout)
    actions = {pathlib.Path(r["path"]).name: r["action"] for r in report["files"]}
    assert actions == {"a.webm": "video", "b.mkv": "remux", "c.mp4": "ok"}
    assert sorted(p.name for p in folder.iterdir()) == ["a.webm", "b.mkv", "c.mp4"]
    # fix mode: rewraps b.mkv, leaves the VP9 file alone (no re-encoding)
    out = _mgd(tmp_path, ["convert", str(folder), "--mode", "fix", "--json-progress"])
    assert out.returncode == 0, out.stdout + out.stderr
    assert sorted(p.name for p in folder.iterdir()) == ["a.webm", "b.mp4", "c.mp4"]
    assert "needs re-encoding" in out.stdout


def test_a_failed_or_stopped_conversion_keeps_the_original(tmp_path):
    if not HAVE_FFMPEG:
        return
    # truncated download: the header promises 8 s, only part is there
    good = make(tmp_path / "full.webm", VP9, OPUS, seconds=8)
    broken = tmp_path / "broken.webm"
    broken.write_bytes(good.read_bytes()[: good.stat().st_size // 3])
    original = broken.read_bytes()
    out = _mgd(tmp_path, ["convert", str(broken), "--json-progress"])
    events = [json.loads(l) for l in out.stdout.splitlines() if l.startswith("{")]
    stages = [e.get("stage") for e in events if e["type"] == "convert"]
    if "failed" in stages:                       # duration check caught it
        assert broken.read_bytes() == original
        assert not (tmp_path / "broken.mp4").exists()
    assert not list(tmp_path.glob(".*mgdconvert*"))

    # stopped half-way (pause in the app): original kept, temp file removed
    long = make(tmp_path / "long.webm", VP9, OPUS, seconds=40, size="1280x720")
    original = long.read_bytes()
    proc = subprocess.Popen([sys.executable, "bin/mgd", "convert", str(long), "--json-progress"],
                            cwd=str(CLI_DIR), env=_env(tmp_path), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    first = proc.stdout.readline()                # the "start" event: converting now
    assert '"stage": "start"' in first, first
    time.sleep(0.8)
    proc.send_signal(signal.SIGTERM)
    rest, _ = proc.communicate(timeout=60)
    assert '"stage": "stopped"' in rest, rest
    done = [json.loads(l) for l in rest.splitlines() if l.startswith('{"type": "done"')][-1]
    assert done["path"] == str(long) and long.read_bytes() == original
    assert not (tmp_path / "long.mp4").exists()
    assert not list(tmp_path.glob(".*mgdconvert*"))


def test_downloads_are_made_mac_ready_on_their_own(tmp_path):
    if not HAVE_FFMPEG:
        return
    clip = make(tmp_path / "src" / "clip.webm", VP9, OPUS, seconds=3)
    origin = conftest.DripOrigin(clip.read_bytes(), chunk=256 * 1024, delay=0)
    try:
        url = origin.url.replace("drip.bin", "clip.webm")
        out = _mgd(tmp_path, ["grab", url, "-d", str(tmp_path / "dl"), "--json-progress"])
        assert out.returncode == 0, out.stdout[-600:] + out.stderr[-600:]
        events = [json.loads(l) for l in out.stdout.splitlines() if l.startswith('{"type"')]
        kinds = [(e["type"], e.get("stage")) for e in events if e["type"] in ("convert", "done")]
        assert ("convert", "start") in kinds and ("convert", "done") in kinds, kinds
        done = events[-1]
        assert done["type"] == "done" and done["path"] == str(tmp_path / "dl" / "clip.mp4")
        assert done["size"] == (tmp_path / "dl" / "clip.mp4").stat().st_size
        assert streams(done["path"])["video"]["codec_name"] == "h264"
        assert not (tmp_path / "dl" / "clip.webm").exists()
        # switched off: the download stays exactly as it arrived
        assert _mgd(tmp_path, ["set-default", "mac-ready", "off"]).returncode == 0
        out = _mgd(tmp_path, ["grab", url, "-d", str(tmp_path / "raw"), "--json-progress"])
        assert '"type": "convert"' not in out.stdout
        assert (tmp_path / "raw" / "clip.webm").read_bytes() == clip.read_bytes()
        cfg = json.loads(_mgd(tmp_path, ["defaults", "--json"]).stdout)
        assert cfg["mac_ready"] == "off"
    finally:
        origin.stop()
