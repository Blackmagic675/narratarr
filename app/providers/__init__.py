"""Metadata sources.

Audible is the primary source. Hardcover is optional (off until enabled with an API key):
when both are on, results are compared, Audible's values win, and Hardcover fills any gaps.
If Audible doesn't answer, Hardcover is used on its own.
"""
from __future__ import annotations

import asyncio
import logging
import re

from ..matching import last_name, narrator_names, norm, primary_author, short_title
from .audible import AudibleProvider
from .audnexus import enrich
from .base import MetadataProvider
from .hardcover import HardcoverProvider

log = logging.getLogger(__name__)

# Fields Hardcover may fill when Audible has nothing for them.
FILLABLE = ("subtitle", "narrators", "series", "runtime_min", "release_date", "year", "publisher",
            "description", "language", "genres", "cover_url", "isbn")


def _empty(v) -> bool:
    return v in (None, "", [], {})


def same_book(a: dict, b: dict) -> bool:
    if a.get("asin") and b.get("asin"):
        return a["asin"].upper() == b["asin"].upper()
    if a.get("isbn") and b.get("isbn") and a["isbn"] == b["isbn"]:
        return True
    if norm(short_title(a.get("title") or "")) != norm(short_title(b.get("title") or "")):
        return False
    if last_name(primary_author(a)) != last_name(primary_author(b)):
        return False
    na = {last_name(n) for n in narrator_names(a)}
    nb = {last_name(n) for n in narrator_names(b)}
    return not na or not nb or bool(na & nb)   # different narrators = different edition


def fill_gaps(primary: dict, extra: dict) -> dict:
    filled = []
    for f in FILLABLE:
        if _empty(primary.get(f)) and not _empty(extra.get(f)):
            primary[f] = extra[f]
            filled.append(f)
    if _empty(primary.get("authors")) and extra.get("authors"):
        primary["authors"] = extra["authors"]
        filled.append("authors")
    if not primary.get("asin") and extra.get("asin"):
        primary["asin"] = extra["asin"]
    primary["hardcover_id"] = primary.get("hardcover_id") or extra.get("hardcover_id")
    primary["sources"] = sorted(set(primary.get("sources") or [primary.get("provider")]) | {extra.get("provider")})
    if filled:
        primary["filled_from"] = {**(primary.get("filled_from") or {}), **{f: extra.get("provider") for f in filled}}
    return primary


def _tag(books: list[dict]) -> list[dict]:
    for b in books:
        b.setdefault("sources", [b.get("provider")])
        b["ref"] = b.get("ref") if b.get("provider") == "hardcover" and not b.get("asin") else (b.get("asin") or b.get("ref"))
    return books


# Editions that only exist as audio: never offer them as an ebook.
AUDIO_ONLY_RE = re.compile(r"full[\s-]*cast|dramati[sz]|radio (?:play|drama|production|adaptation)|\bbbc radio\b|"
                           r"audio ?(?:drama|edition|collection|play)|\baudiobook\b|\bnarrated by\b|"
                           r"\bunabridged\b|\bread by\b", re.I)
# Fields that describe an audio edition and must not leak into an ebook record.
AUDIO_FIELDS = ("narrators", "runtime_min", "runtime_ms", "chapters", "asin", "format")
# What Audible may still fill in for an ebook when the ebook source has nothing.
EBOOK_FILLABLE = ("subtitle", "release_date", "year", "language", "genres", "description", "cover_url")


def is_audio_only(b: dict) -> bool:
    hay = " ".join([b.get("title") or "", b.get("subtitle") or "",
                    *((s or {}).get("name") or "" for s in b.get("series") or [])])
    return bool(AUDIO_ONLY_RE.search(hay))


def ebook_key(b: dict) -> str:
    return f"{norm(short_title(b.get('title') or ''))}|{last_name(primary_author(b))}"


def ebook_record(b: dict) -> dict:
    """An ebook view of a book: no narrator, runtime or audiobook ASIN (the ref still finds it again)."""
    out = {**b, "narrators": [], "runtime_min": None, "runtime_ms": None, "chapters": None, "asin": None,
           "media": "ebook"}
    out["ref"] = b.get("ref") or b.get("asin")
    out.pop("format", None)
    if out.get("provider") == "audible":
        out["series"] = [s for s in out.get("series") or [] if not AUDIO_ONLY_RE.search(s.get("name") or "")]
    return out


def ebook_fill(primary: dict, extra: dict) -> dict:
    for f in EBOOK_FILLABLE:
        if _empty(primary.get(f)) and not _empty(extra.get(f)):
            primary[f] = extra[f]
    if _empty(primary.get("series")) and extra.get("series"):
        primary["series"] = extra["series"]
    primary["sources"] = sorted(set(primary.get("sources") or []) | set(extra.get("sources") or []))
    return primary


def ebook_results(results: list[dict]) -> list[dict]:
    """One result per title + author, audio-only editions dropped, in the order given (best source first)."""
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for b in sorted(results, key=lambda b: (b.get("format") or "").lower() == "abridged"):
        if is_audio_only(b):
            continue
        b = ebook_record(b)
        key = ebook_key(b)
        if key in seen:
            ebook_fill(seen[key], b)
            continue
        seen[key] = b
        out.append(b)
    return out


class MetadataHub:
    name = "hub"

    def __init__(self, settings):
        self.settings = settings
        self.audible = AudibleProvider(settings.get("metadata.region"), settings.get("metadata.fallback_region") or None)
        self.hardcover = (HardcoverProvider(settings.get("metadata.hardcover_token"))
                          if settings.get("metadata.hardcover_enabled") and settings.get("metadata.hardcover_token")
                          else None)

    @property
    def source_count(self) -> int:
        return 1 + (1 if self.hardcover else 0)

    async def search_ex(self, query: str, limit: int = 25, media: str = "audiobook") -> tuple[list[dict], dict]:
        """Returns (results, info). info = {sources: n answered, fallback: primary failed}.

        media="ebook" keeps Hardcover books with no audio edition and merges the different
        narrations of one title into a single result.
        """
        if media == "ebook":
            return await self._ebook_search(query, limit)
        return await self._search_ex(query, limit, media)

    async def _ebook_search(self, query: str, limit: int) -> tuple[list[dict], dict]:
        """Hardcover (the book itself) leads; Audible only adds books Hardcover lacks and fills empty fields."""
        tasks = [self.audible.search(query, limit)]
        if self.hardcover:
            tasks.append(self.hardcover.search(query, limit, "ebook"))
        res = await asyncio.gather(*tasks, return_exceptions=True)
        audible, hc = res[0], (res[1] if len(res) > 1 else None)
        if isinstance(hc, BaseException):
            log.warning("Hardcover search failed: %s", hc)
            hc = None
        if isinstance(audible, BaseException):
            if hc is None:
                raise audible
            log.warning("Audible search failed, using Hardcover only: %s", audible)
            audible = []
        books = ebook_results(_tag(hc)) if hc else []
        by_key = {ebook_key(b): b for b in books}
        for a in ebook_results(_tag(audible)):
            if ebook_key(a) in by_key:
                ebook_fill(by_key[ebook_key(a)], a)
            else:
                books.append(a)
        answered = (1 if not isinstance(res[0], BaseException) else 0) + (1 if hc is not None else 0)
        return books, {"sources": max(answered, 1), "fallback": isinstance(res[0], BaseException)}

    async def _search_ex(self, query: str, limit: int, media: str) -> tuple[list[dict], dict]:
        tasks = [self.audible.search(query, limit)]
        if self.hardcover:
            tasks.append(self.hardcover.search(query, limit, media))
        res = await asyncio.gather(*tasks, return_exceptions=True)
        primary, extra = res[0], (res[1] if len(res) > 1 else None)
        if isinstance(extra, BaseException):
            log.warning("Hardcover search failed: %s", extra)
            extra = None
        if isinstance(primary, BaseException):
            if extra is None:
                raise primary
            log.warning("Primary metadata source failed, using Hardcover only: %s", primary)
            return _tag(extra), {"sources": 1, "fallback": True}
        primary = _tag(primary)
        if extra is None:
            return primary, {"sources": 1, "fallback": False}
        extra = _tag(extra)
        used: set[int] = set()
        for a in primary:
            for i, h in enumerate(extra):
                if i not in used and same_book(a, h):
                    fill_gaps(a, h)
                    used.add(i)
                    break
        asins = {a["asin"].upper() for a in primary if a.get("asin")}
        tail = [h for i, h in enumerate(extra) if i not in used and not (h.get("asin") and h["asin"].upper() in asins)]
        return primary + tail, {"sources": 2, "fallback": False}

    async def search(self, query: str, limit: int = 25, media: str = "audiobook") -> list[dict]:
        return (await self.search_ex(query, limit, media))[0]

    async def get(self, identifier: str, region: str | None = None, media: str = "audiobook") -> dict | None:
        if media == "ebook":
            return await self._get_ebook(identifier, region)
        ident = str(identifier or "").strip()
        if ident.lower().startswith("hardcover:"):
            if not self.hardcover:
                return None
            b = await self.hardcover.get(ident)
            return _tag([b])[0] if b else None
        a = None
        try:
            a = await self.audible.get(ident, region) if region else await self.audible.get(ident)
            if a is None and region:
                a = await self.audible.get(ident)
        except Exception as e:  # noqa: BLE001
            log.warning("Primary metadata lookup for %s failed: %s", ident, e)
        h = None
        if self.hardcover:
            try:
                h = await self.hardcover.get(ident)
            except Exception as e:  # noqa: BLE001
                log.warning("Hardcover lookup for %s failed: %s", ident, e)
        if a and h:
            return fill_gaps(_tag([a])[0], h)
        found = a or h
        return _tag([found])[0] if found else None


    async def _get_ebook(self, identifier: str, region: str | None) -> dict | None:
        ident = str(identifier or "").strip()
        h = a = None
        if self.hardcover:
            try:
                h = await self.hardcover.get(ident, None, "ebook")
            except Exception as e:  # noqa: BLE001
                log.warning("Hardcover lookup for %s failed: %s", ident, e)
        if not ident.lower().startswith("hardcover:"):
            try:
                a = await self.audible.get(ident, region) if region else await self.audible.get(ident)
                if a is None and region:
                    a = await self.audible.get(ident)
            except Exception as e:  # noqa: BLE001
                log.warning("Audible lookup for %s failed: %s", ident, e)
        if h:
            book = ebook_record(_tag([h])[0])
            return ebook_fill(book, ebook_record(_tag([a])[0])) if a else book
        return ebook_record(_tag([a])[0]) if a else None


def get_provider(settings) -> MetadataHub:
    name = settings.get("metadata.provider")
    if name != "audible":
        raise ValueError(f"Unknown metadata provider: {name}")
    return MetadataHub(settings)


async def full_metadata(settings, ident: str, region: str | None = None, media: str = "audiobook") -> dict | None:
    """Fetch a book by ASIN or source ref (primary → fallback region → Hardcover) and enrich it."""
    book = await get_provider(settings).get(ident, region, media)
    if media == "ebook":
        return book
    if book and book.get("asin") and settings.get("metadata.use_audnexus"):
        regions = [book.get("region"), settings.get("metadata.region"), settings.get("metadata.fallback_region")]
        seen: list[str] = []
        for r in regions:
            if r and r not in seen:
                seen.append(r)
        book = await enrich(book, settings.get("metadata.audnexus_url"), seen)
    return book
