"""Write book metadata into audio files without renaming them.

Tag choices follow what Audiobookshelf reads:
  album → title, artist/album artist → author, composer → narrator,
  series / series-part (and MP4/ID3 movement name/number), asin, publisher,
  subtitle, description, genre, year, track / disc numbers.

Multi-file books: each file keeps (or gets) a per-file title so ABS's
track-based chapters are meaningful — Audible chapter names are used when the
file count matches the chapter count. Single-file books get Audible chapters
embedded when the file has none and its length matches the release.
"""
from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (APIC, CHAP, COMM, CTOC, ID3, MVIN, MVNM, TALB, TCOM, TCON, TDRC, TIT2, TIT3, TLAN, TPE1,
                         TPE2, TPOS, TPUB, TRCK, TXXX, CTOCFlags, ID3NoHeaderError)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

log = logging.getLogger(__name__)


def audio_length(path: Path) -> float | None:
    try:
        f = mutagen.File(str(path))
        return float(f.info.length) if f and f.info else None
    except Exception:  # noqa: BLE001 — corrupt/odd files shouldn't kill processing
        return None


def existing_title(path: Path) -> str | None:
    try:
        f = mutagen.File(str(path), easy=True)
        if f and f.tags:
            v = f.tags.get("title")
            if v:
                return str(v[0]).strip() or None
    except Exception:  # noqa: BLE001
        pass
    return None


def _fields(book: dict) -> dict:
    series = (book.get("series") or [None])[0] or {}
    from .providers.base import credited_authors
    authors = ", ".join(credited_authors(book))
    narrators = ", ".join(book.get("narrators") or [])
    return {
        "album": book.get("title") or "",
        "artist": authors,
        "albumartist": authors,
        "composer": narrators,
        "narrator": narrators,
        "genre": book.get("genres") or [],
        "year": book.get("year") or "",
        "date": book.get("release_date") or book.get("year") or "",
        "description": book.get("description") or "",
        "publisher": book.get("publisher") or "",
        "asin": book.get("asin") or "",
        "isbn": book.get("isbn") or "",
        "series": series.get("name") or "",
        "series_part": series.get("sequence") or "",
        "subtitle": book.get("subtitle") or "",
        "language": book.get("language") or "",
    }


# ------------------------------------------------------------------- formats
def _tag_id3(path: Path, f: dict, title: str, track: tuple[int, int], disc: tuple[int, int] | None,
             cover: bytes | None) -> None:
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    tags.setall("TIT2", [TIT2(encoding=3, text=title)])
    tags.setall("TALB", [TALB(encoding=3, text=f["album"])])
    tags.setall("TPE1", [TPE1(encoding=3, text=f["artist"])])
    tags.setall("TPE2", [TPE2(encoding=3, text=f["albumartist"])])
    if f["composer"]:
        tags.setall("TCOM", [TCOM(encoding=3, text=f["composer"])])
    if f["genre"]:
        tags.setall("TCON", [TCON(encoding=3, text=f["genre"])])
    if f["year"]:
        tags.setall("TDRC", [TDRC(encoding=3, text=f["year"])])
    if f["description"]:
        tags.setall("COMM", [COMM(encoding=3, lang="eng", desc="", text=f["description"])])
    if f["publisher"]:
        tags.setall("TPUB", [TPUB(encoding=3, text=f["publisher"])])
    if f["subtitle"]:
        tags.setall("TIT3", [TIT3(encoding=3, text=f["subtitle"])])
    if f["language"]:
        tags.setall("TLAN", [TLAN(encoding=3, text=f["language"])])
    if f["series"]:
        tags.setall("MVNM", [MVNM(encoding=3, text=f["series"])])
        if f["series_part"]:
            tags.setall("MVIN", [MVIN(encoding=3, text=f["series_part"])])
    tags.setall("TRCK", [TRCK(encoding=3, text=f"{track[0]}/{track[1]}")])
    if disc:
        tags.setall("TPOS", [TPOS(encoding=3, text=f"{disc[0]}/{disc[1]}")])
    for desc, val in (("ASIN", f["asin"]), ("AUDIBLE_ASIN", f["asin"]), ("SERIES", f["series"]),
                      ("SERIES-PART", f["series_part"]), ("NARRATOR", f["narrator"]),
                      ("SUBTITLE", f["subtitle"]), ("ISBN", f["isbn"]), ("DESCRIPTION", f["description"])):
        tags.delall(f"TXXX:{desc}")
        if val:
            tags.add(TXXX(encoding=3, desc=desc, text=val))
    if cover:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    tags.save(str(path), v2_version=4)


def _tag_mp4(path: Path, f: dict, title: str, track: tuple[int, int], disc: tuple[int, int] | None,
             cover: bytes | None) -> None:
    m = MP4(str(path))
    if m.tags is None:
        m.add_tags()
    t = m.tags
    t["\xa9nam"] = [title]
    t["\xa9alb"] = [f["album"]]
    t["\xa9ART"] = [f["artist"]]
    t["aART"] = [f["albumartist"]]
    if f["composer"]:
        t["\xa9wrt"] = [f["composer"]]
    if f["genre"]:
        t["\xa9gen"] = ["/".join(f["genre"])]
    if f["year"]:
        t["\xa9day"] = [f["date"] or f["year"]]
    if f["description"]:
        t["desc"] = [f["description"][:255]]
        t["ldes"] = [f["description"]]
        t["\xa9cmt"] = [f["description"]]
    if f["series"]:
        t["\xa9mvn"] = [f["series"]]
        t["\xa9grp"] = [f["series"]]
        try:
            if f["series_part"]:
                t["\xa9mvi"] = [int(float(f["series_part"]))]
        except ValueError:
            pass
    t["trkn"] = [track]
    if disc:
        t["disk"] = [disc]
    t["stik"] = [2]  # Audiobook
    for name, val in (("ASIN", f["asin"]), ("AUDIBLE_ASIN", f["asin"]), ("SERIES", f["series"]),
                      ("SERIES-PART", f["series_part"]), ("NARRATOR", f["narrator"]),
                      ("PUBLISHER", f["publisher"]), ("SUBTITLE", f["subtitle"]), ("ISBN", f["isbn"]),
                      ("LANGUAGE", f["language"])):
        key = f"----:com.apple.iTunes:{name}"
        if val:
            t[key] = [MP4FreeForm(val.encode("utf-8"))]
        elif key in t:
            del t[key]
    if cover:
        t["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    m.save()


def _vorbis_set(tags, f: dict, title: str, track: tuple[int, int], disc: tuple[int, int] | None) -> None:
    def put(k, v):
        if v:
            tags[k] = v if isinstance(v, list) else [str(v)]
    put("TITLE", title)
    put("ALBUM", f["album"])
    put("ARTIST", f["artist"])
    put("ALBUMARTIST", f["albumartist"])
    put("COMPOSER", f["composer"])
    put("NARRATOR", f["narrator"])
    put("GENRE", f["genre"])
    put("DATE", f["date"] or f["year"])
    put("DESCRIPTION", f["description"])
    put("COMMENT", f["description"])
    put("PUBLISHER", f["publisher"])
    put("ASIN", f["asin"])
    put("AUDIBLE_ASIN", f["asin"])
    put("ISBN", f["isbn"])
    put("SERIES", f["series"])
    put("SERIES-PART", f["series_part"])
    put("SUBTITLE", f["subtitle"])
    put("LANGUAGE", f["language"])
    put("TRACKNUMBER", str(track[0]))
    put("TRACKTOTAL", str(track[1]))
    if disc:
        put("DISCNUMBER", str(disc[0]))
        put("DISCTOTAL", str(disc[1]))


def _picture(cover: bytes) -> Picture:
    p = Picture()
    p.type = 3
    p.mime = "image/jpeg"
    p.desc = "Cover"
    p.data = cover
    return p


def _tag_flac(path, f, title, track, disc, cover):
    a = FLAC(str(path))
    if a.tags is None:
        a.add_tags()
    _vorbis_set(a.tags, f, title, track, disc)
    if cover:
        a.clear_pictures()
        a.add_picture(_picture(cover))
    a.save()


def _tag_ogg(cls):
    def inner(path, f, title, track, disc, cover):
        a = cls(str(path))
        if a.tags is None:
            a.add_tags()
        _vorbis_set(a.tags, f, title, track, disc)
        if cover:
            a.tags["METADATA_BLOCK_PICTURE"] = [base64.b64encode(_picture(cover).write()).decode("ascii")]
        a.save()
    return inner


WRITERS = {
    "mp3": _tag_id3,
    "m4b": _tag_mp4, "m4a": _tag_mp4, "mp4": _tag_mp4,
    "flac": _tag_flac,
    "ogg": _tag_ogg(OggVorbis), "opus": _tag_ogg(OggOpus),
}


# ------------------------------------------------------------------ chapters
def _has_chapters(path: Path) -> bool:
    ext = path.suffix.lower().lstrip(".")
    if ext == "mp3":
        try:
            return bool(ID3(str(path)).getall("CHAP"))
        except Exception:  # noqa: BLE001
            return False
    if shutil.which("ffprobe"):
        try:
            out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_chapters", str(path)],
                                 capture_output=True, text=True, timeout=120)
            return bool(json.loads(out.stdout or "{}").get("chapters"))
        except Exception:  # noqa: BLE001
            return False
    return True  # can't tell → don't touch


def _embed_chapters_id3(path: Path, chapters: list[dict], total_ms: int) -> None:
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    ids = []
    for i, ch in enumerate(chapters):
        cid = f"ch{i:03d}"
        start = int(ch["start_ms"])
        end = int(chapters[i + 1]["start_ms"]) if i + 1 < len(chapters) else total_ms
        tags.add(CHAP(element_id=cid, start_time=start, end_time=max(end, start),
                      sub_frames=[TIT2(encoding=3, text=ch["title"])]))
        ids.append(cid)
    tags.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED, child_element_ids=ids,
                  sub_frames=[TIT2(encoding=3, text="Chapters")]))
    tags.save(str(path), v2_version=4)


def _embed_chapters_ffmpeg(path: Path, chapters: list[dict], total_ms: int) -> None:
    """Remux (stream copy) with a chapter list; the filename is preserved."""
    meta = [";FFMETADATA1"]
    for i, ch in enumerate(chapters):
        start = int(ch["start_ms"])
        end = int(chapters[i + 1]["start_ms"]) if i + 1 < len(chapters) else total_ms
        title = ch["title"].replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", " ")
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={max(end, start)}", f"title={title}"]
    with tempfile.TemporaryDirectory(dir=path.parent) as td:
        mf = Path(td) / "chapters.txt"
        mf.write_text("\n".join(meta) + "\n", encoding="utf-8")
        out = Path(td) / ("out" + path.suffix)
        err = ""
        # First try keeping an embedded cover stream; some files' cover streams won't
        # remux cleanly, so fall back to audio only (the cover is re-embedded by mutagen).
        for maps in (["-map", "0:a", "-map", "0:v?", "-disposition:v", "attached_pic"], ["-map", "0:a"]):
            cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path), "-i", str(mf), *maps,
                   "-map_metadata", "0", "-map_chapters", "1", "-c", "copy",
                   "-movflags", "+faststart", "-f", "mp4", str(out)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if r.returncode == 0 and out.exists() and out.stat().st_size >= path.stat().st_size * 0.9:
                out.replace(path)
                return
            err = r.stderr[-300:]
        raise RuntimeError(f"ffmpeg chapter remux failed: {err}")


def maybe_embed_chapters(path: Path, book: dict, tolerance_s: float = 90) -> str:
    chapters = book.get("chapters") or []
    if len(chapters) < 2:
        return "no chapter data"
    length = audio_length(path)
    expected = (book.get("runtime_ms") or 0) / 1000
    if not length or not expected:
        return "length unknown"
    if abs(length - expected) > max(tolerance_s, expected * 0.01):
        return f"length mismatch ({length:.0f}s vs {expected:.0f}s) — skipped"
    if _has_chapters(path):
        return "file already has chapters"
    total_ms = int(length * 1000)
    ext = path.suffix.lower().lstrip(".")
    if ext == "mp3":
        _embed_chapters_id3(path, chapters, total_ms)
    elif ext in ("m4b", "m4a", "mp4") and shutil.which("ffmpeg"):
        _embed_chapters_ffmpeg(path, chapters, total_ms)
    else:
        return f"chapters not supported for .{ext}"
    return f"embedded {len(chapters)} chapters"


# --------------------------------------------------------------------- main
def tag_book(files: list[Path], book: dict, cover: bytes | None, embed_chapters: bool, log_fn=log.info) -> dict:
    """Tag every audio file of one book. `files` must already be in play order."""
    f = _fields(book)
    total = len(files)
    discs = sorted({p.parent for p in files}, key=lambda d: str(d))
    disc_count = len(discs)
    chapters = book.get("chapters") or []
    use_chapter_titles = total > 1 and len(chapters) == total
    result = {"tagged": 0, "skipped": [], "chapters": None}

    if total == 1 and embed_chapters:
        try:
            result["chapters"] = maybe_embed_chapters(files[0], book)
            log_fn(f"Chapters: {result['chapters']}")
        except Exception as e:  # noqa: BLE001
            result["chapters"] = f"failed: {e}"
            log_fn(f"Chapter embed failed: {e}")

    for i, path in enumerate(files, start=1):
        ext = path.suffix.lower().lstrip(".")
        writer = WRITERS.get(ext)
        if not writer:
            result["skipped"].append(path.name)
            continue
        if total == 1:
            title = book.get("title") or path.stem
        elif use_chapter_titles:
            title = chapters[i - 1]["title"]
        else:
            title = existing_title(path) or path.stem
        disc = None
        if disc_count > 1:
            disc = (discs.index(path.parent) + 1, disc_count)
        try:
            writer(path, f, title, (i, total), disc, cover)
            result["tagged"] += 1
        except Exception as e:  # noqa: BLE001
            log_fn(f"Tagging failed for {path.name}: {e}")
            result["skipped"].append(path.name)
    return result
