"""Torrent/magnet support (bencode + .torrent parsing, no dependencies).

The wire protocol itself is delegated to aria2c (installed via brew) and
streaming to webtorrent-cli - the same "best engine per job" philosophy
as yt-dlp for YouTube/X. This module only parses metadata so mgd can
show the user a file picker before the download starts.
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse as u

MAGNET_RE = re.compile(r"^magnet:\?", re.I)
TORRENT_EXT = re.compile(r"\.torrent$", re.I)


def is_magnet(url: str) -> bool:
    return bool(MAGNET_RE.match((url or "").strip()))


def is_torrent_file(url: str) -> bool:
    import pathlib
    p = pathlib.Path(u.urlsplit(url).path if "://" in url else url)
    return bool(TORRENT_EXT.search(p.name))


def is_torrent(url: str) -> bool:
    return is_magnet(url) or is_torrent_file(url)


# ── bencode ─────────────────────────────────────────────────────────────

class BencodeError(ValueError):
    pass


def bdecode(data: bytes):
    """Minimal bencode decoder (dicts keep insertion order, like torrent
    metainfo tools expect)."""
    pos = 0

    def parse():
        nonlocal pos
        if pos >= len(data):
            raise BencodeError("truncated bencode")
        c = data[pos:pos + 1]
        if c == b"i":
            end = data.index(b"e", pos)
            val = int(data[pos + 1:end])
            pos = end + 1
            return val
        if c in (b"l", b"d"):
            pos += 1
            out = [] if c == b"l" else {}
            while data[pos:pos + 1] != b"e":
                if c == b"l":
                    out.append(parse())
                else:
                    key = parse()
                    if not isinstance(key, bytes):
                        raise BencodeError("dict key must be a string")
                    out[key.decode("utf-8", "replace")] = parse()
            pos += 1
            return out
        if c.isdigit():
            end = data.index(b":", pos)
            ln = int(data[pos:end])
            pos = end + 1
            if pos + ln > len(data):
                raise BencodeError("truncated bencode string")
            val = data[pos:pos + ln]
            pos += ln
            return val
        raise BencodeError(f"bad bencode byte {c!r} at {pos}")

    try:
        return parse()
    except (IndexError, ValueError) as err:
        raise BencodeError(str(err)) from err


def bencode(obj) -> bytes:
    """Minimal bencode encoder (for tests and building fixtures)."""
    if isinstance(obj, bool):
        raise BencodeError("bools are not bencodable")
    if isinstance(obj, int):
        return b"i%de" % obj
    if isinstance(obj, bytes):
        return b"%d:%s" % (len(obj), obj)
    if isinstance(obj, str):
        b = obj.encode("utf-8")
        return b"%d:%s" % (len(b), b)
    if isinstance(obj, list):
        return b"l" + b"".join(bencode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        items = sorted(obj.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise BencodeError(f"unbencodable type {type(obj).__name__}")


# ── metainfo ────────────────────────────────────────────────────────────

VIDEO_EXT_RE = re.compile(
    r"\.(mp4|mkv|webm|avi|mov|m4v|mpg|ts|wmv|flv)$", re.I)


def _info_of_metainfo(meta: dict) -> dict:
    info = meta.get(b"info") or meta.get("info")
    if not isinstance(info, dict):
        raise BencodeError("no info dict in torrent")
    return info


def torrent_files(meta: dict) -> list[dict]:
    """[{index, path, size, video}] — index matches aria2's --select-file
    numbering (1-based, order of the files in the metainfo)."""
    info = _info_of_metainfo(meta)
    out: list[dict] = []
    name_key = b"name" if b"name" in info else "name"
    root = info.get(name_key)
    root = root.decode("utf-8", "replace") if isinstance(root, bytes) else str(root or "")
    if b"files" in info or "files" in info:
        files = info.get(b"files") or info.get("files")
        for f in files:
            length = f.get(b"length", f.get("length", 0))
            path_parts = f.get(b"path") or f.get("path") or []
            path = "/".join(p.decode("utf-8", "replace")
                            if isinstance(p, bytes) else str(p) for p in path_parts)
            out.append({"index": len(out) + 1,
                        "path": (root + "/" + path) if root else path,
                        "size": int(length), "video": bool(VIDEO_EXT_RE.search(path))})
    else:
        length = info.get(b"length", info.get("length", 0))
        out.append({"index": 1, "path": root, "size": int(length),
                    "video": bool(VIDEO_EXT_RE.search(root))})
    return out


def torrent_name(meta: dict) -> str:
    info = _info_of_metainfo(meta)
    name = info.get(b"name") or info.get("name") or b""
    return name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)


def info_hash(meta: dict) -> str:
    """The btih hash = sha1 of the bencoded info dict."""
    info = _info_of_metainfo(meta)
    return hashlib.sha1(bencode(info)).hexdigest()


def magnet_info_hash(magnet: str) -> str | None:
    for part in (u.urlsplit(magnet).query or magnet.split("?", 1)[-1]).split("&"):
        if part.lower().startswith("xt=urn:btih:"):
            v = part.split(":", 3)[2] if part.count(":") >= 2 else part[11:]
            if re.fullmatch(r"[0-9a-fA-F]{40}", v):
                return v.lower()
            try:   # base32 magnet hashes exist too
                import base64
                raw = base64.b32decode(v.upper() + "=" * ((8 - len(v) % 8) % 8))
                return raw.hex()
            except Exception:
                return None
    return None


def make_single_file_torrent(path: str, piece_length: int = 32768,
                             announce: str = "http://127.0.0.1:6969/announce") -> bytes:
    """Build a valid single-file .torrent (used by tests and examples)."""
    import pathlib
    data = pathlib.Path(path).read_bytes()
    pieces = b"".join(hashlib.sha1(data[i:i + piece_length]).digest()
                     for i in range(0, len(data), piece_length))
    meta = {
        "announce": announce,
        "info": {
            "name": pathlib.Path(path).name,
            "piece length": piece_length,
            "pieces": pieces,
            "length": len(data),
        },
    }
    return bencode(meta)
