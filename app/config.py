"""Settings schema and store.

Every setting is declared once in SCHEMA. Values live in the SQLite `settings`
table; environment variables (NARRATARR_<KEY with dots as __>) supply the value
when the database has none, which makes first-run docker-compose config easy.
The Settings page is rendered from this schema, so adding a setting here is
all that's needed to expose it in the UI.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("NARRATARR_DATA_DIR", "/config"))
SECRET_MASK = "••••••••"


@dataclass
class Setting:
    key: str
    default: Any
    type: str  # str | int | float | bool | secret | select | text | hidden (stored, not shown)
    label: str
    group: str
    help: str = ""
    options: list = field(default_factory=list)
    labels: dict = field(default_factory=dict)  # option value → display text

    @property
    def env(self) -> str:
        return "NARRATARR_" + self.key.upper().replace(".", "__")


REENCODE_KBPS = {
    64: "64 kbps · single narrator",
    96: "96 kbps · light music and ambient effects",
    128: "128 kbps · full-cast, dramatised productions",
}

SCHEMA: list[Setting] = [
    # --- slskd -------------------------------------------------------------
    Setting("slskd.url", "http://slskd:5030", "str", "slskd URL", "slskd",
            "Base URL of slskd as reachable from this container."),
    Setting("slskd.api_key", "", "secret", "slskd API key", "slskd",
            "An API key from slskd's web.authentication.api_keys (role readwrite)."),
    Setting("slskd.downloads_path", "/slskd-downloads", "str", "slskd downloads folder", "slskd",
            "slskd's completed-downloads directory, as mounted inside THIS container. "
            "Narratarr moves finished files out of here into its own incomplete/ folder."),
    Setting("slskd.search_seconds", 25, "int", "Search window (seconds)", "slskd",
            "How long each slskd search is allowed to collect responses."),
    Setting("slskd.response_limit", 250, "int", "Max responses per search", "slskd"),
    Setting("slskd.max_peer_queue", 50, "int", "Max peer queue length", "slskd",
            "Ignore peers with a longer upload queue than this."),
    Setting("slskd.stall_minutes", 45, "int", "Stalled download timeout (minutes)", "slskd",
            "A source with no progress for this long is abandoned and the next candidate tried."),
    Setting("slskd.cleanup", True, "bool", "Clear finished transfers & searches from slskd", "slskd"),

    # --- metadata ----------------------------------------------------------
    Setting("metadata.provider", "audible", "select", "Lookup provider", "Metadata",
            options=["audible"]),
    Setting("metadata.region", "us", "select", "Primary Audible region", "Metadata",
            options=["au", "us", "uk", "ca", "de", "fr", "it", "es", "jp", "in"]),
    Setting("metadata.fallback_region", "", "select", "Fallback Audible region", "Metadata",
            "Used when the primary region has no results or doesn't carry an ASIN.",
            options=["", "au", "us", "uk", "ca", "de", "fr", "it", "es", "jp", "in"]),
    Setting("metadata.use_audnexus", True, "bool", "Enrich with Audnexus (genres, chapters)", "Metadata"),
    Setting("metadata.hardcover_enabled", False, "bool", "Use Hardcover as a second source", "Metadata",
            "Audible's values always win; Hardcover fills any gaps, adds books Audible doesn't have, "
            "and is used on its own if Audible doesn't respond."),
    Setting("metadata.hardcover_token", "", "secret", "Hardcover API key", "Metadata",
            "Free — create one at hardcover.app/account/api. Paste it with or without the 'Bearer' prefix."),
    Setting("metadata.audnexus_url", "https://api.audnex.us", "str", "Audnexus URL", "Metadata"),

    # --- matching ----------------------------------------------------------
    Setting("matching.min_score", 72, "int", "Auto-pick threshold (0-100)", "Matching",
            "Best candidate must score at least this to download automatically; "
            "otherwise the job waits in the queue for you to pick."),
    Setting("matching.max_queries", 6, "int", "Max search variations per book", "Matching",
            "Query variations are tried in order until one yields a confident match."),
    Setting("matching.formats", "m4b,mp3,m4a,flac,opus,ogg", "str", "Formats (preferred first)", "Matching",
            "Comma-separated; anything not listed is ignored."),
    Setting("matching.runtime_tolerance", 8, "int", "Runtime tolerance (%)", "Matching",
            "How far a candidate's total length may drift from Audible's runtime before it's penalised."),
    Setting("matching.auto_download", True, "bool", "Download best match automatically", "Matching"),
    Setting("matching.ebook_formats", "epub,azw3,mobi,pdf", "str", "Ebook formats (preferred first)", "Matching",
            "Comma-separated; used for ebook requests. Anything not listed is ignored."),

    # --- files -------------------------------------------------------------
    Setting("paths.downloads", "/downloads", "str", "Downloads root", "Files",
            "Contains incomplete/ (in-flight & unprocessed) and completed/ (tagged, organised)."),
    Setting("paths.template", "{author}/{series}/{seqtitle}", "str", "Folder template", "Files",
            "Under completed/. Tokens: {author} {title} {series} {seq} {seqtitle} {narrator} {year} {asin}. "
            "Empty segments are dropped. Filenames are never changed."),
    Setting("paths.import_roots", "", "str", "Extra Manual Import folders", "Files",
            "Comma-separated paths (inside this container) to offer on the Manual Import page."),

    # --- processing --------------------------------------------------------
    Setting("processing.write_tags", True, "bool", "Write audio tags", "Processing"),
    Setting("processing.embed_cover", True, "bool", "Embed cover art in files", "Processing"),
    Setting("processing.save_cover", True, "bool", "Save cover.jpg", "Processing"),
    Setting("processing.embed_chapters", True, "bool", "Embed chapters in single-file books", "Processing",
            "Only when the file has none and its length matches Audible's runtime."),
    Setting("processing.reencode", False, "bool", "Re-encode to save space", "Processing",
            "Re-encodes finished books at the bitrate below (never above the source's own). Only when it makes the "
            "book smaller or joins several files. Replaces the original files."),
    Setting("processing.reencode_merge", True, "bool", "Join into one M4B when re-encoding", "Processing",
            "On: one M4B with chapters (file names aren't kept). Off: each file is re-encoded on its own and keeps "
            "its name and folder (MP3 stays MP3; other formats become AAC .m4a)."),
    Setting("processing.reencode_kbps", 64, "int", "Re-encode bitrate", "Processing",
            "Also the default for the Re-encode button. Never higher than the source's own bitrate.",
            options=list(REENCODE_KBPS), labels={str(k): v for k, v in REENCODE_KBPS.items()}),
    Setting("processing.sidecars", True, "bool", "Write metadata.opf / metadata.json / desc.txt / reader.txt",
            "Processing", "Audiobookshelf reads all of these."),

    # --- audiobookshelf ----------------------------------------------------
    Setting("abs.enabled", False, "bool", "Send finished books to Audiobookshelf", "Audiobookshelf"),
    Setting("abs.url", "http://audiobookshelf:80", "str", "Audiobookshelf URL", "Audiobookshelf",
            "As reachable from this container."),
    Setting("abs.public_url", "", "str", "Audiobookshelf link URL", "Audiobookshelf",
            "Used for 'Open in Audiobookshelf' links in your browser. Blank = same as above."),
    Setting("abs.token", "", "secret", "API key", "Audiobookshelf",
            "Settings → API Keys in Audiobookshelf (an admin user's key: scans and metadata edits need admin)."),
    Setting("abs.library_id", "", "abs_library", "Library", "Audiobookshelf",
            "Save & test to load your libraries."),
    Setting("abs.folder_id", "", "abs_folder", "Library folder", "Audiobookshelf",
            "Finished books are filed into this folder using the folder template above."),
    Setting("abs.local_path", "", "str", "That folder, as mounted in Narratarr", "Audiobookshelf",
            "Blank = mounted at the same path Audiobookshelf uses (recommended). "
            "Otherwise the path inside this container that points at the same host folder."),
    Setting("abs.ebook_library_id", "", "abs_library", "Ebook library", "Audiobookshelf",
            "A separate book library for ebooks. Blank = ebooks stay in Narratarr's completed-ebooks/ folder."),
    Setting("abs.ebook_folder_id", "", "abs_folder", "Ebook library folder", "Audiobookshelf",
            "Finished ebooks are filed into this folder using the folder template above."),
    Setting("abs.ebook_local_path", "", "str", "That ebook folder, as mounted in Narratarr", "Audiobookshelf",
            "Blank = mounted at the same path Audiobookshelf uses."),
    Setting("abs.metadata_mode", "fill", "select", "Metadata", "Audiobookshelf",
            "fill: keep Audiobookshelf's values and fill only what's missing (differences are logged). "
            "overwrite: Narratarr's Audible record replaces Audiobookshelf's.",
            options=["fill", "overwrite"]),
    Setting("abs.delete_after_import", True, "bool", "Delete from Narratarr after import", "Audiobookshelf",
            "On: the book is moved into Audiobookshelf and removed from Narratarr's library list. "
            "Off: it's copied, and Narratarr keeps its own copy in completed/."),
    Setting("abs.sync_minutes", 15, "int", "Library sync interval (minutes)", "Audiobookshelf",
            "How often Narratarr refreshes its view of the Audiobookshelf library and retries pending imports."),

    # --- general / auth groundwork ----------------------------------------
    Setting("general.poll_seconds", 5, "int", "Download poll interval (seconds)", "General"),
    Setting("general.check_updates", True, "bool", "Check for updates", "General",
            "Asks GitHub for the latest Narratarr release a few times a day and shows “Update available” in the "
            "sidebar. Nothing else is sent. Release builds only."),
    Setting("general.welcome_done", False, "hidden", "Welcome guide finished", "General"),
    Setting("auth.mode", "none", "select", "Authentication", "General",
            "Placeholder — only 'none' is implemented. See app/auth.py.", options=["none"]),
]

BY_KEY = {s.key: s for s in SCHEMA}


def _coerce(s: Setting, value: Any) -> Any:
    if value is None:
        return s.default
    t = s.type
    try:
        if t == "bool" or (t == "hidden" and isinstance(s.default, bool)):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if t == "int":
            return int(float(value))
        if t == "float":
            return float(value)
        return "" if value is None else str(value)
    except (TypeError, ValueError):
        return s.default


class SettingsStore:
    """Read-through cache over the settings table."""

    def __init__(self, db):
        self.db = db
        self._cache: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._cache is None:
            rows = self.db.query("SELECT key, value FROM settings")
            stored = {r["key"]: json.loads(r["value"]) for r in rows}
            values: dict[str, Any] = {}
            for s in SCHEMA:
                if s.key in stored:
                    values[s.key] = _coerce(s, stored[s.key])
                elif s.env in os.environ:
                    values[s.key] = _coerce(s, os.environ[s.env])
                else:
                    values[s.key] = s.default
            self._cache = values
        return self._cache

    def get(self, key: str) -> Any:
        return self._load()[key]

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def all(self) -> dict[str, Any]:
        return dict(self._load())

    def update(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            s = BY_KEY.get(key)
            if not s:
                continue
            if s.type == "secret" and value == SECRET_MASK:
                continue  # unchanged
            self.db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(_coerce(s, value))),
            )
        self._cache = None

    def public(self) -> list[dict]:
        """Schema + values for the UI, with secrets masked."""
        values = self._load()
        out = []
        for s in SCHEMA:
            v = values[s.key]
            if s.type == "secret":
                v = SECRET_MASK if v else ""
            out.append({
                "key": s.key, "type": s.type, "label": s.label, "group": s.group,
                "help": s.help, "options": s.options, "labels": s.labels, "value": v,
            })
        return out

    # Convenience paths -----------------------------------------------------
    @property
    def downloads(self) -> Path:
        return Path(self.get("paths.downloads"))

    @property
    def incomplete(self) -> Path:
        return self.downloads / "incomplete"

    @property
    def completed(self) -> Path:
        return self.downloads / "completed"

    @property
    def completed_ebooks(self) -> Path:
        return self.downloads / "completed-ebooks"

    def completed_for(self, media: str | None) -> Path:
        return self.completed_ebooks if media == "ebook" else self.completed

    def formats(self, media: str | None = None) -> list[str]:
        key = "matching.ebook_formats" if media == "ebook" else "matching.formats"
        return [f.strip().lower().lstrip(".") for f in str(self.get(key)).split(",") if f.strip()]
