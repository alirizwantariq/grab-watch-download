"""Media link discovery in HTML pages (for "grab" mode).

Finds:
  - <video>, <audio>, <source> elements and their src attributes
  - og:video*, twitter:player:stream, item-video meta tags
  - anchor links pointing at consumable file types (direct media links)
All relative URLs are resolved against the page URL. Nothing is fetched
beyond the page itself; listed URLs are candidates for the downloader.
"""
from __future__ import annotations

import re
import urllib.parse as u

VIDEO_EXTS = re.compile(
    r"\.(mp4|mkv|webm|avi|mov|flv|m4v|mpg|mpeg|ts|3gp|ogv|gifv|wmv)([?#].*)?$", re.I)
AUDIO_EXTS = re.compile(
    r"\.(mp3|wav|flac|m4a|aac|ogg|opus|wma|aiff)([?#].*)?$", re.I)
DOWNLOADABLE_EXTS = re.compile(
    r"\.(zip|rar|7z|tar|gz|bz2|xz|pdf|docx?|xlsx?|pptx?|txt|md|epub|csv|"
    r"json|xml|dmg|exe|msi|pkg|deb|rpm|apk|appimage|iso|srt|vtt|bin|app)([?#].*)?$",
    re.I)

_MEDIA_TAG_RE = re.compile(
    r"<source\b[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>|(?:<video|<audio)\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.I)
_META_RE = re.compile(
    r"<meta\b[^>]*property=[\"'](?:og:video(:?:(?:secure_)?url)?|twitter:player:stream)[\"'][^>]*\bcontent=[\"']([^\"']+)[\"']", re.I)

# KVS (Kernel Video Sharing) player config - flashvars powers erome.tv and
# most tube sites. The video urls live as JS string values inside a JS object,
# so the tag/attribute scanners above never see them.
_KVS_BLOCK_RE = re.compile(r"flashvars\s*=\s*\{(.{50,}?)\}\s*;", re.S)
_KVS_PAIR_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*'([^']*)'")
# canonical KVS video keys: video_url, video_url_text, video_alt_url,
# video_alt_url_text, video_alt_url2, _3, ... (+ trailing slideshow nums)
_KVS_QUALITY_KEY_RE = re.compile(
    r"^video_url\d{0,2}_alt_url\d{0,2}$|^video_alt_url\d{0,2}$|^video_url\d{0,2}$")
_KVS_QUALITY_RE = re.compile(r"(?:^|[_/.])(\d{3,4}[pP])\.m(?:p4|3u8)", re.I)
_KVS_MEDIA_HINT = (".mp4", ".m3u8", ".webm", ".mpd")


def _strip_amp(url: str) -> str:
    return url.replace("&amp;", "&").strip()


def _kvs_flashvars_candidates(html: str) -> list[tuple[str, str, str]]:
    """KVS player config ('var flashvars = { video_url: …, … }') →
    [(url, quality_label, video_title)] in quality-listed order, best first.
    Only entries whose value points at a media file are returned; e.g.
    erome.tv, countless tube sites."""
    out: list[tuple[str, str, str]] = []
    for block in _KVS_BLOCK_RE.findall(html):
        pairs = {k: v for k, v in _KVS_PAIR_RE.findall(block)}
        title = pairs.get("video_title", "")

        def label_for(key: str) -> str:
            text = pairs.get(key + "_text", "")
            if text and text not in ("-", "none", "None"):
                return text
            q = _KVS_QUALITY_RE.search(pairs[key])
            return q.group(1) if q else ""

        for key, url in pairs.items():
            if not _KVS_QUALITY_KEY_RE.match(key):
                continue
            path = url.partition("?")[0].rstrip("/").lower()
            if not path.endswith(_KVS_MEDIA_HINT):
                continue
            out.append((url, label_for(key), title))
    return out


def extract_links(html: str, base_url: str) -> list[dict]:
    """Return [{kind, url, title}] in discovery order; deduplicated."""
    out: list[dict] = []
    seen: set[str] = set()

    def add(kind: str, raw: str | None, title: str = "") -> None:
        if not raw:
            return
        raw = _strip_amp(raw)
        if raw.startswith(("javascript:", "#", "mailto:")):
            return
        url = u.urljoin(base_url, raw)
        if not u.urlsplit(url).scheme in ("http", "https"):
            return
        if url in seen:
            return
        seen.add(url)
        out.append({"kind": kind, "url": url, "title": title})

    # KVS players first: these are the qualities of the page's own video
    for url, label, title in _kvs_flashvars_candidates(html):
        add("flashvars", url, label or title)
    for tag_kind in ("video", "audio"):
        for m in re.finditer(rf"<{tag_kind}\b[^>]*>", html, re.I):
            src = re.search(r"\bsrc=[\"']([^\"']+)[\"']", m.group(0), re.I)
            poster = re.search(r"\bposter=[\"']([^\"']+)[\"']", m.group(0), re.I)
            add(tag_kind, src.group(1) if src else None,
                "poster" if poster else "")
    for m in _MEDIA_TAG_RE.finditer(html):
        url = m.group(1) or m.group(2)
        add("source", url)
    for m in _META_RE.finditer(html):
        add("meta", m.group(2))
    # direct-file anchor links
    for m in re.finditer(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>", html, re.I):
        href = m.group(1)
        text = re.sub(r"<[^>]+>", " ", html[m.start():m.start() + 400])
        title_m = re.search(r"href=[\"'][^\"']+[\"']>([^<]{1,90})", html[m.start():m.start() + 400], re.I)
        title = title_m.group(1).strip() if title_m else ""
        if VIDEO_EXTS.search(href):
            add("link-video", href, title)
        elif AUDIO_EXTS.search(href):
            add("link-audio", href, title)
        elif DOWNLOADABLE_EXTS.search(href):
            add("link-file", href, title)
    return out


def pick_media_candidate(links: list[dict]) -> dict | None:
    videos = [l for l in links if l["kind"] in ("flashvars", "video", "source",
                                               "meta", "link-video")]
    return videos[0] if videos else (links[0] if links else None)


def is_html(content_type: str | None, body_head: bytes) -> bool:
    if content_type and "text/html" in content_type:
        return True
    head = body_head[:512].lstrip()
    return head.lower().startswith(b"<!doctype html") or head.lower().startswith(b"<html")