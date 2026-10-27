"""Configuration and download-category resolution.

Config lives at ~/.config/mgd/config.json (Windows: %APPDATA%/mgd/config.json).
Layout:
{
  "root": "~/Downloads/mgd",           # default download root
  "defaults": {"video": "path", ...},  # optional user override per category
  "conns": 6,                          # default parallel connections
  "retries": 8,                        # default retry attempts
  "proxy": null                        # default proxy url
}
Category directories are created under root: video audio image archive
document software other
"""
from __future__ import annotations

import json
import os
import pathlib
import urllib.parse as u

CATEGORIES = ("video", "audio", "image", "archive", "document", "software",
              "other", "playlist", "subtitle")


EXT_MAP = {}
for _exts, _cat in [
    ("mp4 mkv webm avi mov wmv flv m4v mpg mpeg ts 3gp ogv gifv", "video"),
    ("mp3 wav flac m4a aac ogg oga opus wma aiff ape mka", "audio"),
    ("jpg jpeg png gif webp svg bmp tiff heic avif ico raw", "image"),
    ("zip rar 7z tar gz bz2 xz zst dmg iso cab", "archive"),
    ("pdf doc docx xls xlsx ppt pptx txt md rtf epub csv json xml keynote pages numbers", "document"),
    ("exe msi pkg dmg app deb rpm apk appimage msix appx psd ai ps1 bat sh", "software"),
    ("srt vtt ass ssa sub", "subtitle"),
    ("m3u m3u8 pls", "playlist"),
]:
    for _e in _exts.split():
        EXT_MAP[_e] = _cat
EXT_MAP.pop("dmg")  # first occurrence wins? no: dmg appears twice
EXT_MAP["dmg"] = "archive"
# keep one mapping per ext (software dmg is also fine but archive chosen)

CONTENT_TYPE_MAP = {
    "video/": "video", "audio/": "audio", "image/": "image",
    "application/zip": "archive", "application/x-rar-compressed": "archive",
    "application/x-7z-compressed": "archive", "application/x-tar": "archive",
    "application/gzip": "archive", "application/pdf": "document",
    "application/msword": "document", "text/": "document",
    "application/x-apple-diskimage": "archive", "application/vnd.android": "software",
    "application/octet-stream": "other", "application/x-msdownload": "software",
}


def config_path() -> pathlib.Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA", "")
        return pathlib.Path(base) / "mgd" / "config.json" if base else (
            pathlib.Path.home() / "AppData" / "Roaming" / "mgd" / "config.json")
    base = os.environ.get("XDG_CONFIG_HOME") or str(pathlib.Path.home() / ".config")
    return pathlib.Path(base) / "mgd" / "config.json"


def load_config() -> dict:
    path = config_path()
    cfg = {
        "root": None,
        "defaults": {},
        "conns": 6,
        "retries": 8,
        "proxy": None,
    }
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except ValueError as err:
            raise SystemExit(f"config {path} is not valid JSON and could not be loaded: {err}; "
                             f"read or remove the file, then use 'mgd set-default' or 'mgd config --set' to rewrite it.") from err
    return cfg


def save_config(cfg: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def default_root(cfg: dict) -> pathlib.Path:
    root = cfg.get("root") or "~/Downloads/mgd"
    path = pathlib.Path(os.path.expanduser(root))
    path.mkdir(parents=True, exist_ok=True)
    return path


def category_dir(cfg: dict, category: str) -> pathlib.Path:
    """User-set default path for the category, else root/<category>. Creates dirs."""
    override = cfg.get("defaults", {}).get(category)
    path = pathlib.Path(os.path.expanduser(override)) if override else default_root(cfg) / category
    path.mkdir(parents=True, exist_ok=True)
    return path


def categorize(filename: str, content_type: str | None = None) -> str:
    ext = pathlib.Path(filename).suffix.lower().lstrip(".")
    if ext and ext in EXT_MAP:
        return EXT_MAP[ext]
    if filename.endswith(".tar.gz") or filename.endswith(".tgz"):
        return "archive"
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        for prefix, cat in CONTENT_TYPE_MAP.items():
            if ct == prefix or ct.startswith(prefix) and not ct == "application/octet-stream":
                return cat
        if ct == "text/html":
            return "other"
    return "other"


def unique_path(dir_path: pathlib.Path, filename: str) -> pathlib.Path:
    """Pick a non-colliding name: file.ext, file (1).ext, ..."""
    stem, ext = pathlib.Path(filename).stem, pathlib.Path(filename).suffix
    candidate = dir_path / filename
    n = 1
    while candidate.exists():
        candidate = dir_path / f"{stem} ({n}){ext}"
        n += 1
    return candidate


def filename_from_response_disposition(disposition: str | None) -> str | None:
    if not disposition:
        return None
    import re
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", disposition)
    if m:
        return u.unquote(m.group(1))
    m = re.search(r'filename="?([^";]+)"?', disposition)
    if m:
        return m.group(1).strip()
    return None


def filename_from_url(path: str) -> str:
    name = u.unquote(u.urlsplit(path).path.rstrip("/").split("/")[-1] or "download")
    return name if name else "download"