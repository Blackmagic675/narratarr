"""Turn a folder of downloaded audio into an organised, tagged book folder.

incomplete/<job>/...  →  completed/<Author>/<Series>/<Book N - Title>/...

Filenames (and disc sub-folders) are kept exactly as downloaded. Sidecars are
written in formats Audiobookshelf reads natively: cover.jpg, desc.txt,
reader.txt, metadata.opf and metadata.json, plus .narratarr.json which records
where the files came from so the Library can be rebuilt by rescanning.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

from .matching import AUDIO_EXTS, EBOOK_EXTS, natural_key
from .providers.base import OPF_ROLES, credited_authors
from .tagging import audio_length, tag_book

log = logging.getLogger(__name__)

JUNK = {".ds_store", "thumbs.db", "desktop.ini"}
EXTRA_KEEP = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".cue", ".epub", ".nfo"}
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _mb(n: int) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB" if n >= 1e7 else f"{n / 1e6:.1f} MB"


def safe_segment(s: str, limit: int = 120) -> str:
    s = re.sub(r"\s*:\s*", " - ", s)
    s = _ILLEGAL.sub("", s).replace("’", "'").strip().rstrip(".")
    s = re.sub(r"\s+", " ", s)
    return s[:limit].rstrip() or "Unknown"


def render_template(template: str, book: dict) -> Path:
    series = (book.get("series") or [None])[0] or {}
    seq = series.get("sequence") or ""
    title = book.get("title") or "Unknown Title"
    tokens = {
        "author": ", ".join(credited_authors(book)[:2]) or "Unknown Author",
        "title": title,
        "series": series.get("name") or "",
        "seq": seq,
        "seqtitle": f"Book {seq} - {title}" if seq and series.get("name") else title,
        "narrator": ", ".join(book.get("narrators") or []),
        "year": book.get("year") or "",
        "asin": book.get("asin") or "",
    }
    parts = []
    for seg in template.replace("\\", "/").split("/"):
        rendered = re.sub(r"\{(\w+)\}", lambda m: str(tokens.get(m.group(1), "")), seg).strip(" -")
        if rendered:
            parts.append(safe_segment(rendered))
    return Path(*parts) if parts else Path(safe_segment(title))


def audio_files(root: Path) -> list[Path]:
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower().lstrip(".") in AUDIO_EXTS]
    return sorted(files, key=lambda p: natural_key(str(p.relative_to(root))))


def _move_or_copy(src: Path, dst: Path, copy: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copy2(src, dst)
    else:
        shutil.move(str(src), str(dst))


async def fetch_cover(url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(url)
            if r.status_code == 200 and (r.content[:3] == b"\xff\xd8\xff" or r.content[:4] == b"\x89PNG"):
                return r.content
    except httpx.HTTPError as e:
        log.warning("cover download failed: %s", e)
    return None


def _opf(book: dict) -> str:
    def el(tag, value, attrs=""):
        return f"    <{tag}{attrs}>{escape(str(value))}</{tag}>\n" if value else ""
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n',
           '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="asin">\n',
           '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">\n']
    out.append(el("dc:title", book.get("title")))
    credited = credited_authors(book)
    for name in credited:
        out.append(el("dc:creator", name, ' opf:role="aut"'))
    for a in book.get("authors") or []:
        if a.get("name") and a["name"] not in credited:
            code = OPF_ROLES.get((a.get("role") or "").split()[0] if a.get("role") else "", "ctb")
            out.append(el("dc:contributor", a["name"], f' opf:role="{code}"'))
    for n in book.get("narrators") or []:
        out.append(el("dc:creator", n, ' opf:role="nrt"'))
    out.append(el("dc:publisher", book.get("publisher")))
    out.append(el("dc:date", book.get("release_date") or book.get("year")))
    out.append(el("dc:language", book.get("language")))
    out.append(el("dc:description", book.get("description")))
    out.append(el("dc:identifier", book.get("asin"), ' id="asin" opf:scheme="ASIN"'))
    out.append(el("dc:identifier", book.get("isbn"), ' opf:scheme="ISBN"'))
    for g in book.get("genres") or []:
        out.append(el("dc:subject", g))
    for s in book.get("series") or []:
        out.append(f'    <meta name="calibre:series" content="{escape(s["name"], {chr(34): "&quot;"})}"/>\n')
        if s.get("sequence"):
            out.append(f'    <meta name="calibre:series_index" content="{escape(str(s["sequence"]))}"/>\n')
        break
    if book.get("subtitle"):
        out.append(f'    <meta name="subtitle" content="{escape(book["subtitle"], {chr(34): "&quot;"})}"/>\n')
    out.append("  </metadata>\n</package>\n")
    return "".join(out)


def _abs_metadata(book: dict, chapters_embedded: bool) -> dict:
    """Audiobookshelf's own metadata.json format (what 'store metadata with item' writes)."""
    series = []
    for s in book.get("series") or []:
        series.append(f"{s['name']} #{s['sequence']}" if s.get("sequence") else s["name"])
    data = {
        "tags": book.get("tags") or [],
        "chapters": [],
        "title": book.get("title"),
        "subtitle": book.get("subtitle"),
        "authors": credited_authors(book),
        "narrators": book.get("narrators") or [],
        "series": series,
        "genres": book.get("genres") or [],
        "publishedYear": book.get("year"),
        "publishedDate": book.get("release_date"),
        "publisher": book.get("publisher"),
        "description": book.get("description"),
        "isbn": book.get("isbn"),
        "asin": book.get("asin"),
        "language": book.get("language"),
        "explicit": bool(book.get("explicit")),
        "abridged": (book.get("format") or "").lower() == "abridged",
    }
    if chapters_embedded and book.get("chapters"):
        chs = book["chapters"]
        total = (book.get("runtime_ms") or 0) / 1000
        for i, c in enumerate(chs):
            start = c["start_ms"] / 1000
            end = chs[i + 1]["start_ms"] / 1000 if i + 1 < len(chs) else total
            data["chapters"].append({"id": i, "start": start, "end": end, "title": c["title"]})
    return data


def unique_dest(dest: Path, asin: str | None) -> Path:
    if not dest.exists():
        return dest
    marker = dest / ".narratarr.json"
    if marker.exists():
        try:
            if json.loads(marker.read_text()).get("book", {}).get("asin") == asin and asin:
                return dest  # same book → merge/replace in place
        except (OSError, ValueError):
            pass
    if not any(dest.iterdir()):
        return dest
    i = 2
    while True:
        cand = dest.with_name(f"{dest.name} ({asin or i})" if i == 2 and asin else f"{dest.name} ({i})")
        if not cand.exists():
            return cand
        i += 1


async def process_book(src: Path, book: dict, settings, *, copy: bool = False, source_info: dict | None = None,
                       log_fn=log.info, progress_fn=None) -> dict:
    """Organise + tag. Returns a summary used for the library record."""
    if not src.exists():
        raise FileNotFoundError(f"Source folder missing: {src}")
    files = audio_files(src) if src.is_dir() else [src]
    if not files:
        raise RuntimeError("No audio files found to import")
    base = src if src.is_dir() else src.parent

    dest = unique_dest(settings.completed / render_template(settings.get("paths.template"), book), book.get("asin"))
    dest.mkdir(parents=True, exist_ok=True)
    log_fn(f"Destination: {dest}")

    moved: list[Path] = []
    for p in files:
        rel = p.relative_to(base) if src.is_dir() else Path(p.name)
        target = dest / rel
        _move_or_copy(p, target, copy)
        moved.append(target)
    # bring along useful extras (images, pdf companions, cue sheets)
    if src.is_dir():
        for p in src.rglob("*"):
            if p.is_file() and p.suffix.lower() in EXTRA_KEEP and p.name.lower() not in JUNK:
                target = dest / p.relative_to(base)
                if not target.exists():
                    _move_or_copy(p, target, copy)
    moved = sorted(moved, key=lambda p: natural_key(str(p.relative_to(dest))))

    if settings.get("processing.reencode"):
        import asyncio
        from .reencode import current_kbps, effective_kbps, reencode, reencode_each, should_reencode
        kbps = int(settings.get("processing.reencode_kbps") or 64)
        merge = bool(settings.get("processing.reencode_merge"))
        if should_reencode(moved, kbps, merge):
            before = sum(p.stat().st_size for p in moved)
            was = current_kbps(moved)
            try:
                if merge:
                    kbps = effective_kbps(moved, kbps)
                    log_fn(f"Re-encoding {len(moved)} file(s) ({was or 0:.0f} kbps) to one M4B at {kbps} kbps")
                    moved = [await asyncio.to_thread(reencode, moved, dest,
                                                     safe_segment(book.get("title") or "Audiobook"),
                                                     book, kbps, progress_fn)]
                else:
                    log_fn(f"Re-encoding {len(moved)} file(s) ({was or 0:.0f} kbps) one by one at up to {kbps} kbps")
                    moved = await asyncio.to_thread(reencode_each, moved, kbps, progress_fn)
                log_fn(f"Re-encoded: {_mb(before)} → {_mb(sum(p.stat().st_size for p in moved))}")
            except Exception as e:  # noqa: BLE001
                moved = audio_files(dest)
                log_fn(f"Re-encode failed, keeping the original files: {e}")
        else:
            log_fn("Re-encode skipped: already at or below the target bitrate")

    cover = None
    if settings.get("processing.save_cover") or settings.get("processing.embed_cover"):
        cover = await fetch_cover(book.get("cover_url"))
        if cover and settings.get("processing.save_cover"):
            (dest / "cover.jpg").write_bytes(cover)
        elif not cover:
            log_fn("No cover art available from the provider")

    tag_result = {"tagged": 0, "skipped": [], "chapters": None}
    if settings.get("processing.write_tags"):
        import asyncio
        tag_result = await asyncio.to_thread(
            tag_book, moved, book, cover if settings.get("processing.embed_cover") else None,
            bool(settings.get("processing.embed_chapters")), log_fn,
        )
        log_fn(f"Tagged {tag_result['tagged']}/{len(moved)} files")

    chapters_embedded = bool(tag_result.get("chapters") and str(tag_result["chapters"]).startswith("embedded"))
    if settings.get("processing.sidecars"):
        if book.get("description"):
            (dest / "desc.txt").write_text(book["description"] + "\n", encoding="utf-8")
        if book.get("narrators"):
            (dest / "reader.txt").write_text(", ".join(book["narrators"]) + "\n", encoding="utf-8")
        (dest / "metadata.opf").write_text(_opf(book), encoding="utf-8")
        (dest / "metadata.json").write_text(json.dumps(_abs_metadata(book, chapters_embedded), indent=2,
                                                       ensure_ascii=False), encoding="utf-8")

    durations = [audio_length(p) or 0 for p in moved]
    summary = {
        "path": str(dest),
        "file_count": len(moved),
        "size_bytes": sum(p.stat().st_size for p in moved),
        "duration_s": round(sum(durations), 1),
        "cover_path": str(dest / "cover.jpg") if (dest / "cover.jpg").exists() else None,
        "chapters": tag_result.get("chapters"),
    }
    (dest / ".narratarr.json").write_text(json.dumps({
        "version": 1, "imported_at": time.time(), "book": book, "source": source_info or {}, "summary": summary,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    if src.is_dir() and not copy:
        _prune_empty(src)
    return summary


def ebook_files(root: Path, formats: list[str] | None = None) -> list[Path]:
    exts = set(formats or EBOOK_EXTS)
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower().lstrip(".") in exts]
    return sorted(files, key=lambda p: natural_key(str(p.relative_to(root))))


async def process_ebook(src: Path, book: dict, settings, *, copy: bool = False, source_info: dict | None = None,
                        log_fn=log.info) -> dict:
    """File an ebook into completed-ebooks/<template>/ with cover and sidecars. No tagging or re-encoding."""
    if not src.exists():
        raise FileNotFoundError(f"Source folder missing: {src}")
    files = ebook_files(src) if src.is_dir() else [src]
    if not files:
        raise RuntimeError("No ebook files found to import")
    base = src if src.is_dir() else src.parent
    book = {k: v for k, v in book.items() if k not in ("narrators", "chapters", "runtime_ms", "runtime_min")}

    dest = unique_dest(settings.completed_ebooks / render_template(settings.get("paths.template"), book),
                       book.get("asin"))
    dest.mkdir(parents=True, exist_ok=True)
    log_fn(f"Destination: {dest}")

    moved: list[Path] = []
    for p in files:
        target = dest / (p.relative_to(base) if src.is_dir() else Path(p.name))
        _move_or_copy(p, target, copy)
        moved.append(target)

    if settings.get("processing.save_cover"):
        cover = await fetch_cover(book.get("cover_url"))
        if cover:
            (dest / "cover.jpg").write_bytes(cover)
        else:
            log_fn("No cover art available from the provider")

    if settings.get("processing.sidecars"):
        if book.get("description"):
            (dest / "desc.txt").write_text(book["description"] + "\n", encoding="utf-8")
        (dest / "metadata.opf").write_text(_opf(book), encoding="utf-8")
        (dest / "metadata.json").write_text(json.dumps(_abs_metadata(book, False), indent=2, ensure_ascii=False),
                                            encoding="utf-8")

    summary = {
        "path": str(dest), "file_count": len(moved), "size_bytes": sum(p.stat().st_size for p in moved),
        "duration_s": None, "cover_path": str(dest / "cover.jpg") if (dest / "cover.jpg").exists() else None,
        "chapters": None, "media": "ebook",
        "formats": sorted({p.suffix.lower().lstrip(".") for p in moved}),
    }
    (dest / ".narratarr.json").write_text(json.dumps({
        "version": 1, "media": "ebook", "imported_at": time.time(), "book": book, "source": source_info or {},
        "summary": summary,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    log_fn(f"Filed {len(moved)} ebook file(s): {', '.join(p.name for p in moved)}")

    if src.is_dir() and not copy:
        _prune_empty(src)
    return summary


def _prune_empty(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        d = Path(dirpath)
        for fn in filenames:
            if fn.lower() in JUNK:
                try:
                    (d / fn).unlink()
                except OSError:
                    pass
        try:
            d.rmdir()
        except OSError:
            pass
