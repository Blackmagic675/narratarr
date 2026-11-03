"""Library rescan and Manual Import folder discovery."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .matching import AUDIO_EXTS, DISC_DIR_RE, suggest_query_from_folder


def rescan(db, completed: Path, completed_ebooks: Path | None = None) -> dict:
    """Rebuild library rows from .narratarr.json markers; drop rows whose folder is gone."""
    added = updated = removed = 0
    seen: set[str] = set()
    for root, media in ((completed, "audiobook"), (completed_ebooks, "ebook")):
        if not root or not root.exists():
            continue
        for marker in root.rglob(".narratarr.json"):
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            book = data.get("book") or {}
            summary = data.get("summary") or {}
            path = str(marker.parent)
            seen.add(path)
            values = {
                "asin": book.get("asin"), "title": book.get("title") or marker.parent.name,
                "subtitle": book.get("subtitle"), "authors": book.get("authors") or [],
                "narrators": book.get("narrators") or [], "series": book.get("series") or [],
                "path": path, "cover_path": str(marker.parent / "cover.jpg") if (marker.parent / "cover.jpg").exists() else None,
                "file_count": summary.get("file_count"), "size_bytes": summary.get("size_bytes"),
                "duration_s": summary.get("duration_s"), "book": book, "media": data.get("media") or media,
            }
            row = db.one("SELECT id FROM library WHERE path=?", (path,))
            if row:
                db.update("library", row["id"], values)
                updated += 1
            else:
                db.insert("library", {**values, "source": "scan", "added_at": data.get("imported_at") or time.time()})
                added += 1
    for row in db.query("SELECT id, path FROM library"):
        if row["path"] not in seen and not Path(row["path"]).exists():
            db.execute("DELETE FROM library WHERE id=?", (row["id"],))
            removed += 1
    return {"added": added, "updated": updated, "removed": removed}


def find_book_folders(root: Path, exclude: list[Path] | None = None, max_depth: int = 6) -> list[dict]:
    """Folders that directly hold audio, with disc sub-folders folded into their parent."""
    exclude = [e.resolve() for e in exclude or [] if e]
    root = root.resolve()
    books: dict[Path, dict] = {}
    if not root.exists():
        return []
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        rd = d.resolve()
        if any(rd == e or e in rd.parents for e in exclude):
            dirnames[:] = []
            continue
        depth = len(rd.relative_to(root).parts)
        if depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [n for n in dirnames if not n.startswith(".")]
        audio = [f for f in filenames if f.rsplit(".", 1)[-1].lower() in AUDIO_EXTS and not f.startswith(".")]
        if not audio:
            continue
        book_dir = d.parent if DISC_DIR_RE.match(d.name) and d != root else d
        entry = books.setdefault(book_dir, {"files": 0, "size": 0, "formats": set(), "mtime": 0.0})
        for f in audio:
            try:
                st = (d / f).stat()
            except OSError:
                continue
            entry["files"] += 1
            entry["size"] += st.st_size
            entry["mtime"] = max(entry["mtime"], st.st_mtime)
            entry["formats"].add(f.rsplit(".", 1)[-1].lower())
    out = []
    for path, e in books.items():
        rel = str(path.resolve().relative_to(root)) if path.resolve() != root else "."
        has_marker = (path / ".narratarr.json").exists()
        name = path.name if rel != "." else root.name
        suggestion = suggest_query_from_folder(name)
        if len(suggestion.split()) < 2 and rel != ".":
            suggestion = suggest_query_from_folder(f"{path.parent.name} {name}")
        out.append({
            "rel": rel, "name": path.name, "files": e["files"], "size": e["size"],
            "formats": sorted(e["formats"]), "mtime": e["mtime"], "already_imported": has_marker,
            "suggestion": suggestion,
            "parent": path.parent.name if rel != "." else "",
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out
