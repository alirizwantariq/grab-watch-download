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


def _strip_amp(url: str) -> str:
    return url.replace("&amp;", "&").strip()


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
    videos = [l for l in links if l["kind"] in ("video", "source", "meta",
                                                "link-video")]
    return videos[0] if videos else (links[0] if links else None)


def is_html(content_type: str | None, body_head: bytes) -> bool:
    if content_type and "text/html" in content_type:
        return True
    head = body_head[:512].lstrip()
    return head.lower().startswith(b"<!doctype html") or head.lower().startswith(b"<html")