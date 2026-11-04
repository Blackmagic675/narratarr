"""Mock Audible + Audnexus + slskd for end-to-end testing.

Run:  python tests/mocks/mock_server.py <workdir> [port]
- Audible:   /{region}/1.0/catalog/products[/{asin}]
- Audnexus:  /audnexus/books/{asin}[/chapters]
- slskd:     /api/v0/...  (Soulseek AND-matching over a fake share list;
             downloads "complete" after a few seconds by copying generated audio
             into <workdir>/slskd-downloads, honouring batch destinations)
- /mock/config  POST {"batches": false} to emulate an older slskd
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

WORK = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/narratarr-mock").resolve()
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 9100
SRC = WORK / "src"
DL = WORK / "slskd-downloads"
BASE = f"http://127.0.0.1:{PORT}"
CONFIG = {"batches": True, "busy": 0, "search_secs": 2, "enqueue_delay": 0, "audible_down": False}


def ff(*args):
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found on PATH — install it (Windows: winget install Gyan.FFmpeg) and reopen the terminal")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


COVER_COLOURS = [("2b3a55", "e8a33d"), ("1d4e4a", "9fd3c7"), ("5a1f2b", "f2b880"), ("2e2a4f", "b39ddb"),
                 ("3b2f2f", "d7a86e"), ("16324f", "6fa3c7"), ("4a3b12", "e3c770"), ("26332a", "a3c48e")]


def cover_for(key: str) -> Path:
    i = sum(map(ord, key)) % len(COVER_COLOURS)
    p = SRC / f"cover{i}.jpg"
    return p if p.exists() else SRC / "cover.jpg"


def gen_media():
    SRC.mkdir(parents=True, exist_ok=True)
    if not (SRC / "single.m4b").exists():
        ff("-f", "lavfi", "-i", "sine=frequency=440:duration=60", "-c:a", "aac", "-b:a", "32k", "-f", "mp4", str(SRC / "single.m4b"))
    if not (SRC / "part.mp3").exists():
        ff("-f", "lavfi", "-i", "sine=frequency=330:duration=15", "-c:a", "libmp3lame", "-b:a", "32k", str(SRC / "part.mp3"))
    if not (SRC / "single.mp3").exists():
        ff("-f", "lavfi", "-i", "sine=frequency=550:duration=60", "-c:a", "libmp3lame", "-b:a", "32k", str(SRC / "single.mp3"))
    if not (SRC / "book.epub").exists():
        # a minimal, valid-looking EPUB container padded past the "tiny file" threshold
        import zipfile
        with zipfile.ZipFile(SRC / "book.epub", "w") as z:
            z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" '
                       'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles/></container>')
            z.writestr("filler.bin", bytes(range(256)) * 160, compress_type=zipfile.ZIP_STORED)
    if not (SRC / "book.pdf").exists():
        (SRC / "book.pdf").write_bytes(b"%PDF-1.4\n" + b"0" * 40_000 + b"\n%%EOF\n")
    if not (SRC / "cover.jpg").exists():
        ff("-f", "lavfi", "-i", "color=c=0xe8a33d:s=500x500", "-frames:v", "1", str(SRC / "cover.jpg"))
    for i, (a, b) in enumerate(COVER_COLOURS):
        out = SRC / f"cover{i}.jpg"
        if not out.exists():
            ff("-f", "lavfi", "-i", f"gradients=s=500x500:c0=0x{a}:c1=0x{b}:x0=0:y0=0:x1=500:y1=500:nb_colors=2",
               "-frames:v", "1", str(out))


def product(asin, title, authors, narrators, runtime, series=None, subtitle=None, fmt="unabridged", year="2021"):
    return {
        "asin": asin, "title": title, "subtitle": subtitle,
        "authors": [{"name": a, "asin": "B0AUTH" + str(i)} for i, a in enumerate(authors)],
        "narrators": [{"name": n} for n in narrators],
        "series": [{"title": s, "sequence": q, "asin": "B0SER"} for s, q in (series or [])],
        "runtime_length_min": runtime, "release_date": f"{year}-05-04", "publisher_name": "Mock Studios",
        "publisher_summary": f"<p>{title} is a <b>test</b> book.</p><p>Second paragraph &amp; more.</p>",
        "language": "english", "format_type": fmt, "content_delivery_type": "SinglePartBook",
        "product_images": {"500": f"{BASE}/cover/{asin}._SL500_.jpg"},
        "category_ladders": [{"ladder": [{"id": "1", "name": "Science Fiction & Fantasy"}, {"id": "2", "name": "Science Fiction"}]}],
    }


CATALOG = {
    "au": [
        product("B0TINY0001", "The Tiny Test: A Novel", ["Jane Author"], ["Ray Reader"], 1, [("Tiny Saga", "1")]),
        product("B0TINY0002", "The Tiny Test: A Novel", ["Jane Author"], ["Other Voice"], 1),
        product("B0LONG0001", "Long Road Home", ["Sam Writer"], ["Kim Narrator"], 1, [("Road Series", "2")]),
        product("B0FAIL0001", "Fallback Book", ["Fay Backer"], ["Nora Speaker"], 1),
        product("B0NONE0001", "Nowhere To Be Found", ["Ghost Writer"], ["No One"], 600),
        product("B0IMP00001", "Imported Tales", ["Ima Porter"], ["Ivy Voice"], 1),
        product("B0SHELF001", "Shelf Bound", ["Abe Shelver"], ["Nia Reader"], 1, [("Shelf Saga", "3")]),
        product("B0EXIST001", "Existing Story", ["Old Author"], ["Prior Voice"], 1),
        product("B0CLASS001", "Shelf Classic", ["Ada Shelf"], ["Some Reader"], 1),
        {**product("B0GAP00001", "Gap Book", ["Gwen Gap"], [], 1), "publisher_summary": None, "series": []},
        product("B0CHAP0001", "Chapter Book", ["Cora Chapter"], ["Pat Reader"], 1),
        product("B0VAN00001", "Vanish Book", ["Val Vanish"], ["Vic Reader"], 1),
        product("B0EBOOKFC1", "Paper Lantern (Full-Cast Edition)", ["Ella Page"], ["A Cast"], 1,
                [("Lantern Tales (Full-Cast Editions)", "1")]),
        product("B0EBOOK001", "Paper Lantern", ["Ella Page"], ["Ned Voice"], 1, [("Lantern Tales", "1")]),
        product("B0EBOOK002", "Paper Lantern", ["Ella Page"], ["Other Narrator"], 1, [("Lantern Tales", "1")]),
        product("B0EDIT0001", "Essays On Rivers", ["Lena Krause - editor", "Omar Field - foreword"],
                ["Contributing Authors", "Lena Krause"], 1),
    ],
    "us": [
        product("B0USONLY01", "American Exclusive", ["Usa Author"], ["Us Narrator"], 1),
    ],
}
CHAPTERS = {
    "B0SHELF001": [("Opening", 0, 20000), ("Middle", 20000, 20000), ("End", 40000, 20000)],
    "B0TINY0001": [("Opening Credits", 0, 5000), ("Chapter 1", 5000, 25000), ("Chapter 2", 30000, 25000), ("End Credits", 55000, 5000)],
}

# remote shares: (username, remote path, source file, length seconds, peer extras)
SHARES = [
    ("goodpeer", "@@good\\Audiobooks\\Jane Author\\Tiny Saga\\The Tiny Test (Unabridged) - Ray Reader\\The Tiny Test.m4b", "single.m4b", 60, {}),
    ("otherpeer", "@@oth\\Books\\Jane Author - The Tiny Test (read by Other Voice)\\The Tiny Test.mp3", "single.mp3", 61, {}),
    ("abridgedpeer", "@@abr\\Jane Author\\The Tiny Test [Abridged]\\tiny.mp3", "single.mp3", 30, {}),
    # multi-file with discs
    *[("discpeer", f"@@disc\\Audio\\Sam Writer\\Long Road Home\\CD{d}\\{d}-{t:02d} Long Road Home.mp3", "part.mp3", 15, {})
      for d in (1, 2) for t in (1, 2)],
    # fallback scenario: first (best) peer errors, second works
    ("flakypeer", "@@fl\\Fay Backer\\Fallback Book\\Fallback Book.m4b", "single.m4b", 60, {"fail": True, "speed": 9_000_000}),
    ("steadypeer", "@@st\\audiobooks\\Fay Backer - Fallback Book\\Fallback Book.m4b", "single.m4b", 60, {"speed": 200_000}),
    # series dump folder containing several books
    ("dumppeer", "@@dump\\Jane Author\\Everything\\The Tiny Test.m4b", "single.m4b", 60, {}),
    ("dumppeer", "@@dump\\Jane Author\\Everything\\Some Other Book.m4b", "single.m4b", 60, {}),
    *[("chappeer", f"@@ch\\Audiobooks\\Cora Chapter\\Chapter Book\\0{n} - Chapter Book.mp3", "part.mp3", 15, {})
      for n in (1, 2, 3)],
    *[("vanpeer", f"@@va\\Audiobooks\\Val Vanish\\Vanish Book\\0{n} - Vanish Book.mp3", "part.mp3", 15,
       {"vanish_once": n == 2}) for n in (1, 2, 3)],
    ("indiepeer", "@@in\\Audiobooks\\Ivy Indie\\Indie Tale\\Indie Tale.m4b", "single.m4b", 60, {}),
    ("essaypeer", "@@es\\Audiobooks\\Lena Krause\\Essays On Rivers\\Essays On Rivers.m4b", "single.m4b", 60, {}),
    ("shelfpeer", "@@sh\\Audiobooks\\Abe Shelver\\Shelf Saga 3 - Shelf Bound\\Shelf Bound.m4b", "single.m4b", 60, {}),
    # ebooks: an epub in a shelf folder (next to another title), a pdf elsewhere, and the audiobook
    ("ebookpeer", "@@eb\\Books\\Ella Page\\Paper Lantern.epub", "book.epub", None, {}),
    ("ebookpeer", "@@eb\\Books\\Ella Page\\Lantern Tales Book 2 - Paper Lantern Returns.epub", "book.epub", None, {}),
    ("pdfpeer", "@@pd\\Ebooks\\Ella Page - Paper Lantern.pdf", "book.pdf", None, {"speed": 9_000_000}),
    ("lanternaudio", "@@la\\Audiobooks\\Ella Page\\Paper Lantern\\Paper Lantern.m4b", "single.m4b", 60, {}),
]

app = FastAPI()
SEARCHES: dict[str, dict] = {}
VANISHED: set[str] = set()   # files an "auto-importer" already took once
TRANSFERS: dict[str, list[dict]] = {}
BATCH_DEST: dict[str, str] = {}


# ------------------------------------------------------------------ Audible
@app.get("/{region}/1.0/catalog/products")
def search(region: str, keywords: str = ""):
    if CONFIG["audible_down"]:
        raise HTTPException(503)
    words = [w for w in re.split(r"\W+", keywords.lower()) if w]
    out = []
    for p in CATALOG.get(region, []):
        hay = " ".join([p["title"], *(a["name"] for a in p["authors"]), *(n["name"] for n in p["narrators"])]).lower()
        if all(w in hay for w in words):
            out.append(p)
    return {"products": out, "total_results": len(out)}


@app.get("/{region}/1.0/catalog/products/{asin}")
def get_product(region: str, asin: str):
    if CONFIG["audible_down"]:
        raise HTTPException(503)
    for p in CATALOG.get(region, []):
        if p["asin"] == asin:
            return {"product": p}
    return {"product": {"asin": asin}}


@app.get("/cover/{name}")
def get_cover(name: str):
    return FileResponse(cover_for(name.split(".")[0]), media_type="image/jpeg")


# ----------------------------------------------------------------- Audnexus
@app.get("/audnexus/books/{asin}")
def nx_book(asin: str, region: str = "us"):
    for p in CATALOG.get(region, []):
        if p["asin"] == asin:
            return {"asin": asin, "genres": [{"asin": "1", "name": "Science Fiction & Fantasy", "type": "genre"},
                                             {"asin": "2", "name": "Space Opera", "type": "tag"}],
                    "isbn": "9780000000001", "summary": "x"}
    raise HTTPException(404)


@app.get("/audnexus/books/{asin}/chapters")
def nx_chapters(asin: str, region: str = "us"):
    if asin not in CHAPTERS:
        raise HTTPException(404)
    chs = CHAPTERS[asin]
    return {"asin": asin, "chapters": [{"title": t, "startOffsetMs": s, "lengthMs": l, "startOffsetSec": s // 1000} for t, s, l in chs],
            "runtimeLengthMs": sum(c[2] for c in chs), "isAccurate": True}


# -------------------------------------------------------------------- slskd
def require_key(request: Request):
    if request.headers.get("X-API-Key") != "testkey":
        raise HTTPException(401)


@app.get("/github/releases/latest")
async def gh_latest():
    return {"tag_name": "v0.2.0", "html_url": "https://github.com/joshbosh/narratarr/releases/tag/v0.2.0",
            "published_at": "2026-10-22T00:00:00Z",
            "body": "## What's Changed\n* Example change by @someone in https://github.com/joshbosh/narratarr/pull/9\n\n"
                    "**Full Changelog**: https://github.com/joshbosh/narratarr/compare/v0.1.9...v0.2.0"}


@app.post("/mock/config")
def mock_config(body: dict = Body(...)):
    CONFIG.update(body)
    return CONFIG


@app.get("/api/v0/application")
def application(request: Request):
    require_key(request)
    return {"version": {"full": "0.mock"}, "server": {"state": "Connected, LoggedIn", "isConnected": True, "isLoggedIn": True, "username": "mockuser"}}


@app.post("/api/v0/searches")
def start_search(request: Request, body: dict = Body(...)):
    require_key(request)
    words = [w for w in re.split(r"\W+", body["searchText"].lower()) if w]
    by_user: dict[str, dict] = {}
    for user, path, src, length, extra in SHARES:
        hay = re.sub(r"\W+", " ", path.lower())
        if all(re.search(rf"\b{re.escape(w)}", hay) for w in words):
            r = by_user.setdefault(user, {"username": user, "fileCount": 0, "files": [], "hasFreeUploadSlot": True,
                                          "queueLength": 0, "uploadSpeed": extra.get("speed", 1_500_000),
                                          "lockedFileCount": 0, "lockedFiles": [], "token": 1})
            r["files"].append({"filename": path, "size": (SRC / src).stat().st_size, "extension": path.rsplit(".", 1)[-1],
                               "length": length, "bitRate": 32, "code": 1, "isLocked": False})
            r["fileCount"] += 1
    SEARCHES[body["id"]] = {"id": body["id"], "searchText": body["searchText"], "state": "InProgress", "isComplete": False,
                            "responses": list(by_user.values()), "started": time.time()}
    return {k: v for k, v in SEARCHES[body["id"]].items() if k != "responses"}


@app.get("/api/v0/searches/{sid}")
def search_state(sid: str):
    s = SEARCHES.get(sid) or {}
    if not s:
        raise HTTPException(404)
    if time.time() - s["started"] > CONFIG["search_secs"]:
        s["state"], s["isComplete"] = "Completed, TimedOut", True
    return {k: v for k, v in s.items() if k != "responses"}


@app.put("/api/v0/searches/{sid}")
def stop_search(sid: str):
    return {}


@app.get("/api/v0/searches/{sid}/responses")
def search_responses(sid: str):
    return SEARCHES[sid]["responses"]


@app.delete("/api/v0/searches/{sid}")
def delete_search(sid: str):
    SEARCHES.pop(sid, None)
    return {}


def _busy():
    if CONFIG["busy"] > 0:
        CONFIG["busy"] -= 1
        return JSONResponse("The wait timed out after 5000 milliseconds", status_code=500)
    return None


def _enqueue(username: str, files: list[dict], dest: str | None):
    if CONFIG["enqueue_delay"]:
        time.sleep(CONFIG["enqueue_delay"])  # real slskd blocks while it connects to the peer
    lst = TRANSFERS.setdefault(username, [])
    for f in files:
        share = next(s for s in SHARES if s[1] == f["filename"])
        lst.append({"id": str(uuid.uuid4()), "username": username, "filename": f["filename"], "size": f["size"],
                    "requestedAt": f"{time.time():.6f}", "created": time.time(), "dest": dest, "src": share[2],
                    "fail": share[4].get("fail", False), "vanish_once": share[4].get("vanish_once", False),
                    "removed": False, "state": "Requested"})


@app.post("/api/v0/transfers/downloads/batches")
def batches(request: Request, body: dict = Body(...)):
    require_key(request)
    if (b := _busy()):
        return b
    if not CONFIG["batches"]:
        # what older slskd actually does: routes "batches" as a username and fails to parse the body
        return JSONResponse("The JSON value could not be converted to System.Collections.Generic.IEnumerable`1"
                            "[slskd.Transfers.API.QueueDownloadRequest]. Path: $ | LineNumber: 0 | BytePositionInLine: 1.",
                            status_code=400)
    _enqueue(body["username"], body["files"], body.get("options", {}).get("destination"))
    return JSONResponse({"batch": {"id": body.get("id")}, "failures": []}, status_code=201)


@app.post("/api/v0/transfers/downloads/{username}")
def legacy(request: Request, username: str, body: list = Body(...)):
    require_key(request)
    if (b := _busy()):
        return b
    _enqueue(username, body, None)
    return JSONResponse({}, status_code=201)


def _advance(t: dict):
    age = time.time() - t["created"]
    if t["state"].startswith("Completed"):
        return
    if age < 1.5:
        t["state"], t["bytesTransferred"], t["percentComplete"] = "Queued, Remotely", 0, 0
    elif age < 3:
        t["state"], t["bytesTransferred"], t["percentComplete"] = "InProgress", t["size"] // 2, 50.0
    elif t["fail"]:
        t["state"], t["bytesTransferred"], t["percentComplete"] = "Completed, Errored", t["size"] // 2, 50.0
    else:
        parts = re.split(r"[\\/]+", t["filename"])
        sub = t["dest"] if t["dest"] else parts[-2]
        target = DL / sub / parts[-1]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SRC / t["src"], target)
        if t.get("vanish_once") and t["filename"] not in VANISHED:
            VANISHED.add(t["filename"])
            target.unlink()              # simulate another app moving the finished file away
        t["state"], t["bytesTransferred"], t["percentComplete"] = "Completed, Succeeded", t["size"], 100.0


@app.get("/api/v0/transfers/downloads/{username}")
def user_downloads(request: Request, username: str):
    require_key(request)
    files = []
    for t in TRANSFERS.get(username, []):
        if t["removed"]:
            continue
        _advance(t)
        files.append({k: v for k, v in t.items() if k not in ("created", "src", "fail", "dest", "removed", "vanish_once")})
    if not files:
        raise HTTPException(404)
    return {"username": username, "directories": [{"directory": "x", "fileCount": len(files), "files": files}]}


@app.delete("/api/v0/transfers/downloads/{username}/{tid}")
def cancel(username: str, tid: str, remove: bool = False):
    for t in TRANSFERS.get(username, []):
        if t["id"] == tid:
            if not t["state"].startswith("Completed"):
                t["state"] = "Completed, Cancelled"
            if remove:
                t["removed"] = True
    return {}


# ----------------------------------------------------------------- Hardcover
def _hc_contrib(name, role=None):
    return {"contribution": role, "author": {"name": name}}


def _hc_book(bid, title, authors, narrators, asin, secs, series=None, desc=None, genres=None, isbn=None, audio=True):
    edition = {"id": bid * 10, "asin": asin, "isbn_13": isbn, "audio_seconds": secs, "release_date": "2020-03-01",
               "reading_format_id": 2, "image": {"url": f"{BASE}/cover/hc{bid}.jpg"}, "publisher": {"name": "HC Press"},
               "language": {"language": "English"},
               "contributions": [_hc_contrib(n, "Narrator") for n in narrators]}
    return {"id": bid, "title": title, "subtitle": None, "description": desc, "release_date": "2020-03-01",
            "release_year": 2020, "slug": title.lower().replace(" ", "-"),
            "cached_tags": {"Genre": [{"tag": g} for g in (genres or [])]},
            "image": {"url": f"{BASE}/cover/hc{bid}.jpg"},
            "contributions": [_hc_contrib(a) for a in authors],
            "book_series": [{"position": series[1], "series": {"name": series[0]}}] if series else [],
            "default_audio_edition": edition if audio else None}


HC_BOOKS = {
    101: _hc_book(101, "The Tiny Test", ["Jane Author"], ["Ray Reader"], "B0TINY0001", 60, genres=["Fantasy"],
                  isbn="9780000000101"),
    102: _hc_book(102, "Gap Book", ["Gwen Gap"], ["Hal Cover"], "B0GAP00001", 60, series=("Gap Saga", 2.0),
                  desc="<p>Description from Hardcover.</p>", genres=["Mystery"]),
    103: _hc_book(103, "Indie Tale", ["Ivy Indie"], ["Nell Narr"], None, 60, genres=["Literary"]),
    104: _hc_book(104, "Paper Lantern", ["Ella Page"], [], None, None, series=("Lantern Tales", 1.0),
                  desc="<p>The printed story of a paper lantern.</p>", genres=["Fantasy"], audio=False),
}


@app.post("/hardcover/v1/graphql")
def hardcover(request: Request, body: dict = Body(...)):
    if request.headers.get("authorization") != "Bearer hctoken":
        return JSONResponse({"error": "Unable to verify token"}, status_code=401)
    q, v = body.get("query", ""), body.get("variables") or {}
    if "search(" in q:
        words = [w for w in re.split(r"\W+", v.get("q", "").lower()) if w]
        ids = [bid for bid, b in HC_BOOKS.items()
               if all(w in (b["title"] + " " + " ".join(c["author"]["name"] for c in b["contributions"])).lower() for w in words)]
        return {"data": {"search": {"ids": [str(i) for i in ids]}}}
    if "books(" in q:
        return {"data": {"books": [HC_BOOKS[i] for i in v.get("ids", []) if i in HC_BOOKS]}}
    if "editions(" in q:
        rows = []
        for b in HC_BOOKS.values():
            ed = b["default_audio_edition"]
            if ed and ed["asin"] and ed["asin"] == v.get("asin"):
                rows.append({**ed, "book": b})
        return {"data": {"editions": rows}}
    return {"errors": [{"message": "unsupported query in mock"}]}


# ------------------------------------------------------------ Audiobookshelf
# ABS "sees" its folder at /audiobooks; on disk that's <WORK>/abs-library (path mapping test).
ABS_PATH = "/audiobooks"
ABS_LOCAL = WORK / "abs-library"
EBOOK_PATH = "/ebooks"
EBOOK_LOCAL = WORK / "abs-ebooks"
ROOTS = [("lib_books", ABS_PATH, ABS_LOCAL), ("lib_ebooks", EBOOK_PATH, EBOOK_LOCAL)]
ABS = {"items": {}, "pending": [], "watcher": True}


def abs_auth(request: Request):
    if request.headers.get("Authorization") != "Bearer abstoken":
        raise HTTPException(401)


def _abs_local(p: str) -> Path:
    for _lib, remote, local in ROOTS:
        if p == remote or p.startswith(remote + "/"):
            return local / p[len(remote):].lstrip("/")
    raise HTTPException(400, f"path outside library folders: {p}")


def _root_of(p: Path) -> tuple[str, str, Path]:
    return next(r for r in ROOTS if p == r[2] or r[2] in p.parents)


def _abs_remote(p: Path) -> str:
    _lib, remote, local = _root_of(p)
    return remote + "/" + p.relative_to(local).as_posix()


def _abs_seed():
    for iid, title, author, asin, rel in (("li_exist", "Existing Story", "Old Author", "B0EXIST001", "Old Author/Existing Story"),
                                          ("li_class", "Shelf Classic", "Ada Shelf", None, "Ada Shelf/Shelf Classic")):
        d = ABS_LOCAL / rel
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy(SRC / "part.mp3", d / "01.mp3")
        shutil.copy(cover_for(iid), d / "cover.jpg")
        ABS["items"][iid] = _abs_item(iid, d, {"title": title, "authors": [author], "asin": asin, "narrators": []})


def _abs_item(iid: str, d: Path, meta: dict) -> dict:
    audio = sorted(p for p in d.rglob("*") if p.suffix.lower() in (".mp3", ".m4b", ".m4a", ".flac"))
    ebooks = sorted(p.name for p in d.rglob("*") if p.suffix.lower() in (".epub", ".pdf", ".mobi", ".azw3"))
    return {"id": iid, "lib": _root_of(d)[0], "ebooks": ebooks,
            "path": _abs_remote(d), "addedAt": int(time.time() * 1000), "size": sum(p.stat().st_size for p in audio),
            "audio": [p.name for p in audio], "duration": 60.0 if len(audio) == 1 else 15.0 * len(audio),
            "chapters": [], "cover": (d / "cover.jpg").exists(),
            "meta": {"title": meta.get("title"), "subtitle": None, "authors": meta.get("authors") or [],
                     "narrators": meta.get("narrators") or [], "series": [], "genres": [], "publishedYear": None,
                     "publishedDate": None, "publisher": meta.get("publisher"), "description": None, "isbn": None,
                     "asin": meta.get("asin"), "language": None}}


def _abs_scan_dir(d: Path):
    if any(it["path"] == _abs_remote(d) for it in ABS["items"].values()):
        return
    # Simulated scanner: reads only folder names + a fixed publisher (so Narratarr has gaps to fill and a conflict)
    parts = d.relative_to(_root_of(d)[2]).parts
    title = parts[-1].split(" - ", 1)[-1] if parts[-1].startswith("Book ") else parts[-1]
    iid = "li_" + uuid.uuid4().hex[:8]
    ABS["items"][iid] = _abs_item(iid, d, {"title": title, "authors": [parts[0]], "publisher": "Scanner Pub"})


def _book_dir(local_file: Path) -> Path:
    d = local_file.parent
    root = _root_of(local_file)[2]
    while d != root and not (d / ".narratarr.json").exists():
        d = d.parent
    return d


def _abs_tick():
    now = time.time()
    for due, d in list(ABS["pending"]):
        if now >= due:
            _abs_scan_dir(d)
            ABS["pending"].remove((due, d))


def _min(it):
    m = it["meta"]
    return {"id": it["id"], "path": it["path"], "addedAt": it["addedAt"], "size": it["size"], "mediaType": "book",
            "media": {"metadata": {"title": m["title"], "subtitle": m["subtitle"], "authorName": ", ".join(m["authors"]),
                                   "narratorName": ", ".join(m["narrators"]),
                                   "seriesName": ", ".join(f"{s['name']} #{s['sequence']}" if s.get("sequence") else s["name"] for s in m["series"]),
                                   "asin": m["asin"]},
                      "coverPath": "/x/cover.jpg" if it["cover"] else None, "numTracks": len(it["audio"]),
                      "numChapters": len(it["chapters"]), "duration": it["duration"]}}


@app.post("/mock/abs")
def mock_abs_config(body: dict = Body(...)):
    ABS.update(body)
    return {k: v for k, v in ABS.items() if k == "watcher"}


@app.get("/mock/abs/items")
def mock_abs_items():
    return ABS["items"]


@app.get("/abs/api/libraries")
def abs_libraries(request: Request):
    abs_auth(request)
    return {"libraries": [{"id": "lib_books", "name": "Audiobooks", "mediaType": "book",
                           "folders": [{"id": "fol_main", "fullPath": ABS_PATH}]},
                          {"id": "lib_ebooks", "name": "Ebooks", "mediaType": "book",
                           "folders": [{"id": "fol_eb", "fullPath": EBOOK_PATH}]},
                          {"id": "lib_pod", "name": "Podcasts", "mediaType": "podcast", "folders": [{"id": "fol_p", "fullPath": "/podcasts"}]}]}


@app.get("/abs/api/libraries/{lid}/items")
def abs_items(request: Request, lid: str, limit: int = 0, page: int = 0):
    abs_auth(request)
    _abs_tick()
    items = [_min(it) for it in ABS["items"].values() if it["lib"] == lid]
    if limit:
        chunk = items[page * limit:(page + 1) * limit]
    else:
        chunk = items
    return {"results": chunk, "total": len(items), "limit": limit, "page": page}


@app.post("/abs/api/watcher/update")
def abs_watcher(request: Request, body: dict = Body(...)):
    abs_auth(request)
    if ABS["watcher"]:
        d = _book_dir(_abs_local(body["path"]))
        if all(x[1] != d for x in ABS["pending"]):
            ABS["pending"].append((time.time() + 2, d))
    return JSONResponse("OK")


@app.post("/abs/api/libraries/{lid}/scan")
def abs_scan(request: Request, lid: str):
    abs_auth(request)
    local = next(r[2] for r in ROOTS if r[0] == lid)
    for marker in local.rglob(".narratarr.json"):
        ABS["pending"].append((time.time() + 1, marker.parent))
    return JSONResponse("OK")


@app.get("/abs/api/items/{iid}")
def abs_item(request: Request, iid: str):
    abs_auth(request)
    it = ABS["items"].get(iid) or {}
    if not it:
        raise HTTPException(404)
    m = dict(it["meta"])
    m["authors"] = [{"id": "au", "name": a} for a in m["authors"]]
    m["series"] = [{"id": "se", "name": x["name"], "sequence": x.get("sequence")} for x in m["series"]]
    return {"id": iid, "path": it["path"], "media": {"metadata": m, "chapters": it["chapters"], "duration": it["duration"],
                                                     "audioFiles": [{"metadata": {"filename": a}} for a in it["audio"]]}}


@app.patch("/abs/api/items/{iid}/media")
def abs_update(request: Request, iid: str, body: dict = Body(...)):
    abs_auth(request)
    m = ABS["items"][iid]["meta"]
    for k, v in (body.get("metadata") or {}).items():
        m[k] = [a["name"] for a in v] if k == "authors" else v
    return {"updated": True}


@app.post("/abs/api/items/{iid}/chapters")
def abs_chapters(request: Request, iid: str, body: dict = Body(...)):
    abs_auth(request)
    ABS["items"][iid]["chapters"] = body["chapters"]
    return {"success": True, "updated": True}


@app.post("/abs/api/items/{iid}/scan")
def abs_scan_item(request: Request, iid: str):
    abs_auth(request)
    it = ABS["items"][iid]
    d = _abs_local(it["path"])
    audio = sorted(p for p in d.rglob("*") if p.suffix.lower() in (".mp3", ".m4b", ".m4a", ".flac"))
    it["audio"] = [p.name for p in audio]
    it["size"] = sum(p.stat().st_size for p in audio)
    ABS["scanned"] = ABS.get("scanned", []) + [iid]
    return {"result": "UPDATED"}


@app.get("/abs/api/items/{iid}/cover")
def abs_cover(request: Request, iid: str):
    abs_auth(request)
    return FileResponse(cover_for(iid), media_type="image/jpeg")


if __name__ == "__main__":
    gen_media()
    DL.mkdir(parents=True, exist_ok=True)
    for _lib, _remote, local in ROOTS:
        shutil.rmtree(local, ignore_errors=True)
        local.mkdir(parents=True, exist_ok=True)
    _abs_seed()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
