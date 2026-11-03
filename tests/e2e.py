"""End-to-end test against tests/mocks/mock_server.py.

    python tests/e2e.py   (starts the mock + Narratarr on spare ports, runs scenarios)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles
import tempfile
import time
from pathlib import Path

import httpx
import mutagen

ROOT = Path(__file__).resolve().parent.parent
WORK = Path(os.environ.get("E2E_WORK") or tempfile.mkdtemp(prefix="narratarr-e2e-"))
WORK.mkdir(parents=True, exist_ok=True)
MOCK_PORT, APP_PORT = 9100, 8787
APP = f"http://127.0.0.1:{APP_PORT}"


def start():
    shutil.rmtree(WORK / "config", ignore_errors=True)
    shutil.rmtree(WORK / "downloads", ignore_errors=True)
    shutil.rmtree(WORK / "slskd-downloads", ignore_errors=True)
    mock = subprocess.Popen([sys.executable, str(ROOT / "tests/mocks/mock_server.py"), str(WORK), str(MOCK_PORT)])
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "NARRATARR_DATA_DIR": str(WORK / "config"),
        "NARRATARR_AUDIBLE_BASE_OVERRIDE": f"http://127.0.0.1:{MOCK_PORT}/{{region}}",
        "NARRATARR_METADATA__AUDNEXUS_URL": f"http://127.0.0.1:{MOCK_PORT}/audnexus",
        "NARRATARR_SLSKD__URL": f"http://127.0.0.1:{MOCK_PORT}",
        "NARRATARR_SLSKD__API_KEY": "testkey",
        "NARRATARR_METADATA__REGION": "au",
        "NARRATARR_METADATA__FALLBACK_REGION": "us",
        "NARRATARR_SLSKD__DOWNLOADS_PATH": str(WORK / "slskd-downloads"),
        "NARRATARR_PATHS__DOWNLOADS": str(WORK / "downloads"),
        "NARRATARR_SLSKD__SEARCH_SECONDS": "5",
        "NARRATARR_GENERAL__POLL_SECONDS": "1",
        "NARRATARR_BUSY_WAIT": "2",
        "NARRATARR_ABS_WATCHER_WAIT": "8",
        "NARRATARR_HARDCOVER_URL_OVERRIDE": f"http://127.0.0.1:{MOCK_PORT}/hardcover/v1/graphql",
        "NARRATARR_UPDATE_URL_OVERRIDE": f"http://127.0.0.1:{MOCK_PORT}/github/releases/latest",
        "NARRATARR_VERSION": "0.1.0",
    }
    app = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(APP_PORT), "--log-level", "warning"],
                           cwd=ROOT, env=env, stdout=open(WORK / "app.log", "w", encoding="utf-8"), stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            httpx.get(f"{APP}/api/system/status", timeout=1).raise_for_status()
            httpx.get(f"http://127.0.0.1:{MOCK_PORT}/au/1.0/catalog/products", timeout=1)
            return mock, app
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise SystemExit("servers didn't start")


def wait_status(c, jid, wanted, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in wanted:
            return j
        time.sleep(0.5)
    raise AssertionError(f"job {jid} stuck in {j['status']}: {j['detail']}\n" + "\n".join(l['msg'] for l in j['log']))


def search(c, q):
    return c.get("/api/search", params={"q": q}).json()["results"]


def request(c, book):
    r = c.post("/api/jobs", json={"asin": book["asin"], "region": book["region"], "book": book})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def ok(msg):
    print(f"  ✓ {msg}")


def main():
    mock, app = start()
    failures = 0
    try:
        c = httpx.Client(base_url=APP, timeout=30)
        assert c.post("/api/settings/test/slskd").json()["ok"]
        ok("slskd connection test")

        # 1 — narrator-aware search, single m4b, batch destination, chapters embedded
        print("Scenario 1: single-file m4b, narrator-specific edition")
        res = search(c, "tiny test")
        assert {r["asin"] for r in res} == {"B0TINY0001", "B0TINY0002"}, res
        book = next(r for r in res if r["narrators"] == ["Ray Reader"])
        assert book["series"][0]["name"] == "Tiny Saga" and book["region"] == "au"
        assert "._SL500_" not in book["cover_url"]
        ok("Audible AU search returns both narrations; hi-res cover URL")
        jid = request(c, book)
        assert c.post("/api/jobs", json={"asin": book["asin"]}).status_code == 409
        ok("duplicate request rejected")
        j = wait_status(c, jid, {"completed", "failed", "needs_review"}, 90)
        assert j["status"] == "completed", (j["status"], j["detail"], [l["msg"] for l in j["log"]])
        assert j["selected"]["username"] == "goodpeer", j["selected"]
        out = Path(j["output_path"])
        assert out == WORK / "downloads/completed/Jane Author/Tiny Saga/Book 1 - The Tiny Test_ A Novel" or out.name.startswith("Book 1 - The Tiny Test"), out
        f = out / "The Tiny Test.m4b"
        assert f.exists(), list(out.iterdir())
        tags = mutagen.File(str(f))
        assert tags["\xa9alb"] == ["The Tiny Test: A Novel"], tags.get("\xa9alb")
        assert tags["\xa9wrt"] == ["Ray Reader"] and tags["\xa9ART"] == ["Jane Author"]
        assert tags["\xa9mvn"] == ["Tiny Saga"] and tags["----:com.apple.iTunes:ASIN"][0] == b"B0TINY0001"
        assert tags.get("covr"), "cover not embedded"
        probe = json.loads(subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_chapters", str(f)],
                                          capture_output=True, text=True).stdout)
        assert [ch["tags"]["title"] for ch in probe["chapters"]] == ["Opening Credits", "Chapter 1", "Chapter 2", "End Credits"], probe
        for side in ("cover.jpg", "desc.txt", "reader.txt", "metadata.opf", "metadata.json", ".narratarr.json"):
            assert (out / side).exists(), side
        meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
        assert meta["narrators"] == ["Ray Reader"] and meta["series"] == ["Tiny Saga #1"] and len(meta["chapters"]) == 4
        assert "opf:role=\"nrt\">Ray Reader" in (out / "metadata.opf").read_text(encoding="utf-8")
        assert not any((WORK / "slskd-downloads").rglob("*.m4b")), "slskd downloads not cleaned"
        ok(f"downloaded from goodpeer → {out.relative_to(WORK)} with tags, cover, 4 chapters, sidecars")

        # 2 — legacy slskd (no batches), multi-disc mp3, names preserved
        print("Scenario 2: multi-disc mp3 via legacy enqueue")
        httpx.post(f"http://127.0.0.1:{MOCK_PORT}/mock/config", json={"batches": False})
        book = search(c, "long road home")[0]
        jid = request(c, book)
        j = wait_status(c, jid, {"completed", "failed", "needs_review"}, 90)
        assert j["status"] == "completed", (j["detail"], [l["msg"] for l in j["log"]])
        out = Path(j["output_path"])
        files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.mp3"))
        assert files == ["CD1/1-01 Long Road Home.mp3", "CD1/1-02 Long Road Home.mp3",
                         "CD2/2-01 Long Road Home.mp3", "CD2/2-02 Long Road Home.mp3"], files
        t = mutagen.File(str(out / "CD2/2-02 Long Road Home.mp3"))
        assert str(t["TPOS"]) == "2/2" and str(t["TRCK"]) == "4/4" and str(t["TALB"]) == "Long Road Home"
        assert str(t["TCOM"]) == "Kim Narrator" and str(t["TXXX:SERIES"]) == "Road Series" and str(t["TXXX:SERIES-PART"]) == "2"
        ok("4 files across CD1/CD2 kept their names; disc/track numbers + narrator/series tags set")
        httpx.post(f"http://127.0.0.1:{MOCK_PORT}/mock/config", json={"batches": True})

        # 3 — failing source → automatic fallback to next candidate
        print("Scenario 3: source fails, next candidate used")
        book = search(c, "fallback book")[0]
        jid = request(c, book)
        j = wait_status(c, jid, {"completed", "failed", "needs_review"}, 120)
        assert j["status"] == "completed", (j["detail"], [l["msg"] for l in j["log"]])
        assert j["selected"]["username"] == "steadypeer", j["selected"]
        assert any("retry" in l["msg"] for l in j["log"]) and any("next candidate" in l["msg"] for l in j["log"])
        ok("flakypeer errored (2 retries), fell back to steadypeer and completed")

        # 3b — slskd busy (500 "wait timed out") must not burn through sources
        print("Scenario 3b: slskd busy → back off, same source")
        c.delete(f"/api/jobs/{jid}")
        httpx.post(f"http://127.0.0.1:{MOCK_PORT}/mock/config", json={"busy": 5})
        jid = request(c, book)
        j = wait_status(c, jid, {"completed", "failed", "needs_review"}, 120)
        assert j["status"] == "completed", (j["detail"], [l["msg"] for l in j["log"]])
        assert j["selected"]["username"] == "flakypeer" or j["selected"]["username"] == "steadypeer", j["selected"]
        assert any("slskd busy" in l["msg"] for l in j["log"]), [l["msg"] for l in j["log"]]
        assert not any("trying next candidate" in l["msg"] and "busy" in l["msg"] for l in j["log"])
        assert len(j["selected"]["tried"]) <= 2, j["selected"]["tried"]
        ok("5 busy responses → backed off and retried the same source, no candidates burned")

        # 3c — removing/cancelling the active job hands over to the next one immediately
        print("Scenario 3c: queue hand-off with slow searches and enqueues")
        mock_cfg = lambda **kw: httpx.post(f"http://127.0.0.1:{MOCK_PORT}/mock/config", json=kw)
        c.put("/api/settings", json={"slskd.search_seconds": 30}).raise_for_status()
        mock_cfg(search_secs=60)                       # searches run their whole window, like real slskd
        nowhere = search(c, "nowhere to be found")[0]
        a = request(c, nowhere)
        wait_status(c, a, {"searching"}, 30)
        b = request(c, search(c, "imported tales")[0])
        time.sleep(3)
        c.delete(f"/api/jobs/{a}").raise_for_status()
        t0 = time.time()
        wait_status(c, b, {"searching"}, 15)
        ok(f"removed a job mid-search; next job started searching after {time.time() - t0:.1f}s (search window is 30s)")
        c.post(f"/api/jobs/{b}/cancel").raise_for_status()
        c.delete(f"/api/jobs/{b}").raise_for_status()

        mock_cfg(search_secs=1, enqueue_delay=20)      # slskd slow to accept the download
        c.put("/api/settings", json={"slskd.search_seconds": 5}).raise_for_status()
        a = request(c, search(c, "long road home")[0])
        b = request(c, nowhere)
        t0 = time.time()
        for _ in range(60):
            if c.get(f"/api/jobs/{a}").json()["status"] == "downloading":
                break
            time.sleep(0.5)
        wait_status(c, b, {"searching", "needs_review"}, 15)
        ok(f"next job started searching {time.time() - t0:.1f}s in, while the previous download was still being queued (20s)")
        c.delete(f"/api/jobs/{a}").raise_for_status()
        c.post(f"/api/jobs/{b}/cancel"); c.delete(f"/api/jobs/{b}")
        def live_transfers():
            r = httpx.get(f"http://127.0.0.1:{MOCK_PORT}/api/v0/transfers/downloads/discpeer", headers={"X-API-Key": "testkey"})
            files = (r.json().get("directories") or [{}])[0].get("files", []) if r.status_code == 200 else []
            return [t for t in files if not t["state"].startswith("Completed")]
        time.sleep(22)                                 # the slow enqueue for disc 1 returns, then gets undone
        for _ in range(20):
            if not live_transfers():
                break
            time.sleep(1)
        assert not live_transfers(), "transfers queued for a removed job must be cancelled"
        ok("transfers that slskd accepted after the job was removed were cancelled")
        mock_cfg(search_secs=2, enqueue_delay=0)

        # 4 — no results → needs_review, custom search, manual pick
        print("Scenario 4: nothing found → needs review → custom search → pick")
        book = search(c, "nowhere to be found")[0]
        jid = request(c, book)
        j = wait_status(c, jid, {"needs_review", "failed"}, 90)
        assert j["status"] == "needs_review" and not j["candidates"], j["detail"]
        assert len(j["attempts"]) >= 3
        ok(f"{len(j['attempts'])} query variations tried, then parked for review")
        c.post(f"/api/jobs/{jid}/search", json={"query": "jane author everything"}).raise_for_status()
        j = wait_status(c, jid, {"needs_review"}, 30)
        time.sleep(0.5)
        j = wait_status(c, jid, {"needs_review"}, 30)
        assert j["candidates"], j
        cand = j["candidates"][0]
        c.post(f"/api/jobs/{jid}/select", json={"candidate_id": cand["id"]}).raise_for_status()
        j = wait_status(c, jid, {"completed", "failed", "needs_review"}, 60)
        assert j["status"] == "completed", j["detail"]
        ok(f"manual pick of {cand['display_dir']} (score {cand['score']}) completed")

        # 5 — manual import
        print("Scenario 5: manual import from incomplete/")
        src = WORK / "downloads/incomplete/Ima Porter - Imported Tales (2020) [64kbps]"
        src.mkdir(parents=True, exist_ok=True)
        shutil.copy(WORK / "src/part.mp3", src / "01 - part one.mp3")
        shutil.copy(WORK / "src/part.mp3", src / "02 - part two.mp3")
        folders = c.get("/api/import/scan", params={"root": "incomplete"}).json()
        f = next(x for x in folders if x["name"].startswith("Ima Porter"))
        assert f["suggestion"] == "Ima Porter Imported Tales", f["suggestion"]
        book = search(c, f["suggestion"])[0]
        r = c.post("/api/import", json={"root": "incomplete", "rel": f["rel"], "asin": book["asin"], "region": book["region"], "mode": "move", "book": book})
        r.raise_for_status()
        j = wait_status(c, r.json()["id"], {"completed", "failed"}, 30)
        assert j["status"] == "completed", j
        out = Path(j["output_path"])
        assert (out / "01 - part one.mp3").exists() and not src.exists()
        ok(f"folder name cleaned to “{f['suggestion']}”, imported to {out.relative_to(WORK)}")
        assert c.post("/api/import", json={"root": "incomplete", "rel": "../../..", "asin": "x"}).status_code == 400
        ok("path traversal rejected")

        # 6 — region fallback
        print("Scenario 6: AU → US fallback")
        res = search(c, "american exclusive")
        assert res and res[0]["region"] == "us"
        ok("US result returned when AU has none")

        # 7 — library
        lib = c.get("/api/library").json()["narratarr"]
        assert len(lib) == 5, len(lib)
        assert c.get(f"/api/library/{lib[0]['id']}/cover").status_code == 200
        c.post("/api/library/rescan").raise_for_status()
        assert len(c.get("/api/library").json()["narratarr"]) == 5
        ok("library lists 5 books, covers served, rescan idempotent")
        assert c.get("/queue").status_code == 200 and "Narratarr" in c.get("/library").text
        ok("SPA routes served")

        # 6b — Audible role suffixes ("Name - editor") must not leak into searches, tags or folders
        print("Scenario 6b: contributor roles stripped")
        ed = search(c, "essays on rivers")[0]
        assert [a["name"] for a in ed["authors"]] == ["Lena Krause", "Omar Field"], ed["authors"]
        assert [a["role"] for a in ed["authors"]] == ["editor", "foreword"], ed["authors"]
        jid = request(c, ed)
        j = wait_status(c, jid, {"completed", "failed", "needs_review"}, 90)
        assert j["status"] == "completed", (j["detail"], [l["msg"] for l in j["log"]])
        assert not any("editor" in a["query"] or "foreword" in a["query"] or "contributing" in a["query"]
                       for a in j["attempts"]), [a["query"] for a in j["attempts"]]
        out = Path(j["output_path"])
        assert out.parts[-2] == "Lena Krause, Omar Field", out
        t = mutagen.File(str(out / "Essays On Rivers.m4b"))
        assert t["\xa9ART"] == ["Lena Krause, Omar Field"], t.get("\xa9ART")
        ok(f"roles split off (editor, foreword); queries {[a['query'] for a in j['attempts']]}; tags and folder clean")

        # 6c — Hardcover: off by default; when on, fills gaps, adds books, and is the fallback
        print("Scenario 6c: Hardcover as a second metadata source")
        r = c.get("/api/search", params={"q": "gap book"}).json()
        gap = r["results"][0]
        assert r["sources"] == 1 and gap["narrators"] == [] and gap["sources"] == ["audible"], (r["sources"], gap)
        ok("off by default: one source, Audible-only result")
        c.put("/api/settings", json={"metadata.hardcover_enabled": True, "metadata.hardcover_token": "Bearer hctoken"}).raise_for_status()
        t = c.post("/api/settings/test/metadata").json()
        assert t["ok"] and "Hardcover: OK" in t["message"], t
        r = c.get("/api/search", params={"q": "gap book"}).json()
        gap = r["results"][0]
        assert r["sources"] == 2 and gap["title"] == "Gap Book" and gap["sources"] == ["audible", "hardcover"], gap
        assert gap["narrators"] == ["Hal Cover"] and gap["series"][0]["name"] == "Gap Saga" and gap["series"][0]["sequence"] == "2", gap
        assert gap["description"] == "Description from Hardcover." and set(gap["filled_from"]) >= {"narrators", "series", "description"}
        ok("Audible result kept its own title; narrator, series and description filled from Hardcover")
        tiny = {x["asin"]: x for x in c.get("/api/search", params={"q": "tiny test"}).json()["results"]}
        assert tiny["B0TINY0001"]["title"] == "The Tiny Test: A Novel" and tiny["B0TINY0001"]["sources"] == ["audible", "hardcover"]
        assert tiny["B0TINY0001"]["narrators"] == ["Ray Reader"] and tiny["B0TINY0001"]["genres"][0] != "Fantasy"
        ok("matched by ASIN; Audible values win where both have data")
        indie = c.get("/api/search", params={"q": "indie tale"}).json()["results"]
        assert len(indie) == 1 and indie[0]["ref"] == "hardcover:103" and not indie[0]["asin"], indie
        jr = c.post("/api/jobs", json={"ref": indie[0]["ref"], "book": indie[0]})
        assert jr.status_code == 200, jr.text
        assert c.post("/api/jobs", json={"ref": "hardcover:103"}).status_code == 409, "duplicate by reference rejected"
        j = wait_status(c, jr.json()["id"], {"completed", "failed", "needs_review"}, 90)
        assert j["status"] == "completed", (j["detail"], [l["msg"] for l in j["log"]])
        assert Path(j["output_path"]).parts[-2:] == ("Ivy Indie", "Indie Tale"), j["output_path"]
        st = c.post("/api/search/status", json={"items": [{"ref": "hardcover:103", "title": "Indie Tale", "authors": []}]}).json()
        assert st["hardcover:103"]["job"], st
        ok("book only on Hardcover (no ASIN) found, requested by reference, downloaded and filed")
        httpx.post(f"http://127.0.0.1:{MOCK_PORT}/mock/config", json={"audible_down": True})
        r = c.get("/api/search", params={"q": "tiny test"}).json()
        assert r["fallback"] and r["sources"] == 1 and r["results"][0]["title"] == "The Tiny Test", r
        got = c.post("/api/jobs", json={"asin": "B0GAP00001"})
        assert got.status_code == 200 and got.json()["narrators"] == ["Hal Cover"], got.text
        c.post(f"/api/jobs/{got.json()['id']}/cancel"); c.delete(f"/api/jobs/{got.json()['id']}")
        ok("primary source down → search and ASIN lookups fall back to Hardcover")
        httpx.post(f"http://127.0.0.1:{MOCK_PORT}/mock/config", json={"audible_down": False})
        c.put("/api/settings", json={"metadata.hardcover_enabled": False}).raise_for_status()

        # 6d — size: lean copies preferred, large ones lose a little, never enough to beat a better match
        print("Scenario 6d: bitrate in scoring")
        sys.path.insert(0, str(ROOT))
        from app import matching as M
        bk = {"title": "Lean Book", "authors": [{"name": "Lee Lean"}], "narrators": [], "series": [], "runtime_min": 600}
        ctx = M.scoring_context(bk, ["m4b", "mp3"], 8)
        def resp(user, kbps, length=36000):
            return {"username": user, "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 2_000_000,
                    "files": [{"filename": f"@@{user}\\Lee Lean\\Lean Book\\Lean Book.mp3", "extension": "mp3",
                               "size": int(kbps * 1000 / 8 * length), "length": length}]}
        cands = {c["username"]: c for c in M.build_candidates([resp("lean", 64), resp("big", 180), resp("huge", 320),
                                                               resp("tiny", 24)], bk, ctx)}
        order = sorted(cands, key=lambda u: cands[u]["rank"], reverse=True)
        assert order == ["lean", "big", "huge", "tiny"], [(u, cands[u]["score"]) for u in order]
        assert cands["lean"]["kbps"] == 64 and any("large" in r for r in cands["big"]["reasons"])
        partial = M.build_candidates([resp("partial", 64, length=5400)], bk, ctx)[0]
        assert cands["huge"]["score"] > partial["score"], "a large complete copy still beats a lean partial one"
        ok(f"order {order}; scores " + ", ".join(f"{u} {cands[u]['score']}" for u in order) + f"; partial {partial['score']}")

        # 6e — optional re-encode to a single M4B
        print("Scenario 6e: re-encode to one M4B")
        c.put("/api/settings", json={"processing.reencode": True, "processing.reencode_kbps": 64}).raise_for_status()
        jid = request(c, search(c, "chapter book")[0])
        j = wait_status(c, jid, {"completed", "failed", "needs_review"}, 120)
        assert j["status"] == "completed", (j["detail"], [l["msg"] for l in j["log"]])
        out = Path(j["output_path"])
        audio = sorted(p.name for p in out.rglob("*") if p.suffix in (".mp3", ".m4b"))
        assert audio == ["Chapter Book.m4b"], audio
        m4b = out / "Chapter Book.m4b"
        probe = json.loads(subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_chapters",
                                           "-show_format", str(m4b)], capture_output=True, text=True).stdout)
        titles = [ch["tags"]["title"] for ch in probe["chapters"]]
        assert titles == ["01 - Chapter Book", "02 - Chapter Book", "03 - Chapter Book"], titles
        assert abs(float(probe["format"]["duration"]) - 45) < 2, probe["format"]["duration"]
        assert mutagen.File(str(m4b))["\xa9alb"] == ["Chapter Book"]
        assert any("Re-encoded:" in l["msg"] for l in j["log"]), [l["msg"] for l in j["log"]]
        ok(f"3 MP3s → one M4B with chapters {titles}, tagged, originals removed")
        c.put("/api/settings", json={"processing.reencode": False}).raise_for_status()

        # 6f — a file taken by another app: keep going, then re-download just that file
        print("Scenario 6f: file moved away by another app")
        jid = request(c, search(c, "vanish book")[0])
        seen_activity = False
        for _ in range(120):
            j = c.get(f"/api/jobs/{jid}").json()
            act = c.get("/api/activity").json()
            seen_activity = seen_activity or any(i["id"] == jid and i["kind"] == "downloading" for i in act["items"])
            if j["status"] in ("needs_review", "completed", "failed"):
                break
            time.sleep(1)
        assert j["status"] == "needs_review", (j["status"], j["detail"], [l["msg"] for l in j["log"]])
        lj = next(x for x in c.get("/api/jobs?scope=active").json() if x["id"] == jid)
        assert lj["files_missing"] == 1 and lj["files_done"] == 2, lj
        assert any("disappeared" in l["msg"] for l in j["log"])
        assert seen_activity, "download showed in the activity panel"
        ok("one file vanished: the other two were kept, job parked with 1 missing file")
        folders = c.get("/api/import/scan", params={"root": "incomplete"}).json()
        staged = next(f for f in folders if f["name"].endswith("Vanish Book"))
        assert staged["suggestion"] == "Val Vanish Vanish Book", staged["suggestion"]
        ok(f"Manual Import suggests “{staged['suggestion']}” for the staging folder (no job number)")
        c.post(f"/api/jobs/{jid}/redownload-missing").raise_for_status()
        j = wait_status(c, jid, {"completed", "failed", "needs_review"}, 90)
        assert j["status"] == "completed", (j["detail"], [l["msg"] for l in j["log"]])
        assert len(list(Path(j["output_path"]).glob("*.mp3"))) == 3
        ok("re-downloaded just the missing file; book completed with all 3 files")
        recent = c.get("/api/activity").json()["items"]
        assert any(i["id"] == jid and i["kind"] == "completed" for i in recent), recent
        ok("activity panel shows the finished book briefly")

        # 7 — Audiobookshelf
        print("Scenario 7: Audiobookshelf integration")
        abs_local = WORK / "abs-library"
        settings = {"abs.enabled": True, "abs.url": f"http://127.0.0.1:{MOCK_PORT}/abs", "abs.token": "abstoken",
                    "abs.library_id": "lib_books", "abs.folder_id": "fol_main", "abs.local_path": str(abs_local),
                    "abs.delete_after_import": True, "abs.metadata_mode": "fill"}
        c.put("/api/settings", json=settings).raise_for_status()
        t = c.post("/api/settings/test/abs").json()
        assert t["ok"], t
        ok(f"connection test: {t['message']}")

        res = {r["asin"]: r for r in search(c, "existing story") + search(c, "shelf classic") + search(c, "shelf bound")}
        assert res["B0EXIST001"]["in_abs"] and res["B0EXIST001"]["in_abs"]["link"].endswith("/item/li_exist"), res["B0EXIST001"]
        assert res["B0CLASS001"]["in_abs"], "title+author match for ABS item without ASIN"
        assert not res["B0SHELF001"]["in_abs"]
        ok("Search flags books already in Audiobookshelf (by ASIN, and by title+author when ABS has no ASIN)")

        jid = request(c, res["B0SHELF001"])
        j = wait_status(c, jid, {"completed", "failed", "needs_review"}, 90)
        assert j["status"] == "completed", (j["detail"], [l["msg"] for l in j["log"]])
        for _ in range(60):
            j = c.get(f"/api/jobs/{jid}").json()
            if j["abs_status"] != "pending":
                break
            time.sleep(1)
        assert j["abs_status"] == "imported", (j["abs_status"], j["abs_detail"], [l["msg"] for l in j["log"]])
        dest = abs_local / "Abe Shelver/Shelf Saga/Book 3 - Shelf Bound"
        assert (dest / "Shelf Bound.m4b").exists() and (dest / "cover.jpg").exists(), list(abs_local.rglob("*"))
        assert not (WORK / "downloads/completed/Abe Shelver").exists(), "completed copy should be gone (delete on)"
        item = httpx.get(f"http://127.0.0.1:{MOCK_PORT}/mock/abs/items").json()[j["abs_item_id"]]
        m = item["meta"]
        assert m["asin"] == "B0SHELF001" and m["narrators"] == ["Nia Reader"], m
        assert m["series"] == [{"name": "Shelf Saga", "sequence": "3"}] and m["genres"], m
        assert m["publisher"] == "Scanner Pub", "fill mode must keep Audiobookshelf's existing value"
        assert any("Kept Audiobookshelf's publisher" in l["msg"] for l in j["log"])
        assert [ch["title"] for ch in item["chapters"]] == ["Opening", "Middle", "End"], item["chapters"]
        lib = c.get("/api/library").json()
        assert not any(b.get("asin") == "B0SHELF001" for b in lib["narratarr"]), "removed from Narratarr library"
        shelf = next(b for b in lib["audiobookshelf"] if b["asin"] == "B0SHELF001")
        assert shelf["via_narratarr"] == 1
        ok("moved into Audiobookshelf folder (path-mapped), scanned, ASIN/narrator/series/genres filled, "
           "publisher conflict kept + logged, 3 chapters added, removed from Narratarr")

        # copy mode: send an existing Narratarr book
        c.put("/api/settings", json={"abs.delete_after_import": False}).raise_for_status()
        road = next(b for b in lib["narratarr"] if b["title"] == "Long Road Home")
        r = c.post(f"/api/library/{road['id']}/abs")
        r.raise_for_status()
        rj = r.json()["job_id"]
        for _ in range(60):
            j = c.get(f"/api/jobs/{rj}").json()
            if j["abs_status"] not in ("pending", None):
                break
            time.sleep(1)
        assert j["abs_status"] == "imported", (j["abs_detail"], [l["msg"] for l in j["log"]])
        assert (abs_local / "Sam Writer/Road Series/Book 2 - Long Road Home/CD2/2-02 Long Road Home.mp3").exists()
        assert Path(road["path"]).exists(), "copy mode keeps Narratarr's copy"
        lib = c.get("/api/library").json()
        road2 = next(b for b in lib["narratarr"] if b["id"] == road["id"])
        assert road2["abs_status"] == "imported" and road2["abs_link"]
        assert not any(b["title"] == "Long Road Home" for b in lib["audiobookshelf"]), "linked items aren't listed twice"
        ok("copy mode: CD1/CD2 copied, Narratarr keeps its copy and links to the Audiobookshelf item")

        # pending + retry: folder not mounted, then fixed
        c.put("/api/settings", json={"abs.local_path": str(WORK / "not-mounted")}).raise_for_status()
        imp = next(b for b in lib["narratarr"] if b["title"] == "Imported Tales")
        pj = c.post(f"/api/library/{imp['id']}/abs").json()["job_id"]
        for _ in range(20):
            j = c.get(f"/api/jobs/{pj}").json()
            if j.get("abs_detail") and "mounted" in j["abs_detail"]:
                break
            time.sleep(0.5)
        assert j["abs_status"] == "pending" and "mounted" in j["abs_detail"], j["abs_detail"]
        assert any(x["id"] == pj for x in c.get("/api/jobs?scope=active").json()), "pending ABS jobs stay visible"
        c.put("/api/settings", json={"abs.local_path": str(abs_local)}).raise_for_status()
        c.post("/api/abs/sync").raise_for_status()
        for _ in range(60):
            j = c.get(f"/api/jobs/{pj}").json()
            if j["abs_status"] == "imported":
                break
            time.sleep(1)
        assert j["abs_status"] == "imported", j["abs_detail"]
        ok("unmounted folder → pending with a clear reason, retried automatically after the fix")

        # watcher off → falls back to a full library scan
        httpx.post(f"http://127.0.0.1:{MOCK_PORT}/mock/abs", json={"watcher": False})
        tiny = next(b for b in lib["narratarr"] if b["asin"] == "B0TINY0001")
        wj = c.post(f"/api/library/{tiny['id']}/abs").json()["job_id"]
        for _ in range(120):
            j = c.get(f"/api/jobs/{wj}").json()
            if j["abs_status"] == "imported":
                break
            time.sleep(1)
        assert j["abs_status"] == "imported" and any("library scan" in l["msg"] for l in j["log"]), j["abs_detail"]
        ok("folder watcher off → fell back to a library scan and still matched the item")
        assert c.get(f"/api/abs/items/li_exist/cover").status_code == 200
        ok("Audiobookshelf covers proxied")
        httpx.post(f"http://127.0.0.1:{MOCK_PORT}/mock/abs", json={"watcher": True})

        # 7b — ebooks, filed into a separate Audiobookshelf library
        print("Scenario 7b: Ebooks")
        ebook_local = WORK / "abs-ebooks"
        c.put("/api/settings", json={"abs.ebook_library_id": "lib_ebooks", "abs.ebook_folder_id": "fol_eb",
                                     "abs.ebook_local_path": str(ebook_local),
                                     "abs.delete_after_import": True}).raise_for_status()
        t = c.post("/api/settings/test/abs").json()
        assert t["ok"] and "ebooks go to /ebooks" in t["message"], t
        audio_hits = search(c, "paper lantern")
        assert len(audio_hits) == 2, "audiobook search keeps both narrations"
        eb = c.get("/api/search", params={"q": "paper lantern", "media": "ebook"}).json()
        assert eb["media"] == "ebook" and len(eb["results"]) == 1, [r["asin"] for r in eb["results"]]
        lantern = eb["results"][0]
        assert lantern["narrators"] == [] and lantern["runtime_min"] is None and lantern["media"] == "ebook"
        ok("ebook search merges the two narrations into one result without narrator or runtime")

        r = c.post("/api/jobs", json={"asin": lantern["asin"], "ref": lantern["ref"], "region": lantern["region"],
                                      "book": lantern, "media": "ebook"})
        r.raise_for_status()
        ej = r.json()["id"]
        assert r.json()["media"] == "ebook"
        assert c.post("/api/jobs", json={"ref": lantern["ref"], "book": lantern, "media": "ebook"}).status_code == 409
        aj = c.post("/api/jobs", json={"asin": lantern["asin"], "ref": lantern["ref"], "region": lantern["region"],
                                       "book": audio_hits[0]})
        assert aj.status_code == 200, "an audiobook request isn't blocked by the same title's ebook job"
        c.post(f"/api/jobs/{aj.json()['id']}/cancel").raise_for_status()
        j = wait_status(c, ej, {"completed", "failed", "needs_review"}, 90)
        assert j["status"] == "completed", (j["detail"], [l["msg"] for l in j["log"]])
        cands = j["candidates"]
        assert cands[0]["formats"] == ["epub"] and cands[0]["display_dir"].endswith("Paper Lantern.epub"), cands[0]
        assert all(c_["formats"][0] in ("epub", "pdf") for c_ in cands), "audio files never offered for an ebook"
        other = next(c_ for c_ in cands if "Returns" in c_["display_dir"])
        assert other["score"] < cands[0]["score"] and any("≠ #1" in x for x in other["reasons"]), other
        pdf = next(c_ for c_ in cands if c_["formats"] == ["pdf"])
        assert pdf["score"] < cands[0]["score"]
        ok(f"epub picked (score {cands[0]['score']}) over the faster pdf ({pdf['score']}) "
           f"and the series sequel in the same folder ({other['score']})")
        for _ in range(60):
            j = c.get(f"/api/jobs/{ej}").json()
            if j["abs_status"] != "pending":
                break
            time.sleep(1)
        assert j["abs_status"] == "imported", (j["abs_detail"], [l["msg"] for l in j["log"]])
        edest = ebook_local / "Ella Page/Lantern Tales/Book 1 - Paper Lantern"
        assert (edest / "Paper Lantern.epub").exists() and (edest / "cover.jpg").exists(), list(ebook_local.rglob("*"))
        assert (edest / "metadata.opf").exists() and not (edest / "reader.txt").exists()
        assert not any(abs_local.rglob("*.epub")), "nothing went into the audiobook library"
        item = httpx.get(f"http://127.0.0.1:{MOCK_PORT}/mock/abs/items").json()[j["abs_item_id"]]
        assert item["lib"] == "lib_ebooks" and item["ebooks"] == ["Paper Lantern.epub"], item
        assert item["meta"]["narrators"] == [] and item["meta"]["series"] == [{"name": "Lantern Tales", "sequence": "1"}]
        ok("epub filed into the ebook library folder with cover and OPF, scanned, metadata filled, no narrator")
        eb2 = c.get("/api/search", params={"q": "paper lantern", "media": "ebook"}).json()["results"][0]
        assert eb2["in_abs"], "ebook search sees the ebook library"
        assert not any(r_["in_abs"] for r_ in search(c, "paper lantern")), "audiobook search doesn't"
        lib = c.get("/api/library").json()
        assert any(b["media"] == "ebook" and b["title"] == "Paper Lantern" for b in lib["audiobookshelf"])
        ok("Search and Library tell the ebook library and the audiobook library apart")

        # 8 — on-demand re-encode of a Narratarr library item and an Audiobookshelf item
        print("Scenario 8: Re-encode button")
        lib = c.get("/api/library").json()
        road = next(b for b in lib["narratarr"] if b["title"] == "Long Road Home")
        r = c.post(f"/api/library/{road['id']}/reencode"); r.raise_for_status()
        j = wait_status(c, r.json()["job_id"], {"completed", "failed"}, 120)
        assert j["status"] == "completed" and "Re-encoded" in j["detail"], (j["detail"], [l["msg"] for l in j["log"]])
        audio = sorted(p.name for p in Path(road["path"]).rglob("*") if p.suffix in (".mp3", ".m4b"))
        assert audio == ["Long Road Home.m4b"], audio
        assert c.get(f"/api/library/{road['id']}").json()["file_count"] == 1
        assert any("at 32 kbps" in l["msg"] for l in j["log"]), "lean (32 kbps) source isn't re-encoded at a higher bitrate"
        ok(f"Narratarr library item: 4 MP3s → {audio[0]} ({j['detail']})")
        abs_id = c.get(f"/api/jobs/{rj}").json()["abs_item_id"]
        r = c.post(f"/api/abs/items/{abs_id}/reencode"); r.raise_for_status()
        j = wait_status(c, r.json()["job_id"], {"completed", "failed"}, 120)
        assert j["status"] == "completed", (j["detail"], [l["msg"] for l in j["log"]])
        abs_dir = abs_local / "Sam Writer/Road Series/Book 2 - Long Road Home"
        audio = sorted(p.name for p in abs_dir.rglob("*") if p.suffix in (".mp3", ".m4b"))
        assert audio == ["Long Road Home.m4b"], audio
        mock_abs = httpx.get(f"http://127.0.0.1:{MOCK_PORT}/mock/abs/items").json()[abs_id]
        assert mock_abs["audio"] == ["Long Road Home.m4b"], mock_abs["audio"]
        t = mutagen.File(str(abs_dir / "Long Road Home.m4b"))
        assert t["\xa9alb"] == ["Long Road Home"] and t["\xa9wrt"] == ["Kim Narrator"], dict(t.tags or {}).keys()
        ok("Audiobookshelf item re-encoded in place, tagged from its Audiobookshelf metadata, item rescanned")
        # file by file: names and folders kept, lean files left alone, FLAC → .m4a
        lib = c.get("/api/library").json()
        other = next(b for b in lib["narratarr"] if b["title"] != "Long Road Home" and Path(b["path"]).is_dir())
        folder = Path(other["path"])
        for p in folder.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".mp3", ".m4b", ".m4a", ".flac"):
                p.unlink()
        (folder / "CD2").mkdir(exist_ok=True)
        def tone(path, *codec, src="sine=frequency=400:duration=20"):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, *codec, str(path)], check=True)
        tone(folder / "00 Intro.mp3", "-c:a", "libmp3lame", "-b:a", "32k")
        tone(folder / "01 Part.mp3", "-c:a", "libmp3lame", "-b:a", "192k")
        tone(folder / "02 Part.mp3", "-c:a", "libmp3lame", "-b:a", "192k")
        tone(folder / "CD2" / "03 Part.flac", "-c:a", "flac", src="anoisesrc=duration=20:amplitude=0.3")  # FLAC of a sine is tiny
        assert c.post(f"/api/library/{other['id']}/reencode", json={"merge": False, "kbps": 9999}).status_code == 400
        r = c.post(f"/api/library/{other['id']}/reencode", json={"merge": False, "kbps": 96}); r.raise_for_status()
        j = wait_status(c, r.json()["job_id"], {"completed", "failed"}, 120)
        assert j["status"] == "completed", (j["detail"], [l["msg"] for l in j["log"]])
        audio = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")
                       if p.suffix in (".mp3", ".m4a", ".flac", ".m4b"))
        assert audio == ["00 Intro.mp3", "01 Part.mp3", "02 Part.mp3", "CD2/03 Part.m4a"], audio
        assert round(mutagen.File(str(folder / "00 Intro.mp3")).info.bitrate / 1000) == 32, "lean file was re-encoded"
        rate = mutagen.File(str(folder / "01 Part.mp3")).info.bitrate / 1000
        assert 88 <= rate <= 104, rate
        assert c.get(f"/api/library/{other['id']}").json()["file_count"] == 4
        assert not list(folder.rglob(".narratarr-tmp-*"))
        ok(f"file by file: {audio} kept their names, lean intro untouched, 192 kbps → {rate:.0f} kbps as chosen ({j['detail']})")
        opt = next(x for x in c.get("/api/settings").json() if x["key"] == "processing.reencode_kbps")
        assert opt["options"] == [64, 96, 128] and "dramatised" in opt["labels"]["128"], opt
        ok("bitrate offered as a choice of 64 / 96 / 128 kbps in Settings and the Re-encode dialog")
        assert c.post(f"/api/library/{other['id']}/reencode", json={"merge": False, "kbps": 96}).status_code == 409
        assert c.post("/api/abs/items/li_exist/reencode").status_code == 409
        ok("single file already at a low bitrate: refused with 'nothing to gain'")

        # 9 — health light, update check, first-run guide
        print("Scenario 9: health, updates, welcome guide")
        hs = c.get("/api/system/health").json()
        assert hs["version"] == "0.1.0" and hs["welcome_done"] is False, hs
        assert hs["health"]["level"] == "ok", hs["health"]
        c.post("/api/system/update/check").raise_for_status()  # the startup check ran before the mock was up
        hs = c.get("/api/system/health").json()
        assert hs["update"]["available"] and hs["update"]["latest"]["version"] == "0.2.0", hs["update"]
        assert "What's Changed" in hs["update"]["latest"]["notes"]
        ok("v0.1.0 sees v0.2.0 on GitHub with its release notes; health light green")
        good_url = next(x for x in c.get("/api/settings").json() if x["key"] == "slskd.url")["value"]
        c.put("/api/settings", json={"slskd.url": "http://127.0.0.1:9"}).raise_for_status()
        hs = c.get("/api/system/health").json()
        assert hs["health"]["level"] == "bad" and any("slskd" in i["text"] for i in hs["health"]["issues"]), hs["health"]
        c.put("/api/settings", json={"slskd.url": good_url}).raise_for_status()
        assert c.get("/api/system/health").json()["health"]["level"] == "ok"
        ok("slskd down → red with the reason; back to green once fixed")
        c.put("/api/settings", json={"general.welcome_done": True, "general.check_updates": False}).raise_for_status()
        hs = c.get("/api/system/health").json()
        assert hs["welcome_done"] is True and not hs["update"]["enabled"] and not hs["update"]["available"], hs
        hidden = next(x for x in c.get("/api/settings").json() if x["key"] == "general.welcome_done")
        assert hidden["type"] == "hidden" and hidden["value"] is True
        ok("welcome guide remembered as done; turning update checks off hides the notice")
    except AssertionError as e:
        failures += 1
        print("FAIL:", e)
    finally:
        app.terminate()
        mock.terminate()
    print("\nApp log tail:\n" + "".join((WORK / "app.log").read_text(encoding="utf-8", errors="replace").splitlines(True)[-15:]) if failures else "\nAll scenarios passed.")
    print(f"workdir: {WORK}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
