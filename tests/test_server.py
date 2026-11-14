import importlib.util, sys, time, json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

@pytest.fixture()
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("YT_JOBS_DB", str(tmp_path / "jobs.db"))
    spec = importlib.util.spec_from_file_location("server", ROOT / "server.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["server"] = m
    spec.loader.exec_module(m)
    m.app.config["TESTING"] = True
    return m

def client(server):
    return server.app.test_client()

def test_sanitize(server):
    assert server.sanitize('a/b:c*?"<>|') == "abc"
    assert server.sanitize("   ") == "untitled"

def test_to_ytdlp_date(server):
    assert server.to_ytdlp_date("2026-01-05") == "20260105"
    assert server.to_ytdlp_date("20260105") == "20260105"
    assert server.to_ytdlp_date("today") == "today"
    assert server.to_ytdlp_date("") == ""

def test_best_thumb(server):
    t = [{"url": "small"}, {"url": "big"}]
    assert server.best_thumb({"thumbnails": t}) == "big"
    assert server.best_thumb({"thumbnail": "x"}) == "x"
    assert server.best_thumb({}) == ""

def test_settings_roundtrip(server):
    c = client(server)
    assert c.get("/api/settings").status_code == 200
    r = c.post("/api/settings", json={"speed_limit_mbps": 7, "sponsorblock": True, "bogus": 1})
    assert r.status_code == 200
    d = r.get_json()
    assert d["speed_limit_mbps"] == 7 and d["sponsorblock"] is True and "bogus" not in d

def test_start_requires_url(server):
    c = client(server)
    r = c.post("/api/start", json={})
    assert r.status_code == 400

def test_start_blocks_internal_urls(server, monkeypatch):
    called = []
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("192.168.1.10", 0))])
    c = client(server)
    r = c.post("/api/start", json={"url": "http://192.168.1.10:8080/x"})
    assert r.status_code == 400
    r = c.post("/api/start", json={"url": "http://localhost:8080/x"})
    assert r.status_code == 400
    assert not called

def test_info_blocks_internal(server, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))])
    c = client(server)
    r = c.get("/api/info", query_string={"url": "http://localhost:8000/"})
    assert r.status_code == 400 and "Blocked" in r.get_json()["error"]

def test_hyperquality_and_ratelimit_opts(server):
    c = client(server)
    c.post("/api/settings", json={"speed_limit_mbps": 2, "subtitles_lang": "en", "sponsorblock": True, "cookies_browser": "firefox"})
    server.settings_lock  # ensure visible
    cfg = server.get_cfg()
    assert cfg["speed_limit_mbps"] == 2

def test_hook_speed_math(server):
    server.jobs["jt"] = {"videos": [{"id": "v1", "status": "queued", "percent": 0,
        "speed": "", "eta": "", "downloaded_bytes": 0, "total_bytes": 0, "error": None}], "log": []}
    h = server.make_video_hook("jt", "v1")
    time.sleep(0.55)
    h({"status": "downloading", "downloaded_bytes": 2_000_000, "total_bytes": 10_000_000})
    vid = server.jobs["jt"]["videos"][0]
    assert vid["percent"] == 20.0 and "iB/s" in vid["speed"]
    time.sleep(0.55)
    h({"status": "downloading", "downloaded_bytes": 4_000_000, "total_bytes": 10_000_000})
    assert "iB/s" in server.jobs["jt"]["videos"][0]["speed"]
    h({"status": "finished", "downloaded_bytes": 10_000_000, "total_bytes": 10_000_000})
    assert server.jobs["jt"]["videos"][0]["status"] == "processing"

def test_db_persistence(server):
    server._init_db()
    server._save_job_summary({"job_id": "j1", "ts": int(time.time()*1000), "status": "done",
                              "mode": "single", "total": 3, "completed": 3, "failed": 0, "error": None})
    c = client(server)
    r = c.get("/api/status/all")
    assert r.status_code == 200
    assert r.get_json()["j1"]["total_videos"] == 3

def test_version_endpoint(server):
    c = client(server)
    r = c.get("/api/version")
    assert r.status_code == 200
    d = r.get_json()
    assert "installed" in d and "stale" in d


# ── Library / history ────────────────────────────────────────────────────────
def _seed(server, job_id="j-hist", status="done", url="https://example.com/v"):
    server._init_db()
    server._upsert_job({
        "id": job_id, "ts": int(time.time() * 1000), "status": status, "mode": "single",
        "total_videos": 2, "completed_videos": 1, "failed_videos": 1, "error": None,
        "url": url, "title": "A test capture", "quality": "1080p", "section": "videos",
        "date_after": "", "date_before": "", "out_dir": "/tmp/ytdpro-test",
        "subtitles": "en",
    })
    server._upsert_videos(job_id, [
        {"id": "v1", "index": 1, "title": "First", "url": url, "thumbnail": "http://img/1.jpg",
         "duration": 60, "status": "done", "filepath": "/tmp/ytdpro-test/First.mp4",
         "filesize": 1000, "error": None},
        {"id": "v2", "index": 2, "title": "Second", "url": url, "thumbnail": "http://img/2.jpg",
         "duration": 30, "status": "error", "filepath": None, "filesize": 0,
         "error": "boom"},
    ])


def test_history_lists_and_searches(server):
    _seed(server)
    c = client(server)
    d = c.get("/api/history").get_json()
    assert d["total"] == 1
    item = d["items"][0]
    assert item["title"] == "A test capture"
    assert item["size_bytes"] == 1000
    assert item["completed_videos"] == 1 and item["failed_videos"] == 1
    assert item["thumbnail"] == "http://img/1.jpg"
    assert len(item["preview"]) == 2

    assert c.get("/api/history", query_string={"q": "capture"}).get_json()["total"] == 1
    assert c.get("/api/history", query_string={"q": "nothing-here"}).get_json()["total"] == 0
    assert c.get("/api/history", query_string={"status": "done"}).get_json()["total"] == 1
    assert c.get("/api/history", query_string={"status": "error"}).get_json()["total"] == 0


def test_history_detail_and_delete(server):
    _seed(server)
    c = client(server)
    d = c.get("/api/history/j-hist").get_json()
    assert d["id"] == "j-hist" and len(d["videos"]) == 2
    assert c.delete("/api/history/j-hist").status_code == 200
    assert c.get("/api/history/j-hist").status_code == 404
    assert c.delete("/api/history/j-hist").status_code == 404


def test_history_clear(server):
    _seed(server, "j1")
    _seed(server, "j2", status="error")
    c = client(server)
    r = c.delete("/api/history", query_string={"status": "error"})
    assert r.get_json()["removed"] == 1
    assert c.get("/api/history").get_json()["total"] == 1


def test_stats(server):
    _seed(server)
    c = client(server)
    d = c.get("/api/stats").get_json()
    assert d["total_jobs"] == 1
    assert d["total_videos"] == 2
    assert d["done"] == 1 and d["failed"] == 1
    assert d["total_bytes"] == 1000
    assert len(d["activity"]) == 14


def test_open_rejects_outside_download_dir(server, tmp_path, monkeypatch):
    monkeypatch.setitem(server.settings, "download_dir", str(tmp_path))
    (tmp_path / "ok").mkdir()
    c = client(server)
    r = c.post("/api/open", json={"path": "/etc/passwd"})
    assert r.status_code == 403
    r = c.post("/api/open", json={"path": str(tmp_path / "missing")})
    assert r.status_code == 404


def test_relaunch_reuses_stored_settings(server, monkeypatch):
    _seed(server)
    calls = {}
    monkeypatch.setattr(server, "_is_public_url", lambda u: (True, ""))
    monkeypatch.setattr(server, "_spawn_job",
                        lambda url, mode, quality, section, a="", b="", subtitles="": calls.update(
                            url=url, mode=mode, quality=quality, section=section,
                            subtitles=subtitles) or "new-job")
    c = client(server)
    r = c.post("/api/history/j-hist/relaunch")
    assert r.status_code == 200 and r.get_json()["jobId"] == "new-job"
    assert calls == {"url": "https://example.com/v", "mode": "single",
                     "quality": "1080p", "section": "videos", "subtitles": "en"}
    assert server._history_job("j-hist")["status"] == "superseded"
    assert client(server).get("/api/history").get_json()["total"] == 0


def test_retry_failed_creates_job(server, monkeypatch, tmp_path):
    _seed(server)
    monkeypatch.setitem(server.settings, "download_dir", str(tmp_path))
    monkeypatch.setattr(server, "run_pool", lambda *a, **k: None)
    c = client(server)
    r = c.post("/api/history/j-hist/retry")
    assert r.status_code == 200
    jid = r.get_json()["jobId"]
    assert jid in server.jobs
    assert server.jobs[jid]["total_videos"] == 1
    assert server.jobs[jid]["videos"][0]["status"] == "queued"
    assert server.jobs[jid]["mode"] == "retry"
    assert server.jobs[jid]["origin_job_id"] == "j-hist"
    # The temporary retry must not become its own library entry.
    assert client(server).get("/api/history").get_json()["total"] == 1


def test_retry_failed_none(server, monkeypatch):
    server._init_db()
    server._upsert_job({"id": "j-none", "ts": int(time.time() * 1000), "status": "done",
                        "mode": "single", "total_videos": 1, "completed_videos": 1,
                        "failed_videos": 0, "error": None, "url": "https://example.com",
                        "title": "All good", "quality": "best", "section": "videos"})
    server._upsert_videos("j-none", [{"id": "v1", "index": 1, "title": "First", "url": "https://example.com",
                                      "thumbnail": "", "duration": 0, "status": "done",
                                      "filepath": "f", "filesize": 1, "error": None}])
    monkeypatch.setattr(server, "run_pool", lambda *a, **k: None)
    c = client(server)
    assert c.post("/api/history/j-none/retry").status_code == 400


def test_retention_zero_keeps_everything(server):
    _seed(server)
    server.settings["history_retention_days"] = 0
    server._purge_old_jobs_db()
    assert client(server).get("/api/history").get_json()["total"] == 1


def test_version_endpoint_shape(server):
    d = client(server).get("/api/version").get_json()
    assert set(d) == {"installed", "latest", "stale"}


def test_playlist_outtmpl_uses_video_index(server):
    tpl = "/x/%(playlist_index)02d - %(title)s.%(ext)s"
    assert server._video_outtmpl(tpl, 7) == "/x/07 - %(title)s.%(ext)s"
    assert server._video_outtmpl("%(title)s.%(ext)s", 3) == "%(title)s.%(ext)s"


def test_clean_error_strips_ansi(server):
    assert server._clean_error("\x1b[0;31mERROR:\x1b[0m boom") == "ERROR: boom"
    assert server._clean_error("[0;31mERROR: [0m nope") == "ERROR: nope"


def test_merge_retry_results_updates_original(server):
    _seed(server)
    retried = {"id": "v2", "index": 2, "title": "Second", "url": "https://example.com/v",
               "thumbnail": "http://img/2.jpg", "duration": 30, "status": "done",
               "filepath": "/tmp/ytdpro-test/Second.mp4", "filesize": 2000, "error": None}
    server._merge_retry_results("j-hist", "retry-temp", [retried])
    d = client(server).get("/api/history/j-hist").get_json()
    assert d["completed"] == 2 and d["failed"] == 0
    assert d["status"] == "done" and d["videos"][1]["status"] == "done"
    assert d["videos"][1]["filesize"] == 2000
    assert client(server).get("/api/history").get_json()["total"] == 1


def test_normalize_subs(server):
    assert server.normalize_subs("") == ""
    assert server.normalize_subs("all") == "all"
    assert server.normalize_subs("en, fr,EN") == "en,fr"
    with pytest.raises(ValueError):
        server.normalize_subs("en;rm")


def test_base_opts_subtitles(server):
    opts, _ = server._base_opts(server.get_cfg(), "best", Path("/tmp/x"), "single",
                                subs="fr,en")
    assert opts["writesubtitles"] is True
    assert opts["writeautomaticsub"] is True
    assert opts["subtitleslangs"] == ["fr", "en"]
    assert opts["subtitlesformat"] == "vtt/srt/best"
    opts2, _ = server._base_opts(server.get_cfg(), "best", Path("/tmp/x"), "single", subs="")
    assert "writesubtitles" not in opts2


def test_start_passes_subtitles(server, monkeypatch):
    monkeypatch.setattr(server, "_is_public_url", lambda u: (True, ""))
    captured = {}
    monkeypatch.setattr(server, "_spawn_job",
                        lambda url, mode, quality, section, a="", b="", subtitles="": captured.update(
                            subtitles=subtitles) or "jid")
    c = client(server)
    r = c.post("/api/start", json={"url": "https://example.com/v", "subtitles": "fr, en"})
    assert r.status_code == 200 and captured["subtitles"] == "fr,en"
    r = c.post("/api/start", json={"url": "https://example.com/v", "subtitles": "en;rm"})
    assert r.status_code == 400


def test_subtitle_error_detection(server):
    assert server._is_subtitle_error("Unable to download video subtitles for 'ar': HTTP Error 429")
    assert not server._is_subtitle_error("HTTP Error 403: Forbidden")
    assert not server._is_subtitle_error("video unavailable")


def test_tv_quality_preset(server):
    assert "tv" in server.QUALITY_MAP
    assert server.QUALITY_MAP["tv"][1] == "mp4"
    opts, ext = server._base_opts(server.get_cfg(), "tv", Path("/tmp/x"), "single")
    assert ext == "mp4"
    assert "avc1" in opts["format"] and "mp4a" in opts["format"]


def test_subtitle_patterns(server):
    assert server._subtitle_patterns("ar") == ["ar", "ar.*"]
    assert server._subtitle_patterns("en,fr") == ["en", "en.*", "fr", "fr.*"]
    assert server._subtitle_patterns("all") == ["all"]


def test_fetch_subtitles_endpoint(server, monkeypatch):
    _seed(server)
    captured = {}
    monkeypatch.setattr(server, "_spawn_subtitle_job",
                        lambda job_id, row, langs: captured.update(
                            job_id=job_id, langs=langs) or "subs-job")
    c = client(server)
    r = c.post("/api/history/j-hist/subtitles", json={"langs": "ar, en"})
    assert r.status_code == 200
    assert r.get_json()["jobId"] == "subs-job"
    assert captured["langs"] == "ar,en"
    assert c.post("/api/history/j-hist/subtitles", json={"langs": ""}).status_code == 400
    assert c.post("/api/history/j-hist/subtitles", json={"langs": "bad;code"}).status_code == 400


def test_run_subtitle_job_marks_done(server, monkeypatch, tmp_path):
    _seed(server)
    src = tmp_path / "First.mp4"
    src.write_bytes(b"x")
    stub = tmp_path / "First.en.srt"
    stub.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
    monkeypatch.setattr(server, "_download_subtitles_for_video",
                        lambda video, langs, cfg: [stub])
    jid = "subs-run"
    server.jobs[jid] = {
        "id": jid, "ts": int(time.time() * 1000), "status": "running", "log": [],
        "done": False, "error": None, "mode": "subs", "origin_job_id": "j-hist",
        "total_videos": 1, "completed_videos": 0, "failed_videos": 0,
        "active_slots": 0, "max_concurrent": 1, "base_opts": {}, "title": "Subtitles",
        "videos": [{"id": "v1", "index": 1, "title": "First", "url": "https://example.com/v",
                    "status": "queued", "percent": 0, "filepath": str(src),
                    "thumbnail": "", "duration": 1, "error": None}],
    }
    server.run_subtitle_job(jid, "en")
    assert server.jobs[jid]["done"] is True
    assert server.jobs[jid]["status"] == "done"
    assert server.jobs[jid]["videos"][0]["status"] == "done"


def test_confirm_with_selected_videos(server, monkeypatch):
    jid = "pick"
    server.jobs[jid] = {
        "id": jid, "ts": int(time.time() * 1000), "status": "ready", "log": [],
        "done": False, "error": None, "mode": "playlist", "origin_job_id": None,
        "total_videos": 3, "completed_videos": 0, "failed_videos": 0,
        "active_slots": 0, "max_concurrent": 2, "base_opts": {}, "title": "Pick",
        "url": "https://example.com", "quality": "best", "section": "videos",
        "date_after": "", "date_before": "", "out_dir": "/tmp",
        "videos": [
            {"id": "a", "index": 1, "title": "A", "url": "u", "status": "queued"},
            {"id": "b", "index": 2, "title": "B", "url": "u", "status": "queued"},
            {"id": "c", "index": 3, "title": "C", "url": "u", "status": "queued"},
        ],
    }
    c = client(server)
    r = c.post("/api/confirm/pick", json={"videoIds": ["a", "c"]})
    assert r.status_code == 200
    job = server.jobs[jid]
    assert job["status"] == "running" and job["total_videos"] == 2
    assert [v["status"] for v in job["videos"]] == ["queued", "skipped", "queued"]
    assert job["selected_videos"] == ["a", "c"]
    assert c.post("/api/confirm/pick", json={"videoIds": []}).status_code == 400


def test_skipped_video_is_not_downloaded(server):
    called = []
    v = {"id": "x", "status": "skipped", "title": "X", "index": 1, "url": "u"}
    server.jobs["skipjob"] = {"videos": [v], "log": []}
    assert server.download_one("skipjob", v, {}) is False


def test_recover_interrupted_jobs(server):
    _seed(server, "was-running", status="running")
    server._recover_interrupted_jobs()
    row = server._history_job("was-running")
    assert row["status"] == "interrupted"
    assert "resume" in (row["error"] or "").lower()
