"""Hardcover metadata source (https://hardcover.app), optional and off by default.

Uses Hardcover's GraphQL API with a personal API key from https://hardcover.app/account/api.
Books are searched with the `search` query, then the matching books are loaded in one
query with their default audiobook edition, which carries the ASIN, runtime and narrators.

Free keys allow 60 requests a minute; each search here costs two (search + details).
"""
from __future__ import annotations

import logging
import os
import re

import httpx

from .base import empty_book, html_to_text, split_role

log = logging.getLogger(__name__)

API_URL = "https://api.hardcover.app/v1/graphql"
UA = "Narratarr (self-hosted audiobook manager; https://github.com/joshbosh/narratarr)"
ASIN_RE = re.compile(r"^(B0[0-9A-Z]{8}|\d{9}[\dX])$", re.I)

_EDITION_FIELDS = """
  id asin isbn_13 audio_seconds release_date reading_format_id
  image { url }
  publisher { name }
  language { language }
  contributions { contribution author { name } }
"""
_BOOK_FIELDS = f"""
  id title subtitle description release_date release_year slug cached_tags
  image {{ url }}
  contributions {{ contribution author {{ name }} }}
  book_series {{ position series {{ name }} }}
  default_audio_edition {{ {_EDITION_FIELDS} }}
"""


class HardcoverError(Exception):
    pass


def _token(raw: str) -> str:
    raw = (raw or "").strip()
    return raw[7:].strip() if raw.lower().startswith("bearer ") else raw


def _genres(cached_tags) -> list[str]:
    out: list[str] = []
    if isinstance(cached_tags, dict):
        for item in cached_tags.get("Genre") or []:
            tag = item.get("tag") if isinstance(item, dict) else item
            if tag and tag not in out:
                out.append(str(tag))
    return out[:6]


def _seq(position) -> str | None:
    if position is None:
        return None
    f = float(position)
    return str(int(f)) if f.is_integer() else str(f)


def normalise(b: dict, media: str = "audiobook") -> dict:
    """media="ebook" ignores the audio edition: the book's own cover, no ASIN/narrators/runtime."""
    ed = {} if media == "ebook" else (b.get("default_audio_edition") or {})
    book = empty_book()
    authors = []
    for c in b.get("contributions") or []:
        role = (c.get("contribution") or "").strip().lower() or None
        name, suffix_role = split_role(((c.get("author") or {}).get("name")) or "")
        if not name:
            continue
        role = None if role in (None, "author") else role
        if role in ("narrator",):
            continue
        authors.append({"name": name, "role": role or suffix_role, "asin": None})
    narrators = []
    for c in ed.get("contributions") or []:
        if (c.get("contribution") or "").strip().lower() == "narrator":
            name = split_role(((c.get("author") or {}).get("name")) or "")[0]
            if name and name not in narrators:
                narrators.append(name)
    series = []
    for s in b.get("book_series") or []:
        name = (s.get("series") or {}).get("name")
        if name:
            series.append({"name": name, "sequence": _seq(s.get("position")), "asin": None})
    release = ed.get("release_date") or b.get("release_date")
    secs = ed.get("audio_seconds")
    book.update({
        "provider": "hardcover",
        "hardcover_id": b.get("id"),
        "ref": f"hardcover:{b.get('id')}",
        "asin": (ed.get("asin") or "").upper() or None,
        "title": (b.get("title") or "").strip(),
        "subtitle": b.get("subtitle"),
        "authors": authors,
        "narrators": narrators,
        "series": series,
        "runtime_min": round(secs / 60) if secs else None,
        "release_date": release,
        "year": str(b.get("release_year") or (release or "")[:4] or "") or None,
        "publisher": (ed.get("publisher") or {}).get("name"),
        "description": html_to_text(b.get("description")),
        "language": (ed.get("language") or {}).get("language"),
        "genres": _genres(b.get("cached_tags")),
        "cover_url": (ed.get("image") or {}).get("url") or (b.get("image") or {}).get("url"),
        "isbn": ed.get("isbn_13"),
        "has_audio": bool(b.get("default_audio_edition")),
    })
    if media == "ebook":
        book["cover_url"] = (b.get("image") or {}).get("url")
    return book


class HardcoverProvider:
    name = "hardcover"

    def __init__(self, token: str, timeout: float = 15):
        self.token = _token(token)
        self.timeout = timeout
        self.url = os.environ.get("NARRATARR_HARDCOVER_URL_OVERRIDE") or API_URL  # tests/mocks

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        if not self.token:
            raise HardcoverError("No Hardcover API key set")
        headers = {"authorization": f"Bearer {self.token}", "content-type": "application/json", "user-agent": UA}
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(self.url, json={"query": query, "variables": variables or {}}, headers=headers)
        if r.status_code == 401:
            raise HardcoverError("Hardcover rejected the API key (401) — it may have expired")
        if r.status_code == 429:
            raise HardcoverError("Hardcover rate limit reached — try again shortly")
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise HardcoverError("; ".join(e.get("message", "error") for e in data["errors"])[:300])
        return data.get("data") or {}

    async def _books(self, ids: list[int], media: str = "audiobook") -> list[dict]:
        if not ids:
            return []
        q = f"query Books($ids: [Int!]) {{ books(where: {{id: {{_in: $ids}}}}) {{ {_BOOK_FIELDS} }} }}"
        rows = (await self._gql(q, {"ids": ids})).get("books") or []
        by_id = {b["id"]: b for b in rows}
        return [normalise(by_id[i], media) for i in ids if i in by_id]

    async def search(self, query: str, limit: int = 25, media: str = "audiobook") -> list[dict]:
        query = query.strip()
        if ASIN_RE.match(query):
            book = await self.get(query)
            return [book] if book else []
        q = ("query Search($q: String!, $n: Int!) { search(query: $q, query_type: \"Book\", per_page: $n, page: 1) "
             "{ ids } }")
        data = (await self._gql(q, {"q": query, "n": min(max(limit, 1), 25)})).get("search") or {}
        ids = [int(i) for i in (data.get("ids") or []) if str(i).isdigit()]
        books = await self._books(ids, media)
        return books if media == "ebook" else [b for b in books if b["has_audio"]]

    async def get(self, identifier: str, region: str | None = None, media: str = "audiobook") -> dict | None:
        ident = str(identifier).strip()
        if ident.lower().startswith("hardcover:"):
            num = ident.split(":", 1)[1]
            books = await self._books([int(num)], media) if num.isdigit() else []
            return books[0] if books else None
        q = (f"query ByAsin($asin: String!) {{ editions(where: {{asin: {{_eq: $asin}}, reading_format_id: {{_eq: 2}}}}, "
             f"limit: 1) {{ {_EDITION_FIELDS} book {{ {_BOOK_FIELDS} }} }} }}")
        rows = (await self._gql(q, {"asin": ident.upper()})).get("editions") or []
        if not rows:
            return None
        ed = {k: v for k, v in rows[0].items() if k != "book"}
        b = dict(rows[0].get("book") or {})
        b["default_audio_edition"] = ed          # use the edition that matched this ASIN
        return normalise(b, media)
