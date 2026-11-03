"""Job pipeline: queued → searching → (needs_review) → downloading → processing → completed.

One background loop runs searches one at a time (slskd and the Soulseek
network both prefer that), another polls slskd for download progress and
hands finished downloads to post-processing. State lives entirely in SQLite,
so a restart picks up where it left off.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

from rapidfuzz import fuzz

from . import matching
from .integrations.abs import AbsSync
from .postprocess import process_book, process_ebook, safe_segment
from .providers import full_metadata, get_provider
from .providers.base import clean_contributors
from .slskd import SlskdBusy, SlskdClient, SlskdError, transfer_failed, transfer_succeeded

log = logging.getLogger(__name__)

ACTIVE = ("queued", "searching", "needs_review", "downloading", "processing")
MAX_FILE_RETRIES = 2
MAX_CANDIDATES_STORED = 30
BUSY_MAX_ATTEMPTS = 8          # ~25 min of backoff before handing back to the user
BUSY_WAIT = int(os.environ.get("NARRATARR_BUSY_WAIT", "60"))  # seconds, × attempt number (tests shorten it)


def _book_summary(book: dict) -> dict:
    return {
        "asin": book.get("asin") or book.get("ref"), "region": book.get("region"), "title": book.get("title") or "Unknown",
        "subtitle": book.get("subtitle"), "authors": book.get("authors") or [],
        "narrators": book.get("narrators") or [], "series": book.get("series") or [],
        "runtime_min": book.get("runtime_min"), "cover_url": book.get("cover_url"),
    }


class JobManager:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._busy: set[int] = set()
        self.abs = AbsSync(db, settings)
        self._abs_inflight: set[int] = set()
        self._search_tasks: dict[int, asyncio.Task] = {}   # job id → running search (loop or custom)
        self._enqueuing: set[int] = set()                   # job ids with an slskd enqueue in flight

    # ------------------------------------------------------------------ infra
    def slskd(self) -> SlskdClient:
        return SlskdClient(self.settings.get("slskd.url"), self.settings.get("slskd.api_key"))

    def get(self, job_id: int) -> dict | None:
        return self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def update(self, job_id: int, **values) -> None:
        self.db.update("jobs", job_id, values)

    def log(self, job_id: int, msg: str) -> None:
        log.info("job %s: %s", job_id, msg)
        job = self.get(job_id)
        if not job:
            return
        entries = (job.get("log") or [])[-199:]
        entries.append({"t": time.time(), "msg": msg})
        self.update(job_id, log=entries)

    async def start(self) -> None:
        # Recover from restarts: interrupted searches re-run; processing resumes.
        self.db.execute("UPDATE jobs SET status='queued', detail='Restarted' WHERE status='searching'")
        self._tasks = [asyncio.create_task(self._search_loop(), name="search-loop"),
                       asyncio.create_task(self._monitor_loop(), name="monitor-loop"),
                       asyncio.create_task(self._abs_loop(), name="abs-loop")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def staging_dir(self, job: dict) -> Path:
        return self.settings.incomplete / f"{job['id']} - {safe_segment(job['title'], 80)}"

    # ---------------------------------------------------------------- creation
    def find_active(self, asin: str | None, media: str = "audiobook") -> dict | None:
        if not asin:
            return None
        marks = ",".join("?" * len(ACTIVE))
        return self.db.one(f"SELECT * FROM jobs WHERE asin=? AND media=? AND status IN ({marks})",
                           (asin, media, *ACTIVE))

    def create_download(self, book: dict, user_id: str | None = None, media: str = "audiobook") -> int:
        media = "ebook" if media == "ebook" else "audiobook"
        book = clean_contributors(dict(book))
        if media == "ebook":
            # narrators and runtime describe the audio edition; they'd only mislead ebook matching
            book.update({"narrators": [], "runtime_min": None, "runtime_ms": None, "chapters": None,
                         "_alt_narrators": []})
        s = _book_summary(book)
        now = time.time()
        jid = self.db.insert("jobs", {
            "kind": "download", **s, "book": book, "media": media, "status": "queued", "detail": "Waiting to search",
            "requested_by": user_id, "created_at": now, "updated_at": now,
        })
        self.log(jid, f"Requested {'ebook' if media == 'ebook' else 'audiobook'} {s['title']} "
                      f"({s['asin']}, {s['region']})")
        self.wake.set()
        return jid

    def create_import(self, book: dict, path: Path, mode: str, user_id: str | None = None) -> int:
        book = clean_contributors(dict(book))
        s = _book_summary(book)
        now = time.time()
        jid = self.db.insert("jobs", {
            "kind": "import", **s, "book": book, "status": "processing", "detail": "Waiting to import",
            "staging_path": str(path), "import_mode": mode, "requested_by": user_id,
            "created_at": now, "updated_at": now,
        })
        self.log(jid, f"Manual import of {path} ({mode})")
        self.wake.set()
        return jid

    # ------------------------------------------------------------------ search
    async def _search_loop(self) -> None:
        while True:
            try:
                job = self.db.one("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1")
                if not job:
                    self.wake.clear()
                    try:
                        await asyncio.wait_for(self.wake.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
                    continue
                task = asyncio.create_task(self.run_search(job["id"]), name=f"search-{job['id']}")
                self._search_tasks[job["id"]] = task
                try:
                    # asyncio.wait doesn't raise if the *task* is cancelled (job removed), only if
                    # this loop itself is being shut down.
                    await asyncio.wait({task})
                except asyncio.CancelledError:
                    task.cancel()
                    raise
                finally:
                    if self._search_tasks.get(job["id"]) is task:
                        self._search_tasks.pop(job["id"], None)
                if task.cancelled():
                    log.info("search for job %s stopped (job cancelled or removed)", job["id"])
                elif task.exception():
                    log.error("search for job %s failed", job["id"], exc_info=task.exception())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("search loop error")
                await asyncio.sleep(5)

    async def _prepare_book(self, job: dict) -> tuple[dict, list[str]]:
        book = clean_contributors(job["book"])
        ident = book.get("asin") or book.get("ref")
        if ident and not book.get("_enriched"):
            try:
                full = await full_metadata(self.settings, ident, book.get("region"))
                if full and job.get("media") == "ebook":
                    full = {k: v for k, v in full.items()
                            if k not in ("narrators", "runtime_min", "runtime_ms", "chapters")}
                if full:
                    book = {**book, **{k: v for k, v in full.items() if v not in (None, [], "")}}
            except Exception as e:  # noqa: BLE001
                self.log(job["id"], f"Metadata enrichment failed: {e}")
            book["_enriched"] = True
            book = clean_contributors(book)
            self.update(job["id"], book=book, **_book_summary(book))
            if book.get("chapters"):
                self.log(job["id"], f"Chapter data: {len(book['chapters'])} chapters")
            if book.get("filled_from"):
                self.log(job["id"], "Filled from a second source: " + ", ".join(sorted(book["filled_from"])))
        # Other narrations of the same title help reject the wrong edition.
        alt: list[str] = book.get("_alt_narrators") or []
        if "_alt_narrators" not in book and book.get("authors"):
            try:
                st = matching.short_title(book["title"])
                results = await get_provider(self.settings).search(f"{st} {matching.primary_author(book)}", 20)
                for r in results:
                    if r.get("asin") != book.get("asin") and \
                            fuzz.ratio(matching.norm(matching.short_title(r["title"])), matching.norm(st)) >= 90:
                        alt.extend(n for n in r.get("narrators") or [] if n not in book.get("narrators", []))
            except Exception as e:  # noqa: BLE001
                log.debug("alt narrator lookup failed: %s", e)
            book["_alt_narrators"] = sorted(set(alt))
            self.update(job["id"], book=book)
            if alt:
                self.log(job["id"], f"Other narrations exist: {', '.join(sorted(set(alt)))}")
        return book, alt

    def _ctx(self, book: dict, alt: list[str], media: str = "audiobook") -> dict:
        ctx = matching.scoring_context(book, self.settings.formats(media),
                                       self.settings.get("matching.runtime_tolerance"), alt)
        ctx["media"] = media
        return ctx

    def _merge(self, existing: list[dict], new: list[dict]) -> list[dict]:
        by_id = {c["id"]: c for c in existing}
        for c in new:
            if c["id"] not in by_id or c["score"] > by_id[c["id"]]["score"]:
                by_id[c["id"]] = c
        merged = sorted(by_id.values(), key=lambda c: (c.get("rank", c["score"]), c["free_slot"], c["upload_speed"]), reverse=True)
        return merged[:MAX_CANDIDATES_STORED]

    async def _one_search(self, job_id: int, query: str, book: dict, ctx: dict) -> list[dict]:
        s = self.settings
        responses = await self.slskd().search(
            query, s.get("slskd.search_seconds"), s.get("slskd.response_limit"),
            s.get("slskd.max_peer_queue"), s.get("slskd.cleanup"),
        )
        build = matching.build_ebook_candidates if ctx.get("media") == "ebook" else matching.build_candidates
        cands = build(responses, book, ctx, query)
        best = cands[0]["score"] if cands else 0
        job = self.get(job_id)
        attempts = (job.get("attempts") or []) + [{
            "query": query, "responses": len(responses), "candidates": len(cands), "best": best, "t": time.time(),
        }]
        self.update(job_id, attempts=attempts)
        self.log(job_id, f"“{query}” → {len(responses)} peers, {len(cands)} folders, best {best:.0f}")
        return cands

    async def run_search(self, job_id: int, queries: list[str] | None = None, force_review: bool = False) -> None:
        job = self.get(job_id)
        if not job:
            return
        self.update(job_id, status="searching", detail="Preparing", error=None)
        book, alt = await self._prepare_book(job)
        ctx = self._ctx(book, alt, job.get("media") or "audiobook")
        queries = queries or matching.build_queries(book, self.settings.get("matching.max_queries"))
        min_score = self.settings.get("matching.min_score")
        candidates = (job.get("candidates") or []) if force_review else []
        tried = set(((job.get("selected") or {}).get("tried") or []))
        if not force_review:
            self.update(job_id, attempts=[])
        for i, q in enumerate(queries, start=1):
            current = self.get(job_id)
            if not current or current["status"] != "searching":
                return  # cancelled meanwhile
            self.update(job_id, detail=f"Searching {i}/{len(queries)}: {q}")
            try:
                cands = await self._one_search(job_id, q, book, ctx)
            except (SlskdError, OSError, Exception) as e:  # noqa: BLE001
                self.log(job_id, f"Search “{q}” failed: {e}")
                if i == 1 and "connect" in str(e).lower():
                    self.update(job_id, status="failed", error=f"slskd unreachable: {e}", detail="slskd unreachable")
                    return
                continue
            candidates = self._merge(candidates, cands)
            self.update(job_id, candidates=candidates)
            viable = [c for c in candidates if c["id"] not in tried]
            if not force_review and viable and viable[0]["score"] >= min_score and \
                    self.settings.get("matching.auto_download"):
                break

        current = self.get(job_id)
        if not current or current["status"] != "searching":
            return
        viable = [c for c in candidates if c["id"] not in tried]
        if not force_review and viable and viable[0]["score"] >= min_score and self.settings.get("matching.auto_download"):
            self._spawn_download(job_id, viable[0], auto=True)
        elif not candidates:
            self.update(job_id, status="needs_review", detail="No results on Soulseek — try a custom search")
            self.log(job_id, "Nothing found")
        else:
            best = candidates[0]["score"]
            self.update(job_id, status="needs_review",
                        detail=f"{len(candidates)} candidates, best {best:.0f} — pick one")

    # ---------------------------------------------------------------- download
    def _spawn_download(self, job_id: int, cand: dict, auto: bool) -> None:
        """Queue the download in the background so a slow slskd enqueue doesn't hold up the next search."""
        job = self.get(job_id)
        sel = {k: v for k, v in cand.items() if k != "files"}
        sel.update({"tried": (job.get("selected") or {}).get("tried") or [], "pending": cand["id"],
                    "pending_auto": auto, "retry_at": time.time() + 300})  # watchdog if the enqueue never returns
        self.update(job_id, status="downloading", transfers=[], selected=sel, progress=0,
                    detail=f"Queueing {cand['file_count']} file(s) with {cand['username']}")
        self._enqueuing.add(job_id)

        async def go():
            try:
                await self.start_download(job_id, cand["id"], auto=auto)
            except Exception as e:  # noqa: BLE001
                log.exception("start_download failed for job %s", job_id)
                self.log(job_id, f"Couldn't queue the download: {e}")
            finally:
                self._enqueuing.discard(job_id)
        asyncio.create_task(go(), name=f"enqueue-{job_id}")

    async def start_download(self, job_id: int, candidate_id: str, auto: bool = False) -> None:
        job = self.get(job_id)
        if not job or job["status"] == "cancelled":
            return
        cand = next((c for c in job.get("candidates") or [] if c["id"] == candidate_id), None)
        if not cand:
            raise ValueError("Candidate not found")
        prev = job.get("selected") or {}
        tried = list(prev.get("tried") or [])
        self._clear_staging(job)
        # Group by sub-folder so each disc lands in its own destination folder.
        by_rel: dict[str, list[dict]] = {}
        for f in cand["files"]:
            by_rel.setdefault(f["rel"], []).append(f)
        transfers = []
        client = self.slskd()
        try:
            for rel, files in by_rel.items():
                cur = self.get(job_id)
                if not cur or cur["status"] == "cancelled":
                    break                      # removed while earlier discs were being queued
                dest = "/".join(["narratarr", str(job_id)] + ([rel] if rel else []))
                mode = await client.enqueue(cand["username"], files, dest)
                for f in files:
                    transfers.append({**f, "username": cand["username"], "mode": mode, "dest": dest,
                                      "state": "Requested", "percent": 0, "bytes": 0, "retries": 0,
                                      "local": None, "missing": 0})
        except SlskdBusy as e:
            # Not this source's fault: park the job and retry the same source with backoff.
            n = (prev.get("busy_attempts") or 0) + 1 if prev.get("pending") == cand["id"] else 1
            if n > BUSY_MAX_ATTEMPTS:
                self.update(job_id, status="needs_review", transfers=[], selected={**prev, "pending": None},
                            detail="slskd stayed busy — check slskd, then pick a source or Retry", error=str(e))
                self.log(job_id, f"Giving up after {n - 1} attempts: {e}")
                return
            wait = min(BUSY_WAIT * n, 300)
            sel = {k: v for k, v in cand.items() if k != "files"}
            sel.update({"tried": tried, "pending": cand["id"], "pending_auto": auto, "busy_attempts": n,
                        "retry_at": time.time() + wait})
            self.update(job_id, status="downloading", transfers=[], selected=sel, progress=0,
                        detail=f"slskd busy — retrying {cand['username']} in {wait}s (attempt {n}/{BUSY_MAX_ATTEMPTS})")
            self.log(job_id, f"{e} — will retry {cand['username']} in {wait}s")
            return
        except Exception as e:  # noqa: BLE001
            self.log(job_id, f"Enqueue from {cand['username']} failed: {e}")
            tried.append(cand["id"])
            self.update(job_id, selected={**cand, "files": None, "tried": tried})
            await self._next_candidate(job_id, f"enqueue failed: {e}")
            return
        latest = self.get(job_id)
        if not latest or latest["status"] == "cancelled":
            # removed while slskd was queueing it — take the transfers back out
            for t in transfers:
                for r in await client.user_downloads(t["username"]):
                    if r.get("filename") == t["filename"] and r.get("id"):
                        await client.cancel(t["username"], r["id"], remove=True)
            return
        now = time.time()
        selected = {k: v for k, v in cand.items() if k != "files"}
        selected.update({"tried": tried + [cand["id"]], "started_at": now, "last_progress_at": now,
                         "last_bytes": 0, "auto": auto, "pending": None})
        self.update(job_id, status="downloading", selected=selected, transfers=transfers, progress=0,
                    detail=f"Queued {len(transfers)} file(s) from {cand['username']}",
                    staging_path=str(self.staging_dir(job)), error=None)
        self.log(job_id, f"{'Auto-selected' if auto else 'Selected'} {cand['username']}: "
                         f"{cand['display_dir']} (score {cand['score']}, {len(transfers)} files)")

    def _clear_staging(self, job: dict) -> None:
        d = self.staging_dir(job)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    async def _cancel_transfers(self, job: dict) -> None:
        client = self.slskd()
        for t in job.get("transfers") or []:
            if t.get("transfer_id") and not t.get("local"):
                await client.cancel(t["username"], t["transfer_id"], remove=True)

    async def _next_candidate(self, job_id: int, reason: str) -> None:
        job = self.get(job_id)
        await self._cancel_transfers(job)
        tried = set((job.get("selected") or {}).get("tried") or [])
        min_score = self.settings.get("matching.min_score")
        nxt = next((c for c in job.get("candidates") or [] if c["id"] not in tried and c["score"] >= min_score), None)
        if nxt and self.settings.get("matching.auto_download"):
            self.log(job_id, f"Source failed ({reason}); trying next candidate")
            await self.start_download(job_id, nxt["id"], auto=True)
        else:
            self._clear_staging(job)
            self.update(job_id, status="needs_review", transfers=[], progress=0,
                        detail=f"Download failed ({reason}) — pick another source", error=reason)
            self.log(job_id, f"Download failed: {reason}")

    def _locate(self, job: dict, t: dict) -> Path | None:
        root = Path(self.settings.get("slskd.downloads_path"))
        name = t["name"]
        remote_parent = matching.split_remote(t["filename"])[-2] if len(matching.split_remote(t["filename"])) > 1 else ""
        guesses = [root / t["dest"] / name, root / remote_parent / name, root / name]
        for g in guesses:
            if g.is_file() and g.stat().st_size == t["size"]:
                return g
        # slskd may sanitise names (illegal chars → '_'); fall back to size + fuzzy name.
        squash = re.sub(r"[\W_]+", "", name.lower())
        best = None
        for dirpath, _dirs, filenames in os.walk(root):
            for fn in filenames:
                if re.sub(r"[\W_]+", "", fn.lower()) == squash:
                    p = Path(dirpath) / fn
                    try:
                        if p.stat().st_size == t["size"]:
                            if str(t["dest"]) in str(p) or remote_parent in str(p):
                                return p
                            best = best or p
                    except OSError:
                        continue
        return best

    def _stage(self, job: dict, t: dict, src: Path) -> Path:
        dst = self.staging_dir(job) / t["rel"] / t["name"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        root = Path(self.settings.get("slskd.downloads_path")).resolve()
        parent = src.parent
        while parent.resolve() != root and root in parent.resolve().parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return dst

    async def check_download(self, job_id: int) -> None:
        job = self.get(job_id)
        transfers = job.get("transfers") or []
        selected = job.get("selected") or {}
        if not transfers:
            if selected.get("pending") and job_id not in self._enqueuing and time.time() >= selected.get("retry_at", 0):
                await self.start_download(job_id, selected["pending"], auto=selected.get("pending_auto", True))
            return
        client = self.slskd()
        try:
            remote = await client.user_downloads(selected["username"])
        except Exception as e:  # noqa: BLE001
            self.update(job_id, detail=f"Can't reach slskd: {e}")
            return
        latest: dict[str, dict] = {}
        for r in remote:
            fn = r.get("filename")
            key = r.get("requestedAt") or r.get("enqueuedAt") or ""
            if fn not in latest or key >= (latest[fn].get("requestedAt") or latest[fn].get("enqueuedAt") or ""):
                latest[fn] = r
        failure = None
        for t in transfers:
            if t.get("local") or t.get("missing_local"):
                continue
            r = latest.get(t["filename"])
            if not r:
                t["missing"] = t.get("missing", 0) + 1
                if t["missing"] > 24:
                    failure = "transfer disappeared from slskd"
                continue
            t["missing"] = 0
            t["transfer_id"] = r.get("id")
            t["state"] = r.get("state", "")
            t["bytes"] = int(r.get("bytesTransferred") or 0)
            t["percent"] = float(r.get("percentComplete") or 0)
            if transfer_succeeded(t["state"]):
                src = self._locate(job, t)
                if src:
                    t["local"] = str(self._stage(job, t, src))
                    if self.settings.get("slskd.cleanup") and t.get("transfer_id"):
                        await client.cancel(t["username"], t["transfer_id"], remove=True)
                else:
                    t["locate_attempts"] = t.get("locate_attempts", 0) + 1
                    if t["locate_attempts"] == 3:
                        self.log(job_id, f"Finished file not found under slskd downloads path: {t['name']}")
                    if t["locate_attempts"] > 20:
                        if not any(x.get("local") or x.get("missing_local") for x in transfers):
                            # nothing from this book has ever been found: almost certainly a mount problem
                            self.update(job_id, transfers=transfers, status="failed",
                                        error="Downloaded files not found — check the slskd downloads folder setting "
                                              "and that it's mounted into this container",
                                        detail="Can't find downloaded files")
                            return
                        # other files arrived fine, so something else moved this one; carry on with the rest
                        t["missing_local"] = True
                        t["state"] = "Missing after download"
                        self.log(job_id, f"{t['name']} finished in slskd but disappeared before Narratarr could move it. "
                                         "Another app watching slskd's completed folder (e.g. a music auto-importer) "
                                         "probably took it. Continuing with the other files.")
            elif transfer_failed(t["state"]):
                if time.time() < t.get("retry_after", 0):
                    continue
                if t["retries"] < MAX_FILE_RETRIES:
                    t["retries"] += 1
                    self.log(job_id, f"{t['name']}: {t['state']} — retry {t['retries']}")
                    try:
                        await client.cancel(t["username"], t["transfer_id"], remove=True)
                        await client.enqueue(t["username"], [t], t["dest"])
                        t["state"] = "Requested"
                    except SlskdBusy as e:
                        t["retries"] -= 1  # slskd's problem, not the source's — try again shortly
                        t["retry_after"] = time.time() + BUSY_WAIT
                        self.log(job_id, f"{e} — retrying {t['name']} in {BUSY_WAIT}s")
                    except Exception as e:  # noqa: BLE001
                        failure = f"retry failed: {e}"
                else:
                    failure = f"{t['name']}: {t['state']}"

        total = sum(t["size"] for t in transfers) or 1
        done = sum(t["size"] if (t.get("local") or t.get("missing_local")) else t.get("bytes", 0) for t in transfers)
        progress = round(done / total * 100, 1)
        now = time.time()
        if done > selected.get("last_bytes", 0):
            selected["last_bytes"] = done
            selected["last_progress_at"] = now
        n_local = sum(1 for t in transfers if t.get("local"))
        n_missing = sum(1 for t in transfers if t.get("missing_local"))
        states = {t["state"] for t in transfers if not t.get("local") and not t.get("missing_local")}
        detail = f"{n_local}/{len(transfers)} files · {progress:.0f}%" + (f" · {', '.join(sorted(states))}" if states else "")
        self.update(job_id, transfers=transfers, progress=progress, selected=selected, detail=detail)

        if failure:
            await self._next_candidate(job_id, failure)
            return
        stall = self.settings.get("slskd.stall_minutes") * 60
        if now - selected.get("last_progress_at", now) > stall:
            await self._next_candidate(job_id, f"no progress for {self.settings.get('slskd.stall_minutes')} min")
            return
        if n_local + n_missing == len(transfers):
            if n_missing:
                self.update(job_id, status="needs_review", progress=100,
                            detail=f"{n_missing} of {len(transfers)} file(s) went missing after downloading — "
                                   "another app probably moved them. Re-download them, or import what's here.")
                self.log(job_id, f"Downloads finished; {n_missing} file(s) missing")
            else:
                self.update(job_id, status="processing", detail="Download complete — processing", progress=100)
                self.log(job_id, "All files downloaded")

    # -------------------------------------------------------------- processing
    def create_reencode(self, book: dict, folder: Path, abs_item_id: str | None = None,
                        library_id: int | None = None, user_id: str | None = None, merge: bool = True,
                        kbps: int | None = None) -> int:
        s = _book_summary(book)
        s["asin"] = book.get("asin") or book.get("ref")
        now = time.time()
        book = {**book, "_library_id": library_id, "_merge": bool(merge), "_kbps": kbps}
        jid = self.db.insert("jobs", {
            "kind": "reencode", **s, "book": book, "status": "processing", "detail": "Waiting to re-encode",
            "staging_path": str(folder), "abs_item_id": abs_item_id, "requested_by": user_id,
            "created_at": now, "updated_at": now,
        })
        self.log(jid, f"Re-encode requested for {folder}")
        return jid

    async def run_reencode(self, job_id: int) -> None:
        from .postprocess import _mb, audio_files, safe_segment
        from .reencode import current_kbps, effective_kbps, embedded_cover, reencode, reencode_each, should_reencode
        from .tagging import audio_length, tag_book
        job = self.get(job_id)
        folder = Path(job["staging_path"])
        merge = (job["book"] or {}).get("_merge", True)
        kbps = int((job["book"] or {}).get("_kbps") or self.settings.get("processing.reencode_kbps") or 64)
        book = {k: v for k, v in (job["book"] or {}).items() if not k.startswith("_")}
        try:
            if not folder.is_dir():
                raise FileNotFoundError(f"Folder not found: {folder}")
            files = audio_files(folder)
            if not should_reencode(files, kbps, merge):
                self.update(job_id, status="completed", output_path=str(folder),
                            detail=f"Nothing to do: already at or below {kbps} kbps")
                return
            before = sum(p.stat().st_size for p in files)
            cover = None
            for name in ("cover.jpg", "cover.jpeg", "cover.png"):
                if (folder / name).exists():
                    cover = (folder / name).read_bytes()
                    break
            if cover is None:
                cover = await asyncio.to_thread(embedded_cover, files[0])
            label = "Re-encoding to M4B" if merge else "Re-encoding"
            self.update(job_id, detail=f"{label} · 0%")
            prog = lambda pct: self.update(job_id, detail=f"{label} · {pct}%")  # noqa: E731
            if merge:
                kbps = effective_kbps(files, kbps)
                self.log(job_id, f"Joining {len(files)} file(s) ({current_kbps(files) or 0:.0f} kbps) into one M4B at {kbps} kbps")
                new_files = [await asyncio.to_thread(reencode, files, folder,
                                                     safe_segment(book.get("title") or "Audiobook"), book, kbps, prog)]
            else:
                self.log(job_id, f"Re-encoding {len(files)} file(s) ({current_kbps(files) or 0:.0f} kbps) one by one "
                                 f"at up to {kbps} kbps, keeping their names")
                new_files = await asyncio.to_thread(reencode_each, files, kbps, prog)
            if self.settings.get("processing.write_tags"):
                await asyncio.to_thread(tag_book, new_files, book,
                                        cover if self.settings.get("processing.embed_cover") else None, False,
                                        lambda m: self.log(job_id, m))
            after = sum(p.stat().st_size for p in new_files)
            duration = sum(audio_length(p) or 0 for p in new_files)
            summary = {"file_count": len(new_files), "size_bytes": after, "duration_s": round(duration, 1)}
            lib_id = (job["book"] or {}).get("_library_id")
            if lib_id:
                self.db.update("library", lib_id, summary)
            marker = folder / ".narratarr.json"
            if marker.exists():
                try:
                    data = json.loads(marker.read_text(encoding="utf-8"))
                    data.setdefault("summary", {}).update(summary)
                    marker.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                except (OSError, ValueError):
                    pass
            item_id = job.get("abs_item_id")
            if item_id and self.abs.enabled:
                try:
                    await self.abs.client().scan_item(item_id)
                    self.log(job_id, "Asked Audiobookshelf to rescan the item")
                except Exception as e:  # noqa: BLE001
                    self.log(job_id, f"Audiobookshelf rescan failed (it will pick the change up on its next scan): {e}")
                try:
                    await self.abs.refresh_index()
                except Exception:  # noqa: BLE001
                    pass
            msg = f"Re-encoded: {_mb(before)} → {_mb(after)}"
            self.log(job_id, msg)
            self.update(job_id, status="completed", output_path=str(folder), progress=100, detail=msg)
        except Exception as e:  # noqa: BLE001
            log.exception("re-encode failed for job %s", job_id)
            self.update(job_id, status="failed", error=str(e),
                        detail="Re-encode failed — original files kept" if merge
                        else "Re-encode failed — files not yet re-encoded were kept")
            self.log(job_id, f"Re-encode failed: {e}")

    async def run_processing(self, job_id: int) -> None:
        job = self.get(job_id)
        if job and job["kind"] == "reencode":
            await self.run_reencode(job_id)
            return
        src = Path(job["staging_path"] or self.staging_dir(job))
        book = clean_contributors({k: v for k, v in job["book"].items()})
        try:
            if job["kind"] == "download" and not book.get("_enriched") and (book.get("asin") or book.get("ref")):
                book, _ = await self._prepare_book(job)
            self.update(job_id, detail="Tagging and organising")
            selected = job.get("selected") or {}
            source = {"kind": job["kind"], "username": selected.get("username"),
                      "remote_dir": selected.get("display_dir"), "score": selected.get("score"),
                      "query": selected.get("query"), "local_source": str(src) if job["kind"] == "import" else None}
            clean_book = {k: v for k, v in book.items() if not k.startswith("_")}
            media = job.get("media") or "audiobook"
            if media == "ebook":
                self.update(job_id, detail="Organising")
                summary = await process_ebook(src, clean_book, self.settings, copy=job.get("import_mode") == "copy",
                                              source_info=source, log_fn=lambda m: self.log(job_id, m))
            else:
                summary = await process_book(src, clean_book, self.settings, copy=job.get("import_mode") == "copy",
                                             source_info=source, log_fn=lambda m: self.log(job_id, m),
                                             progress_fn=lambda pct: self.update(job_id, detail=f"Re-encoding to M4B · {pct}%"))
            self._library_upsert(job, clean_book, summary)
            send = self.abs.enabled_for(media)
            self.update(job_id, status="completed", output_path=summary["path"], progress=100,
                        detail=f"Imported to {summary['path']}", abs_status="pending" if send else None,
                        abs_detail="Sending to Audiobookshelf" if send else None)
            self.log(job_id, "Completed")
            if send:
                self.start_abs_import(job_id)
        except Exception as e:  # noqa: BLE001
            log.exception("processing failed for job %s", job_id)
            self.update(job_id, status="failed", error=str(e),
                        detail="Processing failed — files left in place for Manual Import")
            self.log(job_id, f"Processing failed: {e}")

    # ---------------------------------------------------------- audiobookshelf
    def start_abs_import(self, job_id: int) -> bool:
        """Run the Audiobookshelf step in the background (it can wait minutes for a scan)."""
        if job_id in self._abs_inflight:
            return False
        self._abs_inflight.add(job_id)
        asyncio.create_task(self._abs_import(job_id))
        return True

    async def _abs_import(self, job_id: int) -> None:
        try:
            job = self.get(job_id)
            if not job or not job.get("output_path"):
                return
            self.update(job_id, abs_status="pending", abs_detail="Sending to Audiobookshelf")
            result = await self.abs.import_book(job, lambda m: self.log(job_id, m))
            lib = self.db.one("SELECT id FROM library WHERE job_id=?", (job_id,))
            values = {"abs_status": result["status"], "abs_detail": result["detail"]}
            if result.get("path"):
                values["output_path"] = result["path"]
            if result["status"] == "imported":
                values["abs_item_id"] = result["item_id"]
                self.db.execute("INSERT OR REPLACE INTO abs_links(item_id, job_id, asin, linked_at) VALUES (?,?,?,?)",
                                (result["item_id"], job_id, job.get("asin"), time.time()))
                values["detail"] = f"Imported to {result.get('path') or job.get('output_path')}"
                if self.settings.get("abs.delete_after_import"):
                    if lib:
                        self.db.execute("DELETE FROM library WHERE id=?", (lib["id"],))
                    self.log(job_id, "Removed from Narratarr's library (now in Audiobookshelf)")
                elif lib:
                    self.db.update("library", lib["id"], {"abs_item_id": result["item_id"], "abs_status": "imported"})
            elif lib:
                upd = {"abs_status": "pending"}
                if result.get("path") and self.settings.get("abs.delete_after_import"):
                    upd["path"] = result["path"]
                self.db.update("library", lib["id"], upd)
            self.update(job_id, **values)
            if result["status"] == "imported":
                try:
                    await self.abs.refresh_index()
                except Exception as e:  # noqa: BLE001
                    log.warning("ABS index refresh failed: %s", e)
        except Exception as e:  # noqa: BLE001
            log.exception("ABS import failed for job %s", job_id)
            self.update(job_id, abs_status="pending", abs_detail=f"Error: {e}")
        finally:
            self._abs_inflight.discard(job_id)

    def queue_abs_for_library_item(self, item_id: int) -> int:
        """Send an existing library row to Audiobookshelf (creates a job record if it has none)."""
        row = self.db.one("SELECT * FROM library WHERE id=?", (item_id,))
        if not row:
            raise ValueError("Library item not found")
        job = self.get(row["job_id"]) if row.get("job_id") else None
        if not job:
            book = row["book"] or {}
            now = time.time()
            jid = self.db.insert("jobs", {
                "kind": "import", **_book_summary(book), "book": book, "status": "completed",
                "media": row.get("media") or "audiobook",
                "detail": "Sending to Audiobookshelf", "output_path": row["path"], "created_at": now, "updated_at": now,
            })
            self.db.update("library", row["id"], {"job_id": jid})
        else:
            jid = job["id"]
            self.update(jid, output_path=row["path"])
        self.start_abs_import(jid)
        return jid

    async def _abs_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                if self.abs.enabled:
                    try:
                        await self.abs.refresh_index()
                    except Exception as e:  # noqa: BLE001
                        log.warning("Audiobookshelf sync failed: %s", e)
                    if self.abs.last_error is None:
                        for j in self.db.query("SELECT id FROM jobs WHERE abs_status='pending' AND status='completed'"):
                            self.start_abs_import(j["id"])
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("abs loop error")
            self.abs.wake.clear()
            try:
                await asyncio.wait_for(self.abs.wake.wait(), timeout=max(1, int(self.settings.get("abs.sync_minutes"))) * 60)
            except asyncio.TimeoutError:
                pass

    def _library_upsert(self, job: dict, book: dict, summary: dict) -> None:
        values = {
            "job_id": job["id"], "asin": book.get("asin"), "title": book.get("title") or "Unknown",
            "subtitle": book.get("subtitle"), "authors": book.get("authors") or [],
            "narrators": book.get("narrators") or [], "series": book.get("series") or [],
            "path": summary["path"], "cover_path": summary.get("cover_path"), "file_count": summary["file_count"],
            "size_bytes": summary["size_bytes"], "duration_s": summary["duration_s"], "book": book,
            "source": job["kind"], "added_at": time.time(), "media": job.get("media") or "audiobook",
        }
        existing = self.db.one("SELECT id FROM library WHERE path=?", (summary["path"],))
        if existing:
            self.db.update("library", existing["id"], values)
        else:
            self.db.insert("library", values)

    async def _monitor_loop(self) -> None:
        while True:
            try:
                for job in self.db.query("SELECT id FROM jobs WHERE status='downloading'"):
                    await self.check_download(job["id"])
                for job in self.db.query("SELECT id FROM jobs WHERE status='processing' ORDER BY updated_at"):
                    # one book at a time, in the background, so a long re-encode doesn't stall downloads
                    if not self._busy:
                        jid = job["id"]
                        self._busy.add(jid)
                        task = asyncio.create_task(self.run_processing(jid), name=f"process-{jid}")
                        task.add_done_callback(lambda t, jid=jid: self._busy.discard(jid))
                    break
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("monitor loop error")
            await asyncio.sleep(max(1, int(self.settings.get("general.poll_seconds"))))

    # ------------------------------------------------------------ user actions
    async def cancel(self, job_id: int) -> None:
        job = self.get(job_id)
        if not job:
            return
        task = self._search_tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()                       # stops the slskd search right away
        if job["status"] == "downloading":
            await self._cancel_transfers(job)
            self._clear_staging(job)
        self.update(job_id, status="cancelled", detail="Cancelled", transfers=[])
        self.log(job_id, "Cancelled")
        self.wake.set()                         # let the next queued job start immediately

    async def redownload_missing(self, job_id: int) -> int:
        job = self.get(job_id)
        transfers = job.get("transfers") or []
        missing = [t for t in transfers if t.get("missing_local")]
        if not missing:
            return 0
        client = self.slskd()
        for t in missing:
            if t.get("transfer_id"):
                await client.cancel(t["username"], t["transfer_id"], remove=True)
            await client.enqueue(t["username"], [t], t["dest"])
            t.update({"missing_local": False, "state": "Requested", "percent": 0, "bytes": 0, "locate_attempts": 0,
                      "missing": 0, "transfer_id": None})
        selected = job.get("selected") or {}
        selected["last_progress_at"] = time.time()
        self.update(job_id, status="downloading", transfers=transfers, selected=selected,
                    detail=f"Re-downloading {len(missing)} missing file(s)", error=None)
        self.log(job_id, f"Re-downloading {len(missing)} missing file(s)")
        return len(missing)

    def import_partial(self, job_id: int) -> None:
        job = self.get(job_id)
        n = sum(1 for t in job.get("transfers") or [] if t.get("local"))
        self.update(job_id, status="processing", detail=f"Importing the {n} file(s) that arrived")
        self.log(job_id, f"Importing {n} file(s) without the missing ones")

    def activity(self) -> dict:
        """What's happening right now, for the sidebar activity panel."""
        now = time.time()
        items = []
        for j in self.db.query("SELECT id, title, status, detail, progress, abs_status, updated_at FROM jobs "
                               "WHERE status IN ('searching','downloading','processing') ORDER BY updated_at DESC"):
            items.append({"id": j["id"], "title": j["title"], "kind": j["status"], "text": j["detail"] or "",
                          "progress": j["progress"] if j["status"] in ("downloading", "processing") else None})
        for jid in list(self._abs_inflight):
            j = self.get(jid)
            if j:
                items.append({"id": jid, "title": j["title"], "kind": "abs", "text": j.get("abs_detail") or
                              "Sending to Audiobookshelf", "progress": None})
        if self.abs.syncing:
            items.append({"id": None, "title": "Audiobookshelf", "kind": "sync", "text": "Syncing library", "progress": None})
        recent = self.db.query("SELECT id, title, status, detail FROM jobs WHERE updated_at > ? "
                               "AND status IN ('completed','failed','needs_review') ORDER BY updated_at DESC LIMIT 3",
                               (now - 12,))
        for j in recent:
            items.append({"id": j["id"], "title": j["title"], "kind": j["status"],
                          "text": {"completed": "Completed", "failed": "Failed", "needs_review": "Needs review"}[j["status"]],
                          "progress": None})
        queued = self.db.one("SELECT COUNT(*) n FROM jobs WHERE status='queued'")["n"]
        return {"items": items, "queued": queued}

    def retry(self, job_id: int) -> None:
        job = self.get(job_id)
        if job["kind"] in ("import", "reencode"):
            self.update(job_id, status="processing", detail="Retrying", error=None)
        elif job.get("staging_path") and Path(job["staging_path"]).exists() and \
                all(t.get("local") for t in job.get("transfers") or [{}]) and job.get("transfers"):
            self.update(job_id, status="processing", detail="Retrying processing", error=None)
        else:
            self.update(job_id, status="queued", detail="Waiting to search", error=None,
                        candidates=[], selected=None, transfers=[], progress=0)
        self.log(job_id, "Retry requested")
        self.wake.set()

    def start_custom_search(self, job_id: int, query: str) -> None:
        old = self._search_tasks.get(job_id)
        if old and not old.done():
            old.cancel()
        task = asyncio.create_task(self.custom_search(job_id, query), name=f"custom-search-{job_id}")
        self._search_tasks[job_id] = task
        task.add_done_callback(lambda t: self._search_tasks.pop(job_id, None) if self._search_tasks.get(job_id) is t else None)

    async def custom_search(self, job_id: int, query: str) -> None:
        job = self.get(job_id)
        if job["status"] == "downloading":
            await self._cancel_transfers(job)
        self.update(job_id, status="searching", detail=f"Custom search: {query}")
        self.log(job_id, f"Custom search: {query}")
        await self.run_search(job_id, [query], force_review=True)
