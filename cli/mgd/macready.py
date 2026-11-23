"""Mac-ready videos: files QuickTime, Finder previews and every Mac player
open instantly and decode in hardware (smooth even at 4K).

Why downloads stutter on a Mac: sites serve high resolutions as VP9/AV1
(often in .webm/.mkv). QuickTime cannot open those, and players that can
(VLC, IINA, browsers) may decode them in software - choppy at 1440p/4K.
10-bit or 4:4:4 H.264 has no hardware decoding either, and HEVC tagged
"hev1" (instead of "hvc1") is refused by QuickTime.

The fix is the cheapest one that works, decided per file:
  ok     already Mac-ready (H.264/HEVC 4:2:0 + AAC/MP3/AC-3/ALAC in .mp4/.mov)
  remux  right codecs, wrong box (.mkv/.webm/.ts, hev1 tag): rewrap, no
         re-encoding - seconds, lossless
  audio  video fine, audio not (Opus/Vorbis/FLAC/DTS): AAC audio only
  video  re-encode the video with the Mac's hardware encoder (H.264 up to
         1440p, HEVC for 4K/HDR/10-bit), bitrate matched to the source

The result is verified (playable, same duration) before it replaces the
original; on any failure - or when interrupted - the original stays.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import time

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".flv", ".wmv",
              ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".ogv", ".vob",
              ".f4v", ".divx", ".rm", ".rmvb", ".asf"}
GOOD_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac"}
HW_FRIENDLY_PIX = {"yuv420p", "yuvj420p", "nv12"}
HEVC_PIX = HW_FRIENDLY_PIX | {"yuv420p10le", "p010le"}
MODES = ("convert", "fix", "off")


class Plan:
    def __init__(self, action: str, reasons: list[str], info: dict | None = None):
        self.action = action          # ok | remux | audio | video
        self.reasons = reasons
        self.info = info or {}

    def to_json(self) -> dict:
        return {"action": self.action, "reasons": self.reasons}


def is_video_file(path) -> bool:
    return pathlib.Path(path).suffix.lower() in VIDEO_EXTS


def tools_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def probe(path) -> dict | None:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                              "-show_format", "-show_streams", str(path)],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def _main_video(info: dict) -> dict | None:
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            return s
    return None


def _main_audio(info: dict) -> dict | None:
    return next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)


def _duration(info: dict) -> float:
    for value in [(info.get("format") or {}).get("duration")] + \
            [s.get("duration") for s in info.get("streams", [])]:
        try:
            if value and float(value) > 0:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _bitrate(info: dict, stream: dict | None) -> int:
    for value in ((stream or {}).get("bit_rate"), (info.get("format") or {}).get("bit_rate")):
        try:
            if value and int(value) > 0:
                return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        text = stream.get(key) or ""
        try:
            num, _, den = text.partition("/")
            value = float(num) / float(den or 1)
            if 1 <= value <= 240:
                return value
        except (ValueError, ZeroDivisionError):
            continue
    return 30.0


def _is_hdr(stream: dict) -> bool:
    return stream.get("color_transfer") in ("smpte2084", "arib-std-b67")


def _ten_bit(stream: dict) -> bool:
    return "10" in (stream.get("pix_fmt") or "") or stream.get("bits_per_raw_sample") == "10"


def plan_for(info: dict | None) -> Plan:
    """What it takes to make this file Mac-ready (see module docstring)."""
    if not info:
        return Plan("ok", [])
    video = _main_video(info)
    if video is None:
        return Plan("ok", [], info)               # audio file, image, not a video
    fmt = (info.get("format") or {}).get("format_name", "")
    container_ok = "mp4" in fmt or "mov" in fmt
    codec = (video.get("codec_name") or "").lower()
    pix = (video.get("pix_fmt") or "").lower()
    profile = (video.get("profile") or "")
    tag = (video.get("codec_tag_string") or "").lower()
    reasons: list[str] = []
    video_bad = False
    if codec == "h264":
        if pix not in HW_FRIENDLY_PIX or any(p in profile for p in ("10", "4:2:2", "4:4:4")):
            video_bad = True
            reasons.append(f"{profile or pix} H.264 has no hardware decoding on a Mac (plays choppy)")
    elif codec == "hevc":
        if pix not in HEVC_PIX:
            video_bad = True
            reasons.append(f"HEVC {pix} has no hardware decoding on a Mac")
        elif container_ok and tag == "hev1":
            reasons.append("HEVC is tagged hev1 - QuickTime only opens hvc1")
    elif codec in ("prores", "mjpeg") and container_ok:
        pass
    else:
        video_bad = True
        names = {"vp9": "VP9", "av1": "AV1", "vp8": "VP8", "mpeg4": "MPEG-4 (DivX/Xvid)",
                 "mpeg2video": "MPEG-2", "wmv3": "WMV", "wmv2": "WMV", "vc1": "VC-1",
                 "theora": "Theora", "flv1": "Flash video", "h263": "H.263"}
        reasons.append(f"{names.get(codec, codec.upper() or 'this')} video is not supported by "
                       "QuickTime and plays choppy in other Mac players")
    if (video.get("field_order") or "progressive") not in ("progressive", "unknown"):
        video_bad = True
        reasons.append("interlaced video judders on a Mac screen")
    audio = _main_audio(info)
    acodec = (audio or {}).get("codec_name", "").lower()
    # anything else (including raw PCM, which .mp4 cannot carry) becomes AAC
    audio_bad = bool(audio) and acodec not in GOOD_AUDIO
    if audio_bad:
        reasons.append(f"{acodec or 'its'} audio is not supported by QuickTime")
    if not container_ok and not video_bad:
        reasons.append(f"the .{fmt.split(',')[0]} container does not open in QuickTime")
    if video_bad:
        return Plan("video", reasons, info)
    if audio_bad:
        return Plan("audio", reasons, info)
    if not container_ok or (codec == "hevc" and tag == "hev1"):
        return Plan("remux", reasons, info)
    return Plan("ok", [], info)


_ENCODERS: set[str] | None = None


def _encoders() -> set[str]:
    global _ENCODERS
    if _ENCODERS is None:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        _ENCODERS = {line.split()[1] for line in out.splitlines()
                     if len(line.split()) > 1 and line.startswith(" ")}
    return _ENCODERS


def hardware_encoder_available() -> bool:
    return "h264_videotoolbox" in _encoders()


def _target_bitrate(video: dict, info: dict, hevc: bool) -> int:
    height = int(video.get("height") or 1080)
    base = (2_500_000 if height <= 480 else 5_000_000 if height <= 720 else
            8_000_000 if height <= 1080 else 16_000_000 if height <= 1440 else 28_000_000)
    if hevc:
        base = int(base * 0.65)
    base = int(base * min(2.0, max(0.5, _fps(video) / 30)))
    source = _bitrate(info, video)
    if source:
        # never inflate a low-bitrate source; a little headroom for the
        # codec change (VP9/AV1 are more efficient than H.264)
        base = min(base, max(int(source * 1.4), base // 3))
    return max(base, 500_000)


def _command(src: str, dst: str, plan: Plan) -> list[str]:
    info = plan.info
    video = _main_video(info) or {}
    audio = _main_audio(info)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", src,
           "-map", "0:v:0", "-map", "0:a:0?", "-dn", "-sn", "-map_metadata", "0",
           "-movflags", "+faststart", "-progress", "pipe:1", "-nostats"]
    codec = (video.get("codec_name") or "").lower()
    if plan.action == "video":
        hevc = (int(video.get("height") or 0) > 1440 or _is_hdr(video) or _ten_bit(video))
        rate = _target_bitrate(video, info, hevc)
        filters = []
        if (video.get("field_order") or "progressive") not in ("progressive", "unknown"):
            filters.append("yadif")
        encoders = _encoders()
        if hevc:
            ten = _ten_bit(video) or _is_hdr(video)
            if "hevc_videotoolbox" in encoders:
                cmd += ["-c:v", "hevc_videotoolbox", "-b:v", str(rate),
                        "-profile:v", "main10" if ten else "main",
                        "-pix_fmt", "p010le" if ten else "yuv420p"]
            else:
                cmd += ["-c:v", "libx265", "-preset", "fast", "-crf", "22",
                        "-pix_fmt", "yuv420p10le" if ten else "yuv420p"]
            cmd += ["-tag:v", "hvc1"]
        elif "h264_videotoolbox" in encoders:
            cmd += ["-c:v", "h264_videotoolbox", "-b:v", str(rate),
                    "-profile:v", "high", "-pix_fmt", "yuv420p"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p"]
        if filters:
            cmd += ["-vf", ",".join(filters)]
    else:
        cmd += ["-c:v", "copy"]
        if codec == "hevc":
            cmd += ["-tag:v", "hvc1"]
    if audio is not None:
        acodec = (audio.get("codec_name") or "").lower()
        if acodec in GOOD_AUDIO:
            cmd += ["-c:a", "copy"]
        else:
            channels = int(audio.get("channels") or 2)
            encoder = "aac_at" if "aac_at" in _encoders() else "aac"
            cmd += ["-c:a", encoder, "-b:a", "384k" if channels > 2 else "192k"]
    cmd.append(dst)
    return cmd


class ConversionFailed(Exception):
    pass


def convert(path, plan: Plan, on_progress=None, keep_original: bool = False) -> pathlib.Path:
    """Run the plan. Returns the Mac-ready file's path (the original's, with
    .mp4). Raises ConversionFailed (original untouched) or KeyboardInterrupt
    (stopped; temporary output removed, original untouched)."""
    src = pathlib.Path(path)
    target = src.with_suffix(".mp4")
    if target != src and target.exists():
        n = 1
        while True:
            candidate = src.with_name(f"{src.stem} ({n}).mp4")
            if not candidate.exists():
                target = candidate
                break
            n += 1
    tmp = src.with_name(f".{src.stem}.mgdconvert.mp4")
    duration = _duration(plan.info)
    cmd = _command(str(src), str(tmp), plan)
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
        last = 0.0
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms") and value.isdigit() and duration:
                now = time.monotonic()
                if on_progress and now - last > 0.5:
                    last = now
                    on_progress(min(0.999, int(value) / 1_000_000 / duration))
        err = proc.stderr.read()
        code = proc.wait()
        if code != 0:
            raise ConversionFailed(" ".join(err.strip().splitlines()[-2:])[-300:] or f"ffmpeg exit {code}")
        result = probe(tmp)
        got = _duration(result or {})
        if not result or _main_video(result) is None:
            raise ConversionFailed("the converted file does not play")
        if duration and abs(got - duration) > max(1.5, duration * 0.01):
            raise ConversionFailed(f"the converted file is {got:.1f}s long instead of {duration:.1f}s")
        os.replace(tmp, target)
        if target != src and not keep_original:
            try:
                src.unlink()
            except OSError:
                pass
        if on_progress:
            on_progress(1.0)
        return target
    except BaseException:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
