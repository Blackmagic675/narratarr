"""Provider interface and the normalised book record.

Every provider returns the same `book` dict so the rest of the app (matching,
tagging, sidecars, the UI) never cares where metadata came from. To add a new
lookup source, subclass MetadataProvider and register it in providers/__init__.py.
"""
from __future__ import annotations

import html
import re
from typing import Any, Protocol


def empty_book() -> dict[str, Any]:
    return {
        "asin": None,
        "ref": None,           # stable id for the queue: the ASIN, or "<source>:<id>" when there's no ASIN
        "region": None,
        "provider": None,
        "title": "",
        "subtitle": None,
        "authors": [],        # [{"name": str, "asin": str|None}]
        "narrators": [],      # [str]
        "series": [],         # [{"name": str, "sequence": str|None, "asin": str|None}]
        "runtime_min": None,
        "release_date": None,  # YYYY-MM-DD
        "year": None,
        "publisher": None,
        "description": None,  # plain text
        "language": None,
        "format": None,       # unabridged | abridged | ...
        "genres": [],
        "cover_url": None,
        "explicit": False,
        "isbn": None,
        "chapters": None,     # [{"title": str, "start_ms": int, "length_ms": int}] | None
        "runtime_ms": None,   # precise runtime from chapter data when known
    }


class MetadataProvider(Protocol):
    name: str

    async def search(self, query: str, limit: int = 25) -> list[dict]: ...

    async def get(self, identifier: str) -> dict | None: ...


_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    s = re.sub(r"(?i)<br\s*/?>", "\n", value)
    s = re.sub(r"(?i)</p\s*>", "\n\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "• ", s)
    s = re.sub(r"(?i)</li\s*>", "\n", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip() or None


# Audible appends roles to contributor names: "Jane Doe - editor", "Jane Doe - translator",
# occasionally "Jane Doe (Editor)". Roles start lowercase after the dash, which keeps hyphenated
# names like "Jean-Paul Sartre" or "Smith - Jones" intact.
_ROLE_DASH = re.compile(r"^(?P<name>.+?)\s+[-\u2013\u2014]\s+(?P<role>[a-z][a-z /&,.'-]*)$")
_ROLE_PAREN = re.compile(r"^(?P<name>.+?)\s*\((?P<role>editor|editors|translator|foreword|introduction|afterword|"
                         r"contributor|adapter|adaptation|illustrator|compiler|narrator)\)$", re.I)


def split_role(name: str | None) -> tuple[str, str | None]:
    """'Jane Doe - editor' → ('Jane Doe', 'editor')."""
    n = (name or "").strip()
    m = _ROLE_DASH.match(n) or _ROLE_PAREN.match(n)
    if m:
        return m.group("name").strip(), m.group("role").strip().lower()
    return n, None


# OPF MARC relator codes for contributors who aren't the author.
OPF_ROLES = {"illustrator": "ill", "cover artist": "cov", "editor": "edt", "translator": "trl", "foreword": "aui",
             "introduction": "aui", "afterword": "aft", "contributor": "ctb"}


def credited_authors(book: dict) -> list[str]:
    """The people a book is 'by': contributors with no role. Illustrators, translators, foreword writers
    and the like are left out. A book credited only to editors (an anthology) is 'by' its editors."""
    authors = [a for a in book.get("authors") or [] if a.get("name")]
    plain = [a["name"] for a in authors if not a.get("role") or a["role"] == "author"]
    if plain:
        return plain
    editors = [a["name"] for a in authors if (a.get("role") or "").startswith("editor")]
    return editors or [a["name"] for a in authors[:1]]


def clean_contributors(book: dict) -> dict:
    """Normalise author/narrator names in a stored book (also fixes jobs saved before role parsing)."""
    authors = []
    for a in book.get("authors") or []:
        name, role = split_role(a.get("name"))
        if name:
            authors.append({**a, "name": name, "role": a.get("role") or role})
    book["authors"] = authors
    book["narrators"] = [n for n in (split_role(x)[0] for x in book.get("narrators") or []) if n]
    return book


def primary_series(book: dict) -> dict | None:
    return book["series"][0] if book.get("series") else None


def author_names(book: dict) -> list[str]:
    return [a["name"] for a in book.get("authors", []) if a.get("name")]
