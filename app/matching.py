"""Query generation and candidate scoring for slskd results.

Soulseek searches AND every word against the full remote path, so the trick is
to try a handful of progressively looser queries (author + short title, title
alone, series + number, ...) and stop at the first one that produces a
confident match. Results are grouped per user + folder (with CD1/Disc 2/Part 3
sub-folders folded into their parent), scored against the Audible record, and
ranked. Runtime is the strongest disambiguator — it separates editions,
abridgements and different narrations of the same title.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from typing import Any

from rapidfuzz import fuzz

AUDIO_EXTS = {"m4b", "mp3", "m4a", "flac", "opus", "ogg", "aac", "wma", "wav", "mp4"}
EBOOK_EXTS = {"epub", "azw3", "azw", "mobi", "pdf", "kepub", "fb2", "cbz", "cbr", "djvu", "lit"}
STOPWORDS = {"the", "a", "an", "of", "and", "to", "in", "on", "at", "for", "with", "by", "from", "is", "de", "la", "le"}
DISC_DIR_RE = re.compile(r"^(?:cd|dis[ck]|part|pt|teil)[\s._-]*\d{1,3}\b.*$", re.I)
DISC_SUFFIX_RE = re.compile(r"^(?P<base>.+?)[\s._-]*[\[(]?(?:cd|dis[ck])[\s._-]*\d{1,3}[\])]?$", re.I)
SEQ_IN_PATH_RE = re.compile(r"(?:book|bk|vol(?:ume)?|#|no\.?|part)\s*0*(\d{1,3}(?:\.\d)?)\b", re.I)


# --------------------------------------------------------------------------- text
def norm(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("&", " and ").replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s: str) -> list[str]:
    return [t for t in norm(s).split() if t]


def significant(s: str) -> list[str]:
    toks = tokens(s)
    sig = [t for t in toks if t not in STOPWORDS]
    return sig or toks


def short_title(title: str) -> str:
    """'The Example Title: A Saga, Book 1' → 'The Example Title'."""
    t = re.split(r"\s*[:–—]\s+|\s+-\s+", title, maxsplit=1)[0]
    t = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*", " ", t)
    t = re.sub(r",?\s*(?:book|volume|vol\.?)\s*\d+\s*$", "", t, flags=re.I)
    return t.strip() or title.strip()


# Narrator credits that aren't people; never use them as search words or match signals.
NON_PERSON_NARRATORS = {"contributing authors", "various", "various narrators", "full cast", "a full cast",
                        "uncredited", "anonymous", "the author", "author", "multiple narrators"}


def person_name(name: str | None) -> str:
    """Strip Audible role suffixes ('X - editor') from a contributor name."""
    from .providers.base import split_role
    return split_role(name)[0]


def primary_author(book: dict) -> str:
    """First author credited as an author (not editor/translator/foreword), else the first one listed."""
    authors = book.get("authors") or []
    for a in authors:
        name, role = person_name(a.get("name")), a.get("role")
        if not role and name and person_name(a.get("name")) == (a.get("name") or "").strip():
            return name
    return person_name(authors[0].get("name")) if authors else ""


def narrator_names(book: dict) -> list[str]:
    out = []
    for n in book.get("narrators") or []:
        n = person_name(n)
        if n and norm(n) not in NON_PERSON_NARRATORS:
            out.append(n)
    return out


def last_name(name: str | None) -> str:
    toks = tokens(person_name(name))
    # skip suffixes like 'jr', 'iii'
    toks = [t for t in toks if t not in {"jr", "sr", "ii", "iii", "iv", "phd", "md"}]
    return toks[-1] if toks else ""


def coverage(needles: list[str], hay: set[str]) -> float:
    """Fraction of needle tokens found (fuzzily) in hay tokens."""
    if not needles:
        return 0.0
    hit = 0.0
    for n in needles:
        if n in hay:
            hit += 1
        elif len(n) >= 4 and any(fuzz.ratio(n, h) >= 86 for h in hay if abs(len(h) - len(n)) <= 2):
            hit += 0.85
    return hit / len(needles)


def split_remote(path: str) -> list[str]:
    return [p for p in re.split(r"[\\/]+", path) if p]


# ------------------------------------------------------------------------ queries
def build_queries(book: dict, limit: int = 6) -> list[str]:
    title = book.get("title") or ""
    st = short_title(title)
    st_sig = significant(st)
    author = primary_author(book)
    a_last = last_name(author)
    narrators = narrator_names(book)
    narr_last = last_name(narrators[0]) if narrators else ""
    series = (book.get("series") or [None])[0]

    def q(*parts: str) -> str:
        # Soulseek clients match whole words, so "Philosopher's" is indexed as
        # "philosopher" + "s": split on apostrophes and drop 1-letter fragments
        # (initials like "J. K." too). Digits are kept for series numbers.
        words: list[str] = []
        for p in parts:
            p = re.sub(r"['\u2019]", " ", p or "")
            for w in tokens(p):
                if (len(w) > 1 or w.isdigit()) and w not in words:
                    words.append(w)
        return " ".join(words)

    distinctive = len(st_sig) >= 2 or (st_sig and len(st_sig[0]) >= 7)
    candidates = [
        q(author, st),
        q(a_last, " ".join(st_sig)),
        q(st) if distinctive else "",
        q(" ".join(st_sig[:4]), narr_last) if narr_last else "",
        q(series["name"], series.get("sequence") or "", a_last) if series and series.get("name") else "",
        q(a_last, " ".join(st_sig[:3])) if len(st_sig) > 3 else "",
        q(title),
    ]
    out: list[str] = []
    for c in candidates:
        # Soulseek ignores very short queries and treats leading '-' as exclusion
        if c and len(c) >= 4 and c not in out:
            out.append(c)
    return out[:limit]


# ---------------------------------------------------------------------- candidates
def _file_entry(f: dict, base_dir: str) -> dict:
    fn = f.get("filename", "")
    parts = split_remote(fn)
    remote_dir = "\\".join(parts[:-1])
    rel = remote_dir[len(base_dir):].strip("\\/") if remote_dir.startswith(base_dir) else ""
    ext = (f.get("extension") or (parts[-1].rsplit(".", 1)[-1] if "." in parts[-1] else "")).lower().lstrip(".")
    return {
        "filename": fn,
        "name": parts[-1] if parts else fn,
        "rel": rel.replace("\\", "/"),
        "size": int(f.get("size") or 0),
        "length": f.get("length"),
        "bitrate": f.get("bitRate"),
        "ext": ext,
    }


def group_response(resp: dict, formats: list[str]) -> dict[str, list[dict]]:
    """Group a peer's audio files by book folder, folding disc/part sub-folders."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for f in resp.get("files") or []:
        parts = split_remote(f.get("filename", ""))
        if len(parts) < 2:
            continue
        ext = (f.get("extension") or parts[-1].rsplit(".", 1)[-1]).lower().lstrip(".")
        if ext not in formats:
            continue
        dirs = parts[:-1]
        base = dirs
        if len(dirs) >= 2 and DISC_DIR_RE.match(dirs[-1]):
            base = dirs[:-1]
        elif len(dirs) >= 2:
            m = DISC_SUFFIX_RE.match(dirs[-1])
            if m and len(m.group("base")) >= 3:
                base = dirs[:-1] + [m.group("base") + " \u0000"]  # synthetic merge key
        groups["\\".join(base)].append(f)
    out: dict[str, list[dict]] = {}
    for key, files in groups.items():
        if key.endswith(" \u0000"):
            # Suffix-style discs ("Dune CD1", "Dune CD2") share a parent; keep their real dirs in rel.
            parent = "\\".join(split_remote(key)[:-1])
            out[key] = [_file_entry(f, parent) for f in files]
        else:
            out[key] = [_file_entry(f, key) for f in files]
    return out


def _expected_seconds(book: dict) -> float | None:
    if book.get("runtime_ms"):
        return book["runtime_ms"] / 1000
    if book.get("runtime_min"):
        return float(book["runtime_min"]) * 60
    return None


def fmt_dur(seconds: float | None) -> str:
    if not seconds:
        return "?"
    h, m = divmod(int(round(seconds / 60)), 60)
    return f"{h}h{m:02d}m"


def score_candidate(cand: dict, book: dict, ctx: dict) -> None:
    reasons: list[str] = []
    files = cand["files"]
    dir_parts = split_remote(cand["directory"].replace(" \u0000", ""))
    tail = " ".join(dir_parts[-3:])
    # File names count too: a lone "The Tiny Test.m4b" inside "Everything/" is still a match.
    names = " ".join(f["name"].rsplit(".", 1)[0] for f in files[:3])
    near_tokens = set(tokens(tail + " " + names))
    full_tokens = set(tokens(" ".join(dir_parts) + " " + " ".join(f["rel"] for f in files[:5]) + " " + names))

    # --- title
    st = ctx["short_title"]
    cov = coverage(ctx["title_sig"], near_tokens)
    tsr = fuzz.token_set_ratio(norm(st), norm(tail + " " + names))
    title_score = 0.65 * cov * 100 + 0.35 * tsr
    if cov < 0.5:
        # the title may sit higher up the path (e.g. Author/Title/MP3/...)
        cov_full = coverage(ctx["title_sig"], full_tokens)
        title_score = max(title_score, 0.9 * (0.65 * cov_full * 100 + 0.35 * tsr))
    reasons.append(f"title {title_score:.0f}")

    # --- author
    author_score = 0.0
    if ctx["author_last"]:
        if ctx["author_last"] in full_tokens:
            author_score = 100.0
        elif coverage([ctx["author_last"]], full_tokens) > 0:
            author_score = 80.0
        reasons.append("author ✓" if author_score else "author ✗")
    else:
        author_score = 50.0

    # --- runtime
    expected = ctx["expected"]
    lengths = [f["length"] for f in files if f.get("length")]
    total = float(sum(lengths)) if lengths and len(lengths) == len(files) else None
    cand["length_s"] = total
    tol = ctx["tolerance"]
    if expected and total:
        diff = abs(total - expected) / expected
        if diff <= tol:
            runtime_score = 100.0
        else:
            runtime_score = max(0.0, 100.0 - (diff - tol) / (tol * 3) * 100.0)
        mark = "✓" if diff <= tol else ("~" if runtime_score > 40 else "✗")
        reasons.append(f"runtime {fmt_dur(total)} vs {fmt_dur(expected)} {mark}")
    else:
        runtime_score = 50.0
        reasons.append("runtime unknown")

    # --- format
    fmts = sorted({f["ext"] for f in files}, key=lambda e: ctx["formats"].index(e) if e in ctx["formats"] else 99)
    cand["formats"] = fmts
    idx = ctx["formats"].index(fmts[0]) if fmts and fmts[0] in ctx["formats"] else len(ctx["formats"])
    format_score = max(0.0, 100.0 - idx * 15)
    reasons.append("/".join(fmts))

    # --- availability
    avail = 40.0
    if cand["free_slot"]:
        avail += 40
    if cand["queue_length"] <= 0:
        avail += 10
    if cand["upload_speed"] >= 1_000_000:
        avail += 10
    elif cand["upload_speed"] < 50_000:
        avail -= 20
    reasons.append("free slot" if cand["free_slot"] else f"queue {cand['queue_length']}")

    score = 0.45 * title_score + 0.15 * author_score + 0.25 * runtime_score + 0.08 * format_score + 0.07 * avail

    # --- size: prefer lean files. Speech is fine at 64 kbps; very large copies lose a few points,
    # never enough to beat a better match. Very low bitrates sound poor.
    kbps = bitrate_kbps(cand)
    cand["kbps"] = kbps
    if kbps:
        if kbps > 256:
            score -= 6
            reasons.append(f"{kbps} kbps (large)")
        elif kbps > 160:
            score -= 3
            reasons.append(f"{kbps} kbps (large)")
        elif kbps < 32:
            score -= 8
            reasons.append(f"{kbps} kbps (low quality)")
        else:
            reasons.append(f"{kbps} kbps")

    # --- bonuses / penalties
    if ctx["narrator_lasts"] and any(n in full_tokens for n in ctx["narrator_lasts"]):
        score += 4
        reasons.append("narrator ✓")
    elif ctx["alt_narrators"] and any(n in full_tokens for n in ctx["alt_narrators"]):
        score -= 15
        reasons.append("other narrator ✗")
    path_norm = " " + " ".join(sorted(full_tokens)) + " "
    if ctx["unabridged"] and " abridged " in path_norm and " unabridged " not in path_norm:
        score -= 25
        reasons.append("abridged ✗")
    if not ctx["dramatised"] and any(w in full_tokens for w in ("dramatised", "dramatized", "dramatisation", "dramatization")):
        score -= 15
        reasons.append("dramatisation ✗")
    if {"sample", "preview", "excerpt"} & near_tokens:
        score -= 30
        reasons.append("sample ✗")
    seq = ctx["sequence"]
    if seq:
        found = {m.group(1).lstrip("0") or "0" for m in SEQ_IN_PATH_RE.finditer(" ".join(dir_parts[-2:]))}
        if found:
            if seq.lstrip("0") in found:
                score += 3
            elif len(found) == 1:
                score -= 20
                reasons.append(f"book #{found.pop()} ≠ #{seq}")
    if total and expected and total > expected * 1.8:
        reasons.append("looks like multiple books")

    cand["score"] = round(max(0.0, min(100.0, score)), 1)
    cand["rank"] = round(score, 2)  # unclamped, so bonuses still order 100-scorers
    cand["reasons"] = reasons


def bitrate_kbps(cand: dict) -> int | None:
    """Average bitrate from total size and length, else the files' reported bitrate."""
    length = cand.get("length_s")
    if length and cand.get("size"):
        return round(cand["size"] * 8 / length / 1000)
    rates = [f["bitrate"] for f in cand.get("files") or [] if f.get("bitrate")]
    return round(sum(rates) / len(rates)) if rates else None


def score_ebook(cand: dict, book: dict, ctx: dict) -> None:
    """Ebooks have no runtime, so title and author carry the score, then format preference."""
    reasons: list[str] = []
    f = cand["files"][0]
    dir_parts = split_remote(cand["directory"])
    stem = f["name"].rsplit(".", 1)[0]
    near_tokens = set(tokens(" ".join(dir_parts[-1:]) + " " + stem))
    full_tokens = set(tokens(" ".join(dir_parts) + " " + stem))

    cov = coverage(ctx["title_sig"], set(tokens(stem)))
    tsr = fuzz.token_set_ratio(norm(ctx["short_title"]), norm(stem))
    title_score = 0.65 * cov * 100 + 0.35 * tsr
    if cov < 0.5:
        cov_near = coverage(ctx["title_sig"], near_tokens)
        title_score = max(title_score, 0.85 * (0.65 * cov_near * 100 + 0.35 * tsr))
    reasons.append(f"title {title_score:.0f}")

    author_score = 50.0
    if ctx["author_last"]:
        author_score = 100.0 if ctx["author_last"] in full_tokens else \
            80.0 if coverage([ctx["author_last"]], full_tokens) > 0 else 0.0
        reasons.append("author ✓" if author_score else "author ✗")

    fmts = ctx["formats"]
    ext = f["ext"]
    cand["formats"] = [ext]
    cand["length_s"] = None
    cand["kbps"] = None
    idx = fmts.index(ext) if ext in fmts else len(fmts)
    format_score = max(0.0, 100.0 - idx * 25)
    reasons.append(ext)

    avail = 40.0 + (40 if cand["free_slot"] else 0) + (10 if cand["queue_length"] <= 0 else 0)
    avail += 10 if cand["upload_speed"] >= 1_000_000 else (-20 if cand["upload_speed"] < 50_000 else 0)
    reasons.append("free slot" if cand["free_slot"] else f"queue {cand['queue_length']}")

    score = 0.55 * title_score + 0.25 * author_score + 0.12 * format_score + 0.08 * avail

    if f["size"] and f["size"] < 20_000:
        score -= 20
        reasons.append("tiny file ✗")
    if {"sample", "preview", "excerpt"} & near_tokens:
        score -= 30
        reasons.append("sample ✗")
    path_norm = " " + " ".join(sorted(full_tokens)) + " "
    if " abridged " in path_norm and " unabridged " not in path_norm:
        score -= 15
        reasons.append("abridged ✗")
    seq = ctx["sequence"]
    if seq:
        found = {m.group(1).lstrip("0") or "0" for m in SEQ_IN_PATH_RE.finditer(dir_parts[-1] + " " + stem if dir_parts else stem)}
        if found:
            if seq.lstrip("0") in found:
                score += 3
            elif len(found) == 1:
                score -= 20
                reasons.append(f"book #{found.pop()} ≠ #{seq}")

    cand["score"] = round(max(0.0, min(100.0, score)), 1)
    cand["rank"] = round(score, 2)
    cand["reasons"] = reasons


def build_ebook_candidates(responses: list[dict], book: dict, ctx: dict, query: str = "") -> list[dict]:
    """One candidate per ebook file: a book is a single file, and one folder often holds a whole shelf."""
    out: list[dict] = []
    for resp in responses:
        user = resp.get("username")
        if not user:
            continue
        for f in resp.get("files") or []:
            parts = split_remote(f.get("filename", ""))
            if len(parts) < 2:
                continue
            ext = (f.get("extension") or parts[-1].rsplit(".", 1)[-1]).lower().lstrip(".")
            if ext not in ctx["formats"]:
                continue
            directory = "\\".join(parts[:-1])
            entry = _file_entry(f, directory)
            cid = hashlib.sha1(f"{user}|{entry['filename']}".encode()).hexdigest()[:12]
            cand: dict[str, Any] = {
                "id": cid, "username": user, "directory": directory,
                "display_dir": f"{directory}\\{entry['name']}",
                "files": [entry], "file_count": 1, "size": entry["size"],
                "free_slot": bool(resp.get("hasFreeUploadSlot")),
                "queue_length": int(resp.get("queueLength") or 0),
                "upload_speed": int(resp.get("uploadSpeed") or 0),
                "subset": False, "query": query, "media": "ebook",
            }
            score_ebook(cand, book, ctx)
            out.append(cand)
    out.sort(key=lambda c: (c["rank"], c["free_slot"], c["upload_speed"]), reverse=True)
    return out


def scoring_context(book: dict, formats: list[str], tolerance_pct: float, alt_narrators: list[str] | None = None) -> dict:
    st = short_title(book.get("title") or "")
    series = (book.get("series") or [None])[0]
    narr = [last_name(n) for n in narrator_names(book)]
    alt = [last_name(n) for n in alt_narrators or [] if n and norm(person_name(n)) not in NON_PERSON_NARRATORS]
    alt = [a for a in alt if a and a not in narr]
    title_l = (book.get("title") or "").lower()
    return {
        "short_title": st,
        "title_sig": significant(st),
        "author_last": last_name(primary_author(book)),
        "narrator_lasts": [n for n in narr if n],
        "alt_narrators": alt,
        "expected": _expected_seconds(book),
        "tolerance": max(tolerance_pct, 1) / 100.0,
        "formats": formats,
        "unabridged": (book.get("format") or "unabridged").lower() != "abridged",
        "dramatised": "dramati" in title_l or "dramati" in (book.get("format") or "").lower(),
        "sequence": (series or {}).get("sequence"),
    }


def build_candidates(responses: list[dict], book: dict, ctx: dict, query: str = "") -> list[dict]:
    out: list[dict] = []
    for resp in responses:
        user = resp.get("username")
        if not user:
            continue
        groups = group_response(resp, ctx["formats"])
        for key, files in groups.items():
            variants = [(files, False)]
            # A folder holding several books (series dump): offer the files whose
            # names match this title as their own candidate.
            dir_cov = coverage(ctx["title_sig"], set(tokens(" ".join(split_remote(key)[-2:]))))
            if len(files) > 1 and dir_cov < 0.5:
                hits = [f for f in files if coverage(ctx["title_sig"], set(tokens(f["name"]))) >= 0.8]
                if hits and len(hits) < len(files):
                    variants.append((hits, True))
            for vfiles, subset in variants:
                vfiles = sorted(vfiles, key=lambda f: natural_key(f["rel"] + "/" + f["name"]))
                cid = hashlib.sha1(f"{user}|{key}|{subset}|{len(vfiles)}".encode()).hexdigest()[:12]
                cand: dict[str, Any] = {
                    "id": cid,
                    "username": user,
                    "directory": key,
                    "display_dir": key.replace(" \u0000", " (discs)"),
                    "files": vfiles,
                    "file_count": len(vfiles),
                    "size": sum(f["size"] for f in vfiles),
                    "free_slot": bool(resp.get("hasFreeUploadSlot")),
                    "queue_length": int(resp.get("queueLength") or 0),
                    "upload_speed": int(resp.get("uploadSpeed") or 0),
                    "subset": subset,
                    "query": query,
                }
                score_candidate(cand, book, ctx)
                out.append(cand)
    out.sort(key=lambda c: (c["rank"], c["free_slot"], c["upload_speed"]), reverse=True)
    return out


def natural_key(s: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def suggest_query_from_folder(name: str) -> str:
    """Clean a local folder name into something searchable."""
    s = re.sub(r"[\[(\{][^\])\}]*[\])\}]", " ", name)
    s = re.sub(r"(?i)\b(unabridged|abridged|audiobook|audio ?book|m4b|mp3|flac|aac|\d{2,3}\s?kbps|\d{3,4}p|retail|read by.*|narrated by.*)\b", " ", s)
    s = re.sub(r"\b(19|20)\d{2}\b", " ", s)
    s = re.sub(r"[_.]+", " ", s)
    s = re.sub(r"\s+-\s+", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -")
