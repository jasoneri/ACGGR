# -*- coding: utf-8 -*-
"""Host-side audio tagging for the aria2 bridge carrier.

The public station can only see its own filesystem; a song downloaded through acggr's aria2
lands on the host, so the host writes the tags. Semantics mirror muspalace's tagging.py:
- flac → VORBIS_COMMENT (TITLE/ARTIST/ALBUM/DATE/TRACKNUMBER/LYRICS) + PICTURE type 3
- mp3  → ID3v2.4 (TIT2/TPE1/TALB/TDRC/TRCK/USLT/APIC type 3)

The bridge passes two metadata vocabularies (station: name/artists/track_number/lyric+tlyric;
host: title/artist/track/lyrics) and may stringify lists; all normalised here.
mutagen is imported lazily inside the writers; tag_file never raises (failures are a dict with
success: False + code/message).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

__all__ = ["tag_file"]

_VORBIS_KEYS = ("TITLE", "ARTIST", "ALBUM", "DATE", "TRACKNUMBER", "LYRICS")
_SUPPORTED_SUFFIXES = (".flac", ".mp3")


def _as_text(value: Any) -> str:
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def _as_list(value: Any) -> list:
    """Undo the bridge's ``str()`` flattening of list-valued metadata."""
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                return []
            if isinstance(parsed, (list, tuple)):
                return list(parsed)
    return []


def _artist_names(value: Any) -> list[str]:
    names: list[str] = []
    for entry in _as_list(value):
        if isinstance(entry, dict):
            name = _as_text(entry.get("name"))
        elif isinstance(entry, str):
            name = entry.strip()
        else:
            name = ""
        if name:
            names.append(name)
    return names


def _artist_value(meta: dict) -> str:
    names = _artist_names(meta.get("artists"))
    if names:
        return " / ".join(names)
    direct = _as_text(meta.get("artist"))
    if direct:
        return direct
    joined = _as_text(meta.get("artist_string"))
    if joined:
        parts = [part.strip() for part in joined.split("/") if part.strip()]
        return " / ".join(parts) if parts else joined
    return ""


def _year_from(value: Any) -> str:
    match = re.match(r"^(\d{4})(?:-\d{1,2}){0,2}", _as_text(value))
    return match.group(1) if match else ""


def _track_number(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    return str(number) if number > 0 else ""


def merge_lyrics(lyric: str, tlyric: str) -> str:
    """Original first, translation after, joined by a separator line."""
    original = (lyric or "").strip()
    translated = (tlyric or "").strip()
    if original and translated:
        return original + "\n\n---\n" + translated
    return original or translated


def _normalize_meta(meta: dict) -> dict[str, str]:
    """One vorbis-comment / ID3-frame dictionary from both vocabularies."""
    comment: dict[str, str] = {}
    title = _as_text(meta.get("title")) or _as_text(meta.get("name"))
    if title:
        comment["TITLE"] = title
    artist = _artist_value(meta)
    if artist:
        comment["ARTIST"] = artist
    album = _as_text(meta.get("album"))
    if album:
        comment["ALBUM"] = album
    year = _year_from(meta.get("date")) or _year_from(meta.get("year"))
    if year:
        comment["DATE"] = year
    track = (
        _track_number(meta.get("track"))
        or _track_number(meta.get("track_number"))
        or _track_number(meta.get("trackNumber"))
    )
    if track:
        comment["TRACKNUMBER"] = track
    lyrics = _as_text(meta.get("lyrics")) or merge_lyrics(
        _as_text(meta.get("lyric")), _as_text(meta.get("tlyric"))
    )
    if lyrics:
        comment["LYRICS"] = lyrics
    return comment


def _write_flac(target: Path, comment: dict[str, str], cover: bytes | None, cover_mime: str) -> list[str]:
    from mutagen.flac import FLAC, Picture

    audio = FLAC(str(target))
    written: list[str] = []
    for key in _VORBIS_KEYS:
        if key in comment:
            audio[key] = comment[key]
            written.append(key)
    if cover:
        # Replace, don't accumulate: only the type-3 front cover is ours to own,
        # other picture kinds in the source file stay untouched.
        audio.metadata_blocks = [
            block
            for block in audio.metadata_blocks
            if not (isinstance(block, Picture) and block.type == 3)
        ]
        picture = Picture()
        picture.type = 3
        picture.mime = cover_mime or "image/jpeg"
        picture.desc = ""
        picture.data = cover
        audio.add_picture(picture)
        written.append("PICTURE")
    audio.save(str(target))
    return written


def _write_mp3(target: Path, comment: dict[str, str], cover: bytes | None, cover_mime: str) -> list[str]:
    from mutagen.id3 import (
        APIC,
        ID3,
        ID3NoHeaderError,
        TALB,
        TDRC,
        TIT2,
        TPE1,
        TRCK,
        USLT,
    )

    # Fresh aria2 downloads often carry no ID3 header at all; start from an
    # empty frame set in that case, then save a new ID3v2.4 tag.
    try:
        tags = ID3(str(target))
    except ID3NoHeaderError:
        tags = ID3()
    written: list[str] = []
    text_frames = ((TIT2, "TITLE"), (TPE1, "ARTIST"), (TALB, "ALBUM"))
    for frame, key in text_frames:
        if comment.get(key):
            tags.add(frame(encoding=3, text=comment[key]))  # encoding 3 = UTF-8
            written.append(frame.__name__)
    if comment.get("DATE"):
        tags.add(TDRC(encoding=3, text=comment["DATE"]))
        written.append("TDRC")
    if comment.get("TRACKNUMBER"):
        tags.add(TRCK(encoding=3, text=comment["TRACKNUMBER"]))
        written.append("TRCK")
    if comment.get("LYRICS"):
        tags.add(USLT(encoding=3, lang="und", desc="", text=comment["LYRICS"]))
        written.append("USLT")
    if cover:
        for key in [
            key
            for key, frame in tags.items()
            if frame.FrameID == "APIC" and getattr(frame, "type", None) == 3
        ]:
            del tags[key]
        tags.add(
            APIC(
                encoding=3,
                mime=cover_mime or "image/jpeg",
                type=3,
                desc="",
                data=cover,
            )
        )
        written.append("APIC")
    tags.update_to_v24()
    tags.save(str(target))
    return written


def _failure(code: str, message: str) -> dict:
    return {"success": False, "code": code, "message": message}


def tag_file(path: Path, meta: dict, cover: bytes | None, cover_mime: str) -> dict:
    """Write tags onto ``path`` in place; never raises.

    Injected into the aria2 bridge with ``aria2_bridge.set_tagger(tag_file)``.
    ``meta`` accepts either the station's detail-payload keys or the flat host
    keys; ``cover`` is the artwork bytes (the bridge fetches ``pic_url`` itself).
    """
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        return _failure("acggr_tag_bad_format", f"不支持嵌入标签的格式：{suffix or '(无后缀)'}")
    if not target.is_file():
        return _failure("acggr_tag_not_found", f"文件不存在：{target}")
    comment = _normalize_meta(dict(meta or {}))
    try:
        if suffix == ".flac":
            written = _write_flac(target, comment, cover, cover_mime)
        else:
            written = _write_mp3(target, comment, cover, cover_mime)
    except ImportError as exc:
        return _failure("acggr_tag_missing_mutagen", f"缺少 mutagen 依赖：{exc}")
    except Exception as exc:  # noqa: BLE001 - the bridge needs a dict, not a traceback
        return _failure("acggr_tag_failed", f"{type(exc).__name__}: {exc}")
    try:
        file_size = target.stat().st_size
    except OSError:
        file_size = 0
    return {
        "success": True,
        "tags_written": written,
        "cover_embedded": bool(cover),
        "file": target.name,
        "file_size": file_size,
    }
