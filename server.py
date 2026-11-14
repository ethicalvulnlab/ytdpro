#!/usr/bin/env python3
"""
ytdpro — a local, private web UI for yt-dlp.

Run:  python server.py  →  http://localhost:8080
"""

import copy
import json
import logging
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

import ipaddress
import yt_dlp
from flask import Flask, Response, jsonify, render_template, request

log = logging.getLogger("ytdpro")

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True  # UI edits show up on refresh

# ── Settings ──────────────────────────────────────────────────────────────────
SETTINGS_FILE = Path(os.environ.get("YT_SETTINGS_FILE",
                     Path.home() / ".ytdpro_settings.json"))


def _default_download_dir() -> str:
    return os.environ.get("YT_DOWNLOAD_DIR") or str(Path.home() / "Downloads" / "ytdpro")


DEFAULT_SETTINGS = {
    "download_dir":       _default_download_dir(),
    "max_concurrent":     3,
    "default_quality":    "best",
    "socket_timeout":     30,
    "sleep_interval":     1,
    "max_sleep_interval": 3,
    "speed_limit_mbps":   0,
    "cookies_browser":    "",
    "subtitles_lang":     "",
    "sponsorblock":       False,
    "history_retention_days": 90,
}


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text())}
        except Exception as exc:
            log.warning("Could not load settings from %s: %s", SETTINGS_FILE, exc)
    return dict(DEFAULT_SETTINGS)


def save_settings(s: dict):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


settings      = load_settings()
settings_lock = threading.Lock()


def get_cfg() -> dict:
    with settings_lock:
        return dict(settings)


# ── Quality map ───────────────────────────────────────────────────────────────
QUALITY_MAP = {
    "best":  ("bv*+ba/best",                                          "mp4"),
    "1080p": ("bestvideo[height<=1080]+bestaudio/best[height<=1080]", "mp4"),
    "720p":  ("bestvideo[height<=720]+bestaudio/best[height<=720]",   "mp4"),
    "480p":  ("bestvideo[height<=480]+bestaudio/best[height<=480]",   "mp4"),
    "360p":  ("bestvideo[height<=360]+bestaudio/best[height<=360]",   "mp4"),
    "audio": ("bestaudio/best",                                       "mp3"),
}

MODE_LABELS = {"single": "Single video", "playlist": "Playlist",
               "channel": "Channel", "retry": "Retry"}


_SUBS_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def normalize_subs(value) -> str:
    """Normalize a subtitle selection: '', 'all', or 'en,fr,ar'."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.lower() == "all":
        return "all"
    codes, seen = [], set()
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        if len(part) > 24 or not _SUBS_CODE_RE.match(part):
            raise ValueError(f"Invalid subtitle language code: {part}")
        low = part.lower()
        if low not in seen:
            seen.add(low)
            codes.append(low)
    return ",".join(codes)


def sanitize(name: str) -> str:
    return re.sub(r'[/:*?"<>|\\]+', '', str(name or '')).strip() or "untitled"


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def to_ytdlp_date(date_str: str) -> str:
    """Convert YYYY-MM-DD (HTML date input) to YYYYMMDD (yt-dlp DateRange format)."""
    if not date_str:
        return ""
    s = date_str.strip()
    if re.fullmatch(r"\d{8}", s) or s in ("today", "now", "yesterday"):
        return s
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s.replace("-", "")
    log.warning("Unrecognised date format: %s", s)
    return s


BLOCKED_HOSTS = re.compile(
    r"^(localhost|.*\.local|.*\.localhost|.*\.internal|.*\.lan|.*\.home|.*\.arpa)$", re.I)


def _is_public_url(url: str) -> tuple[bool, str]:
    try:
        u = urlparse(url)
    except Exception:
        return False, "Malformed URL"
    if u.scheme not in ("http", "https") and url:
        return False, "Only http/https URLs allowed"
    host = u.hostname or ""
    if not host or BLOCKED_HOSTS.fullmatch(host):
        return False, "Blocklisted host"
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False, f"Cannot resolve host: {host}"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            return False, "Bad address returned by DNS"
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            return False, f"{host} resolves to a non-public address"
    return True, ""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\[\d+(?:;\d+)*m")


def _clean_error(value) -> str:
    """Strip terminal colour codes from yt-dlp errors before storing them."""
    text = _ANSI_RE.sub("", str(value or ""))
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def best_thumb(entry: dict) -> str:
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    for t in reversed(entry.get("thumbnails") or []):
        if t.get("url"):
            return t["url"]
    return ""


# ── Persistent library (SQLite) ───────────────────────────────────────────────
JOBS_DB = Path(os.environ.get("YT_JOBS_DB", Path(SETTINGS_FILE).parent / ".ytdpro_jobs.db"))
_db_ready = False
_db_lock = threading.Lock()

_JOB_COLUMNS = {
    "updated_ts": "INTEGER",
    "url": "TEXT",
    "title": "TEXT",
    "quality": "TEXT",
    "section": "TEXT",
    "date_after": "TEXT",
    "date_before": "TEXT",
    "out_dir": "TEXT",
    "subtitles": "TEXT",
}


def _db():
    global _db_ready
    if not _db_ready:
        with _db_lock:
            if not _db_ready:
                JOBS_DB.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(JOBS_DB) as db:
                    db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY, ts INTEGER, status TEXT, mode TEXT,
                        total INTEGER, completed INTEGER, failed INTEGER, error TEXT)""")
                    db.execute("""CREATE TABLE IF NOT EXISTS videos (
                        job_id TEXT NOT NULL, video_id TEXT NOT NULL, idx INTEGER,
                        title TEXT, url TEXT, thumbnail TEXT, duration REAL,
                        status TEXT, filepath TEXT, filesize INTEGER, error TEXT,
                        PRIMARY KEY (job_id, video_id))""")
                    existing = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
                    for col, typ in _JOB_COLUMNS.items():
                        if col not in existing:
                            db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
                    db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_ts ON jobs(ts DESC)")
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("PRAGMA synchronous=NORMAL")
                _db_ready = True
    conn = sqlite3.connect(JOBS_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    _db()


def _save_job_summary(db_row: dict):
    """Backwards-compatible job upsert (also used by the test-suite)."""
    try:
        _init_db()
        row = {
            "job_id": db_row.get("job_id"),
            "ts": db_row.get("ts", int(time.time() * 1000)),
            "status": db_row.get("status"),
            "mode": db_row.get("mode"),
            "total": db_row.get("total", 0),
            "completed": db_row.get("completed", 0),
            "failed": db_row.get("failed", 0),
            "error": db_row.get("error"),
            "updated_ts": int(time.time() * 1000),
        }
        with _db() as db:
            db.execute("""INSERT INTO jobs
                (job_id, ts, status, mode, total, completed, failed, error, updated_ts)
                VALUES (:job_id,:ts,:status,:mode,:total,:completed,:failed,:error,:updated_ts)
                ON CONFLICT(job_id) DO UPDATE SET
                  status=excluded.status, mode=excluded.mode, total=excluded.total,
                  completed=excluded.completed, failed=excluded.failed,
                  error=excluded.error, updated_ts=excluded.updated_ts""", row)
    except Exception as exc:
        log.debug("job summary save failed: %s", exc)


def _upsert_job(job: dict):
    try:
        _init_db()
        row = {
            "job_id": job.get("id"), "ts": job.get("ts", int(time.time() * 1000)),
            "status": job.get("status"), "mode": job.get("mode"),
            "total": job.get("total_videos", 0),
            "completed": job.get("completed_videos", 0),
            "failed": job.get("failed_videos", 0),
            "error": job.get("error"), "updated_ts": int(time.time() * 1000),
            "url": job.get("url"), "title": job.get("title"),
            "quality": job.get("quality"), "section": job.get("section"),
            "date_after": job.get("date_after"), "date_before": job.get("date_before"),
            "out_dir": job.get("out_dir"), "subtitles": job.get("subtitles"),
        }
        with _db() as db:
            db.execute("""INSERT INTO jobs
                (job_id, ts, status, mode, total, completed, failed, error, updated_ts,
                 url, title, quality, section, date_after, date_before, out_dir, subtitles)
                VALUES (:job_id,:ts,:status,:mode,:total,:completed,:failed,:error,:updated_ts,
                        :url,:title,:quality,:section,:date_after,:date_before,:out_dir,:subtitles)
                ON CONFLICT(job_id) DO UPDATE SET
                  status=excluded.status, mode=excluded.mode, total=excluded.total,
                  completed=excluded.completed, failed=excluded.failed, error=excluded.error,
                  updated_ts=excluded.updated_ts, url=excluded.url, title=excluded.title,
                  quality=excluded.quality, section=excluded.section,
                  date_after=excluded.date_after, date_before=excluded.date_before,
                  out_dir=excluded.out_dir, subtitles=excluded.subtitles""", row)
    except Exception as exc:
        log.debug("job upsert failed: %s", exc)


def _upsert_videos(job_id: str, videos: list):
    try:
        _init_db()
        rows = []
        for i, v in enumerate(videos or []):
            rows.append({
                "job_id": job_id, "video_id": str(v.get("id") or f"v{i}"),
                "idx": v.get("index", i + 1), "title": v.get("title"),
                "url": v.get("url"), "thumbnail": v.get("thumbnail"),
                "duration": v.get("duration") or 0, "status": v.get("status"),
                "filepath": v.get("filepath"), "filesize": v.get("filesize") or 0,
                "error": v.get("error"),
            })
        with _db() as db:
            db.executemany("""INSERT INTO videos
                (job_id, video_id, idx, title, url, thumbnail, duration, status, filepath, filesize, error)
                VALUES (:job_id,:video_id,:idx,:title,:url,:thumbnail,:duration,:status,:filepath,:filesize,:error)
                ON CONFLICT(job_id, video_id) DO UPDATE SET
                  status=excluded.status, filepath=excluded.filepath,
                  filesize=excluded.filesize, error=excluded.error,
                  title=excluded.title, thumbnail=excluded.thumbnail""", rows)
    except Exception as exc:
        log.debug("video upsert failed: %s", exc)


def _persist_job(job_id: str, include_videos: bool = False):
    """Snapshot an in-memory job into the durable library."""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        snap = {k: copy.deepcopy(v) for k, v in job.items()
                if k not in ("videos", "base_opts", "log")}
        snap["id"] = job_id
        vids = copy.deepcopy(job.get("videos", [])) if include_videos else None
    _upsert_job(snap)
    if include_videos and vids:
        _upsert_videos(job_id, vids)


def _persist_video(job_id: str, video: dict):
    with jobs_lock:
        job = jobs.get(job_id)
        origin = job.get("origin_job_id") if job else None
        total = len(job.get("videos", [])) if job else 0
        completed = job.get("completed_videos", 0) if job else 0
        failed = job.get("failed_videos", 0) if job else 0
        status = job.get("status") if job else None

    # A retry continues the original download; write its result into the
    # original job so it does not become a second library entry.
    if origin:
        try:
            _upsert_videos(origin, [video])
        except Exception as exc:
            log.debug("retry video merge failed: %s", exc)
        return

    try:
        _upsert_videos(job_id, [video])
        with _db() as db:
            db.execute("UPDATE jobs SET completed=?, failed=?, total=?, status=?, updated_ts=? WHERE job_id=?",
                       (completed, failed, total, status, int(time.time() * 1000), job_id))
    except Exception as exc:
        log.debug("video persist failed: %s", exc)


def _purge_old_jobs_db(max_age_seconds: int = None):
    try:
        if max_age_seconds is None:
            days = int(get_cfg().get("history_retention_days", 90) or 0)
            if days <= 0:
                return
            max_age_seconds = days * 86400
        cutoff = int(time.time() * 1000) - max_age_seconds * 1000
        _init_db()
        with _db() as db:
            old = [r[0] for r in db.execute("SELECT job_id FROM jobs WHERE ts < ?", (cutoff,))]
            if old:
                marks = ",".join("?" for _ in old)
                db.execute(f"DELETE FROM videos WHERE job_id IN ({marks})", old)
                db.execute(f"DELETE FROM jobs WHERE job_id IN ({marks})", old)
    except Exception:
        pass


def _history_query(q: str = "", status: str = "", mode: str = "",
                   limit: int = 60, offset: int = 0) -> dict:
    _init_db()
    where, params = [], []
    if q:
        where.append("(COALESCE(title,'') LIKE ? OR COALESCE(url,'') LIKE ?)")
        like = f"%{q}%"
        params += [like, like]
    if status and status != "all":
        where.append("status = ?")
        params.append(status)
    if mode and mode != "all":
        where.append("mode = ?")
        params.append(mode)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with _db() as db:
        total = db.execute(f"SELECT COUNT(*) FROM jobs {clause}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT * FROM jobs {clause} ORDER BY ts DESC LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
        rows = [dict(r) for r in rows]
        ids = [r["job_id"] for r in rows]
        preview = {}
        if ids:
            marks = ",".join("?" for _ in ids)
            vids = db.execute(
                f"SELECT * FROM videos WHERE job_id IN ({marks}) ORDER BY idx", ids).fetchall()
            for v in vids:
                preview.setdefault(v["job_id"], []).append(dict(v))
    items = []
    for r in rows:
        vids = preview.get(r["job_id"], [])
        done = sum(1 for v in vids if v["status"] == "done")
        failed = sum(1 for v in vids if v["status"] == "error")
        size = sum(int(v.get("filesize") or 0) for v in vids)
        duration = sum(float(v.get("duration") or 0) for v in vids)
        thumb = next((v["thumbnail"] for v in vids if v.get("thumbnail")), "")
        items.append({
            "id": r["job_id"], "ts": r["ts"], "status": r["status"], "mode": r["mode"],
            "url": r["url"] or "", "title": r["title"] or "",
            "quality": r["quality"] or "", "section": r["section"] or "",
            "date_after": r["date_after"] or "", "date_before": r["date_before"] or "",
            "out_dir": r["out_dir"] or "", "subtitles": r["subtitles"] or "",
            "error": r["error"],
            "total_videos": r["total"] or len(vids),
            "completed_videos": done if vids else (r["completed"] or 0),
            "failed_videos": failed if vids else (r["failed"] or 0),
            "size_bytes": size, "duration": duration, "thumbnail": thumb,
            "preview": [{k: v.get(k) for k in ("video_id", "title", "thumbnail", "status",
                                                "duration", "filesize", "filepath")}
                        for v in vids[:4]],
        })
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _history_job(job_id: str):
    _init_db()
    with _db() as db:
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        vids = [dict(v) for v in db.execute(
            "SELECT * FROM videos WHERE job_id=? ORDER BY idx", (job_id,)).fetchall()]
    data = dict(row)
    data["id"] = data.pop("job_id")
    data["videos"] = vids
    data["size_bytes"] = sum(int(v.get("filesize") or 0) for v in vids)
    return data


def _delete_history_job(job_id: str) -> bool:
    _init_db()
    with _db() as db:
        cur = db.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
        db.execute("DELETE FROM videos WHERE job_id=?", (job_id,))
        return cur.rowcount > 0


def _clear_history(status: str = "all") -> int:
    _init_db()
    with _db() as db:
        if status and status != "all":
            ids = [r[0] for r in db.execute("SELECT job_id FROM jobs WHERE status=?", (status,))]
            if not ids:
                return 0
            marks = ",".join("?" for _ in ids)
            db.execute(f"DELETE FROM videos WHERE job_id IN ({marks})", ids)
            cur = db.execute(f"DELETE FROM jobs WHERE job_id IN ({marks})", ids)
        else:
            cur = db.execute("DELETE FROM jobs")
            db.execute("DELETE FROM videos")
        return cur.rowcount


# ── Job store ─────────────────────────────────────────────────────────────────
jobs:               dict = {}
jobs_lock           = threading.Lock()
cancel_flags:       dict = {}
video_cancel_flags: dict = {}


def _new_job_id() -> str:
    return f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


def _job_ts(job_id: str) -> int:
    try:
        return int(str(job_id).split("-")[0])
    except Exception:
        return int(time.time() * 1000)


# ── Per-video progress hook ───────────────────────────────────────────────────
def make_video_hook(job_id: str, video_id: str):
    prev_bytes = 0
    prev_time  = time.time()

    def hook(d):
        nonlocal prev_bytes, prev_time
        if cancel_flags.get(job_id) or video_cancel_flags.get((job_id, video_id)):
            raise yt_dlp.utils.DownloadCancelled("Cancelled")
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            vid = next((v for v in job["videos"] if v["id"] == video_id), None)
            if not vid:
                return
            st = d.get("status")
            if st == "downloading":
                dl    = d.get("downloaded_bytes", 0) or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                vid["status"]           = "downloading"
                vid["percent"]          = round(dl / total * 100, 1) if total else 0
                vid["downloaded_bytes"] = dl
                vid["total_bytes"]      = total
                now = time.time()
                if now - prev_time > 0.5:
                    speed_bps = d.get("speed") or ((dl - prev_bytes) / (now - prev_time))
                    if speed_bps:
                        vid["speed"] = (f"{speed_bps / 1e6:.1f} MiB/s" if speed_bps > 1e6
                                        else f"{speed_bps / 1e3:.0f} KiB/s")
                    else:
                        vid["speed"] = ""
                    eta = d.get("eta")
                    if eta is None and total > 0 and speed_bps:
                        eta = int((total - dl) / speed_bps)
                    if eta:
                        m, s = divmod(int(eta), 60)
                        vid["eta"] = f"{m}m{s}s" if m else f"{s}s"
                    else:
                        vid["eta"] = ""
                    prev_bytes, prev_time = dl, now
            elif st == "finished":
                info = d.get("info_dict") or {}
                vid["percent"] = 100
                vid["status"]  = "processing"
                vid["speed"]   = ""
                vid["eta"]     = ""
                vid["filepath"] = info.get("filepath") or d.get("filename") or vid.get("filepath")
            elif st == "error":
                vid["status"] = "error"
                vid["error"]  = _clean_error(d.get("error", "Unknown error"))
                vid["filepath"] = d.get("filename") or vid.get("filepath")
    return hook


def make_postproc_hook(job_id: str, video_id: str):
    """After merge/post-processing, remember the real final file."""
    def hook(d):
        if d.get("status") != "finished":
            return
        info = d.get("info_dict") or {}
        path = info.get("filepath")
        if not path:
            return
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            vid = next((v for v in job["videos"] if v["id"] == video_id), None)
            if vid:
                vid["filepath"] = path
    return hook


# ── Scanning ──────────────────────────────────────────────────────────────────
def scan_videos(job_id: str, url: str, extra_opts: dict = None) -> list:
    opts = {"quiet": True, "extract_flat": True,
            "socket_timeout": 15, "ignoreerrors": True}
    if extra_opts:
        opts.update(extra_opts)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return []
    entries = info.get("entries") or [info]
    result = []
    for i, e in enumerate(entries, 1):
        if not e or cancel_flags.get(job_id):
            continue
        vid_id  = e.get("id") or f"video_{i}"
        vid_url = e.get("webpage_url") or e.get("url") or ""
        if not vid_url.startswith("http"):
            vid_url = f"https://www.youtube.com/watch?v={vid_id}"
        result.append({
            "id": str(vid_id), "index": i,
            "title": e.get("title") or f"Video {i}",
            "url": vid_url, "thumbnail": best_thumb(e),
            "duration": e.get("duration") or 0,
            "status": "queued", "percent": 0, "speed": "", "eta": "",
            "downloaded_bytes": 0, "total_bytes": 0, "error": None,
            "retry_count": 0, "filepath": None, "filesize": 0,
        })
        with jobs_lock:
            job = jobs.get(job_id)
            if job:
                job["scan_count"] = i
                job["videos"] = result
    return result


# ── Download slots (atomic claim/release) ─────────────────────────────────────
def _claim_slot(job_id: str) -> bool:
    while True:
        if cancel_flags.get(job_id):
            return False
        max_conc = max(1, int(get_cfg()["max_concurrent"]))
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return False
            if job.get("active_slots", 0) < max_conc:
                job["active_slots"] = job.get("active_slots", 0) + 1
                return True
        time.sleep(0.25)


def _release_slot(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if job:
            job["active_slots"] = max(0, job.get("active_slots", 1) - 1)


# ── Download one video ────────────────────────────────────────────────────────
def _video_outtmpl(template: str, index) -> str:
    """Replace the playlist-index placeholder for a single video download."""
    try:
        return template.replace("%(playlist_index)02d", f"{int(index or 1):02d}")
    except Exception:
        return template


def _resolve_output(path, ext: str):
    if not path:
        return None
    p = Path(path)
    # Prefer the merged/post-processed artefact (e.g. Title.mp4) over a
    # leftover component file (e.g. Title.f251.webm).
    for candidate in [p.with_suffix("." + ext), p.with_suffix(".mp4"), p.with_suffix(".mkv"),
                      p.with_suffix(".webm"), p.with_suffix(".mp3"), p.with_suffix(".m4a")]:
        if candidate.exists() and candidate.is_file():
            return candidate
    if p.exists() and p.is_file():
        return p
    try:
        for candidate in p.parent.glob(p.stem + ".*"):
            if candidate.suffix.lower() in (".part", ".ytdl", ".temp", ".json"):
                continue
            if candidate.is_file():
                return candidate
    except Exception:
        pass
    return None


def download_one(job_id: str, video: dict, base_opts: dict, ext: str = "mp4") -> bool:
    if cancel_flags.get(job_id):
        with jobs_lock:
            video["status"] = "skipped"
        _persist_video(job_id, video)
        return False

    vid_id = video["id"]
    video_cancel_flags.pop((job_id, vid_id), None)

    if not _claim_slot(job_id):
        with jobs_lock:
            if video.get("status") == "queued":
                video["status"] = "skipped"
        _persist_video(job_id, video)
        return False

    opts = {**base_opts, "noplaylist": True,
            "progress_hooks": [make_video_hook(job_id, vid_id)],
            "postprocessor_hooks": [make_postproc_hook(job_id, vid_id)]}
    if "%(playlist_index)" in str(opts.get("outtmpl", "")):
        opts["outtmpl"] = _video_outtmpl(opts["outtmpl"], video.get("index"))

    try:
        with jobs_lock:
            video["status"]  = "downloading"
            video["percent"] = 0
            video["error"]   = None
            job = jobs.get(job_id)
            if job is not None:
                job["log"].append(f"↓ [{video['index']}] {video['title'][:70]}")

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([video["url"]])

        final = _resolve_output(video.get("filepath"), ext)
        with jobs_lock:
            video["status"]  = "done"
            video["percent"] = 100
            if final:
                video["filepath"] = str(final)
                try:
                    video["filesize"] = final.stat().st_size
                except OSError:
                    pass
            job = jobs.get(job_id)
            if job is not None:
                job["completed_videos"] += 1
                job["log"].append(f"✓ [{video['index']}] {video['title'][:70]}")
        _persist_video(job_id, video)
        return True

    except yt_dlp.utils.DownloadCancelled:
        with jobs_lock:
            video["status"] = "skipped"
            job = jobs.get(job_id)
            if job is not None:
                job["log"].append(f"⏹ [{video['index']}] Skipped")
        _persist_video(job_id, video)
        return False

    except Exception as e:
        err = _clean_error(e)
        with jobs_lock:
            video["status"] = "error"
            video["error"]  = err
            job = jobs.get(job_id)
            if job is not None:
                job["failed_videos"] += 1
                job["log"].append(f"✗ [{video['index']}] {video['title'][:45]} — {err[:100]}")
        _persist_video(job_id, video)
        return False

    finally:
        _release_slot(job_id)


def run_pool(job_id: str, videos: list, base_opts: dict, ext: str = "mp4"):
    def worker(vid):
        download_one(job_id, vid, base_opts, ext)

    workers = max(1, min(12, len(videos)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, v) for v in videos]
        for f in futures:
            f.result()


# ── yt-dlp option builder ─────────────────────────────────────────────────────
def _base_opts(cfg: dict, quality: str, out: Path, mode: str,
               archive: Path = None, subs: str = None) -> dict:
    fmt, ext = QUALITY_MAP.get(quality, QUALITY_MAP["best"])
    rate_limit = float(cfg.get("speed_limit_mbps", 0) or 0)
    cookies_browser = str(cfg.get("cookies_browser", "") or "").strip()
    try:
        subs_lang = normalize_subs(subs if subs is not None else cfg.get("subtitles_lang", ""))
    except ValueError:
        subs_lang = ""
    spons = bool(cfg.get("sponsorblock", False))

    opts = dict(
        format=fmt, merge_output_format=ext,
        add_metadata=True, no_warnings=True, quiet=True, noprogress=True, color="no_color",
        socket_timeout=int(cfg.get("socket_timeout", 30)),
        concurrent_fragment_downloads=4,
        concurrent_fragments=4,
        ratelimit=(rate_limit * 1e6) if rate_limit > 0 else None,
        remote_components=["ejs:github"],
        continuedl=True, retries=5, fragment_retries=5,
        outtmpl=str(out / ("%(playlist_index)02d - %(title)s.%(ext)s"
                           if mode == "playlist"
                           else "%(upload_date)s - %(title)s.%(ext)s"
                           if mode == "channel"
                           else "%(title)s.%(ext)s")),
    )
    if archive:
        opts["download_archive"] = str(archive)
        opts["sleep_interval"] = int(cfg.get("sleep_interval", 1))
        opts["max_sleep_interval"] = int(cfg.get("max_sleep_interval", 3))
    if mode == "channel":
        opts["ignoreerrors"] = True
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    if subs_lang:
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = ["all"] if subs_lang == "all" else subs_lang.split(",")
        opts["subtitlesformat"] = "vtt/srt/best"
    if spons:
        opts["sponsorblock_remove"] = {"sponsor", "selfpromo", "interaction",
                                       "intro", "outro", "preview", "music_offtopic"}
    if quality == "audio":
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio", "preferredcodec": "mp3",
            "preferredquality": "192"}]
    return opts, ext


# ── Master job worker ─────────────────────────────────────────────────────────
def run_job(job_id: str, mode: str, url: str, quality: str,
            date_after: str, date_before: str, section: str, subtitles: str = ""):

    cfg = get_cfg()
    dl_dir = Path(cfg["download_dir"]).expanduser()
    max_conc = max(1, int(cfg["max_concurrent"]))
    out = dl_dir
    success = False

    try:
        with jobs_lock:
            jobs[job_id]["status"] = "scanning"
            jobs[job_id]["scan_count"] = 0
            jobs[job_id]["log"].append("Scanning…")
        _persist_job(job_id)

        # ── Scan ────────────────────────────────────────────────────────────
        if mode == "single":
            out = dl_dir / "Videos"
            out.mkdir(parents=True, exist_ok=True)
            with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": False,
                                   "socket_timeout": 15, "noplaylist": True}) as ydl:
                raw = ydl.extract_info(url, download=False)
            title = raw.get("title") or "Video"
            videos = [{
                "id": str(raw.get("id", "v1")), "index": 1,
                "title": title, "url": url,
                "thumbnail": best_thumb(raw), "duration": raw.get("duration") or 0,
                "status": "queued", "percent": 0, "speed": "", "eta": "",
                "downloaded_bytes": 0, "total_bytes": 0, "error": None,
                "retry_count": 0, "filepath": None, "filesize": 0,
            }]
            base_opts, ext = _base_opts(cfg, quality, out, mode, subs=subtitles)
            with jobs_lock:
                jobs[job_id]["title"] = title

        elif mode == "playlist":
            with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True,
                                   "socket_timeout": 10, "playlist_items": "1"}) as ydl:
                head = ydl.extract_info(url, download=False)
            pl_title = sanitize(head.get("title") or head.get("playlist_title") or "Playlist")
            with jobs_lock:
                jobs[job_id]["log"].append(f"Playlist · {pl_title}")
                jobs[job_id]["title"] = pl_title
            out = dl_dir / "Playlists" / pl_title
            out.mkdir(parents=True, exist_ok=True)
            videos = scan_videos(job_id, url)
            base_opts, ext = _base_opts(cfg, quality, out, mode, subs=subtitles)

        elif mode == "channel":
            tab = {"videos": "/videos", "shorts": "/shorts",
                   "streams": "/streams", "all": ""}.get(section, "/videos")
            ch_url = url.rstrip("/")
            ch = "Channel"
            if "@" in ch_url:
                ch = ch_url.split("@")[-1].split("/")[0]
            elif "/channel/" in ch_url:
                ch = ch_url.split("/channel/")[-1].split("/")[0]
            elif "/user/" in ch_url:
                ch = ch_url.split("/user/")[-1].split("/")[0]
            ch = sanitize(ch)
            dl_url = ch_url + tab
            label = section.capitalize()
            with jobs_lock:
                jobs[job_id]["log"].append(f"{ch} · {label}")
                jobs[job_id]["title"] = f"{ch} · {label}"
                if date_after:
                    jobs[job_id]["log"].append(f"Dates {date_after} → {date_before or 'today'}")
            out = dl_dir / "Channels" / ch / label
            out.mkdir(parents=True, exist_ok=True)
            archive = dl_dir / "Channels" / ".archives" / f"{ch}_{label}.txt"
            archive.parent.mkdir(parents=True, exist_ok=True)
            extra = {}
            if date_after:
                da = to_ytdlp_date(date_after)
                db = to_ytdlp_date(date_before) if date_before else "today"
                extra["daterange"] = yt_dlp.utils.DateRange(da, db)
            videos = scan_videos(job_id, dl_url, extra)
            base_opts, ext = _base_opts(cfg, quality, out, mode, archive=archive, subs=subtitles)
            if date_after:
                da = to_ytdlp_date(date_after)
                db = to_ytdlp_date(date_before) if date_before else "today"
                base_opts["daterange"] = yt_dlp.utils.DateRange(da, db)
        else:
            raise ValueError(f"Unknown mode: {mode}")

        if not videos:
            raise Exception("No videos found.")

        try:
            free_gb = shutil.disk_usage(dl_dir).free / 1e9
        except Exception:
            free_gb = None
        if free_gb is not None and free_gb < 1:
            raise Exception(f"Low disk space: {free_gb:.2f} GB free in {dl_dir} (need at least 1 GB)")

        with jobs_lock:
            job = jobs[job_id]
            job["videos"]           = videos
            job["total_videos"]     = len(videos)
            job["completed_videos"] = 0
            job["failed_videos"]    = 0
            job["active_slots"]     = 0
            job["max_concurrent"]   = max_conc
            job["base_opts"]        = base_opts
            job["out_dir"]          = str(out)
            job["status"]           = "running" if mode == "single" else "ready"
            job["log"].append(f"{len(videos)} video(s) · {max_conc} concurrent slots")
        _persist_job(job_id, include_videos=True)

        if mode != "single":
            deadline = time.time() + 3600
            while time.time() < deadline:
                if cancel_flags.get(job_id):
                    raise Exception("Cancelled before start.")
                with jobs_lock:
                    st = jobs[job_id]["status"]
                if st == "running":
                    break
                time.sleep(0.4)
            else:
                raise Exception("Timed out waiting for confirmation.")

        run_pool(job_id, videos, base_opts, ext)
        success = not cancel_flags.get(job_id)

    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"]  = str(e)
            jobs[job_id]["log"].append(f"✗ {e}")

    finally:
        with jobs_lock:
            job = jobs[job_id]
            job["done"]         = True
            job["active_slots"] = 0
            if cancel_flags.get(job_id):
                job["status"] = "cancelled"
                job["log"].append("Cancelled.")
            elif success:
                comp = job.get("completed_videos", 0)
                fail = job.get("failed_videos", 0)
                job["status"] = "error" if (fail and comp == 0) else "done"
                if job["status"] == "error":
                    job["error"] = f"{fail} video(s) failed"
                job["log"].append(f"{comp} done, {fail} failed → {out}")
        cancel_flags.pop(job_id, None)
        _cleanup_video_cancel_flags(job_id)
        _persist_job(job_id, include_videos=True)
        try:
            _save_job_summary({
                "job_id": job_id, "ts": int(time.time() * 1000),
                "status": jobs[job_id]["status"], "mode": jobs[job_id].get("mode", "?"),
                "total": jobs[job_id].get("total_videos", 0),
                "completed": jobs[job_id].get("completed_videos", 0),
                "failed": jobs[job_id].get("failed_videos", 0),
                "error": jobs[job_id].get("error"),
            })
        except Exception as exc:
            log.debug("persist failed: %s", exc)


def _cleanup_video_cancel_flags(job_id: str):
    keys_to_remove = [k for k in video_cancel_flags if k[0] == job_id]
    for k in keys_to_remove:
        video_cancel_flags.pop(k, None)


def _purge_old_jobs(max_age_seconds: int = 86400):
    now = time.time()
    with jobs_lock:
        to_remove = [jid for jid, j in jobs.items()
                     if j["done"] and (now - _job_ts(jid) / 1000) > max_age_seconds]
        for jid in to_remove:
            jobs.pop(jid, None)
            cancel_flags.pop(jid, None)
    _cleanup_video_cancel_flags_bulk(to_remove)


def _cleanup_video_cancel_flags_bulk(job_ids: list):
    keys_to_remove = [k for k in video_cancel_flags if k[0] in job_ids]
    for k in keys_to_remove:
        video_cancel_flags.pop(k, None)


# ── Routes ────────────────────────────────────────────────────────────────────
_ytdlp_latest = {"checked": 0.0, "version": None}


def _fetch_latest_ytdlp():
    try:
        with urlopen("https://pypi.org/pypi/yt-dlp/json", timeout=10) as r:
            data = json.loads(r.read().decode())
            _ytdlp_latest["version"] = data["info"]["version"]
    except Exception as exc:
        log.debug("PyPI yt-dlp check failed: %s", exc)
    _ytdlp_latest["checked"] = time.time()


def _norm_ver(s: str):
    parts = re.split(r"[.\-]", s)
    if all(p.isdigit() for p in parts[:4]):
        return tuple(int(p or 0) for p in parts[:4])
    return s


@app.route("/api/version")
def version_info():
    installed = yt_dlp.version.__version__
    if _ytdlp_latest["version"] is None or time.time() - _ytdlp_latest["checked"] > 3600:
        threading.Thread(target=_fetch_latest_ytdlp, daemon=True).start()
    latest = _ytdlp_latest["version"]
    return jsonify({"installed": installed, "latest": latest,
                    "stale": bool(latest and _norm_ver(latest) != _norm_ver(installed))})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/settings", methods=["GET"])
def get_settings():
    with settings_lock:
        return jsonify(dict(settings))


@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.json or {}
    with settings_lock:
        for k, v in data.items():
            if k in DEFAULT_SETTINGS:
                settings[k] = v
        save_settings(settings)
        return jsonify(dict(settings))


@app.route("/api/settings/reset", methods=["POST"])
def reset_settings():
    global settings
    with settings_lock:
        settings = dict(DEFAULT_SETTINGS)
        save_settings(settings)
        return jsonify(dict(settings))


def _spawn_job(url: str, mode: str, quality: str, section: str,
               date_after: str = "", date_before: str = "", subtitles: str = "") -> str:
    job_id = _new_job_id()
    ts = int(time.time() * 1000)
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id, "ts": ts, "status": "queued", "log": [], "done": False,
            "error": None, "videos": [], "scan_count": 0,
            "total_videos": 0, "completed_videos": 0, "failed_videos": 0,
            "active_slots": 0, "max_concurrent": 0, "base_opts": {},
            "mode": mode, "url": url, "quality": quality, "section": section,
            "date_after": date_after, "date_before": date_before,
            "subtitles": subtitles, "title": "", "out_dir": "", "preview": {},
        }
    _persist_job(job_id)
    threading.Thread(target=run_job,
                     args=(job_id, mode, url, quality, date_after, date_before, section, subtitles),
                     daemon=True).start()
    return job_id


@app.route("/api/start", methods=["POST"])
def start_download():
    data        = request.json or {}
    url         = normalize_url(data.get("url", ""))
    mode        = data.get("mode", "single")
    quality     = data.get("quality") or settings.get("default_quality", "best")
    date_after  = (data.get("dateAfter") or "").strip()
    date_before = (data.get("dateBefore") or "").strip()
    section     = data.get("section", "videos")
    if "subtitles" in data:
        try:
            subtitles = normalize_subs(data.get("subtitles"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    else:
        subtitles = normalize_subs(settings.get("subtitles_lang", ""))

    if not url:
        return jsonify({"error": "URL is required"}), 400
    if mode not in ("single", "playlist", "channel"):
        return jsonify({"error": f"Invalid mode: {mode}"}), 400
    ok_, why = _is_public_url(url)
    if not ok_:
        log.warning("Blocked non-public URL: %s (%s)", url, why)
        return jsonify({"error": f"Blocked: {why}"}), 400
    if quality not in QUALITY_MAP:
        return jsonify({"error": f"Invalid quality: {quality}"}), 400

    job_id = _spawn_job(url, mode, quality, section, date_after, date_before, subtitles)
    return jsonify({"jobId": job_id})


@app.route("/api/confirm/<job_id>", methods=["POST"])
def confirm_download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if job["status"] != "ready":
            return jsonify({"error": "Not in ready state"}), 400
        job["status"] = "running"
        job["log"].append("Confirmed.")
    _persist_job(job_id)
    return jsonify({"success": True})


@app.route("/api/retry_video/<job_id>/<video_id>", methods=["POST"])
def retry_video(job_id, video_id):
    """Re-queue a single failed/skipped video and download it immediately."""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        vid = next((v for v in job["videos"] if v["id"] == video_id), None)
        if not vid:
            return jsonify({"error": "Video not found"}), 404
        if vid["status"] not in ("error", "skipped"):
            return jsonify({"error": "Video not in retryable state"}), 400
        vid["status"]  = "queued"
        vid["percent"] = 0
        vid["error"]   = None
        vid["retry_count"] = vid.get("retry_count", 0) + 1
        if job["done"]:
            job["done"]   = False
            job["status"] = "running"
        job["failed_videos"] = max(0, job.get("failed_videos", 1) - 1)
        base_opts = dict(job.get("base_opts", {}))
        video = copy.deepcopy(vid)

    quality = "best"
    with jobs_lock:
        quality = jobs.get(job_id, {}).get("quality") or "best"
    ext = QUALITY_MAP.get(quality, QUALITY_MAP["best"])[1]
    _persist_job(job_id, include_videos=True)

    def _retry():
        download_one(job_id, vid, base_opts, ext)
        with jobs_lock:
            j = jobs.get(job_id)
            if j:
                still_active = any(v["status"] in ("queued", "downloading", "processing")
                                   for v in j["videos"])
                if not still_active:
                    j["done"]   = True
                    j["status"] = "done" if j.get("failed_videos", 0) == 0 else "error"
                    j["log"].append("Retry complete.")
        _persist_job(job_id, include_videos=True)
    threading.Thread(target=_retry, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/events/<job_id>")
def job_events(job_id):
    def stream():
        while True:
            final = False
            with jobs_lock:
                job = jobs.get(job_id)
                if not job:
                    yield "data: " + json.dumps({"missing": True}) + "\n\n"
                    return
                j = copy.deepcopy(job)
                j.pop("base_opts", None)
                final = j.get("done") and not any(
                    v["status"] in ("queued", "downloading", "processing") for v in j.get("videos", []))
            yield "data: " + json.dumps(j) + "\n\n"
            if final:
                return
            time.sleep(1.0)
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        j = copy.deepcopy(job)
    j.pop("base_opts", None)
    return jsonify(j)


@app.route("/api/status/all")
def all_jobs_status():
    _purge_old_jobs()
    with jobs_lock:
        out = {jid: {
                "status":           j["status"],
                "done":             j["done"],
                "ts":               j.get("ts"),
                "mode":             j.get("mode"),
                "title":            j.get("title"),
                "total_videos":     j["total_videos"],
                "completed_videos": j["completed_videos"],
                "failed_videos":    j["failed_videos"],
                "active_slots":     j["active_slots"],
                "error":            j["error"],
            }
            for jid, j in jobs.items()}
    try:
        _purge_old_jobs_db()
        with _db() as db:
            rows = db.execute("SELECT * FROM jobs").fetchall()
        for row in rows:
            r = dict(row)
            out.setdefault(r["job_id"], {
                "status": r["status"], "done": True, "ts": r["ts"], "mode": r["mode"],
                "title": r["title"], "total_videos": r["total"],
                "completed_videos": r["completed"], "failed_videos": r["failed"],
                "active_slots": 0, "error": r["error"],
            })
    except Exception:
        pass
    return jsonify(out)


@app.route("/api/active")
def active_jobs():
    with jobs_lock:
        out = []
        for jid, j in jobs.items():
            if j["done"]:
                continue
            snap = copy.deepcopy(j)
            snap.pop("base_opts", None)
            out.append(snap)
    return jsonify(out)


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_download(job_id):
    with jobs_lock:
        if job_id not in jobs:
            return jsonify({"error": "Job not found"}), 404
        if jobs[job_id]["done"]:
            return jsonify({"error": "Already done"}), 400
        jobs[job_id]["log"].append("Cancellation requested…")
    cancel_flags[job_id] = True
    return jsonify({"success": True})


@app.route("/api/cancel_video/<job_id>/<video_id>", methods=["POST"])
def cancel_single_video(job_id, video_id):
    with jobs_lock:
        if job_id not in jobs:
            return jsonify({"error": "Job not found"}), 404
    video_cancel_flags[(job_id, video_id)] = True
    return jsonify({"success": True})


@app.route("/api/info")
def get_info():
    url = normalize_url(request.args.get("url", ""))
    if not url:
        return jsonify({"error": "No URL"}), 400
    ok_, why = _is_public_url(url)
    if not ok_:
        return jsonify({"error": f"Blocked: {why}"}), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True,
                               "playlist_items": "1", "socket_timeout": 10}) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = info.get("entries") or []
        count = info.get("playlist_count") or (len(entries) if entries else 0)
        manual_subs = sorted((info.get("subtitles") or {}).keys())
        auto_subs = sorted((info.get("automatic_captions") or {}).keys())
        return jsonify({
            "title":       info.get("title") or info.get("playlist_title", ""),
            "channel":     info.get("channel") or info.get("uploader", ""),
            "thumbnail":   best_thumb(info),
            "duration":    info.get("duration", 0),
            "is_playlist": info.get("_type") == "playlist",
            "count":       count,
            "subtitle_manual": manual_subs[:40],
            "subtitle_auto":   auto_subs[:40],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def _promote_retry_entry(retry_id: str):
    """Turn an orphaned retry entry into a normal library card."""
    try:
        with _db() as db:
            row = db.execute("SELECT title, total FROM jobs WHERE job_id=?", (retry_id,)).fetchone()
            if not row:
                return
            title = re.sub(r"^Retry · ", "", row[0] or "") or "Download"
            mode = "single" if (row[1] or 0) == 1 else "playlist"
            db.execute("UPDATE jobs SET title=?, mode=? WHERE job_id=?", (title, mode, retry_id))
    except Exception as exc:
        log.debug("retry promotion failed: %s", exc)


def _merge_retry_results(origin_id: str, retry_id: str, videos: list):
    """Fold a finished retry back into its original library entry."""
    try:
        _init_db()
        _upsert_videos(origin_id, videos)
        with _db() as db:
            if not db.execute("SELECT 1 FROM jobs WHERE job_id=?", (origin_id,)).fetchone():
                # The original was deleted meanwhile: keep the retry result,
                # but promote it to a normal card.
                _persist_job(retry_id, include_videos=True)
                _promote_retry_entry(retry_id)
                return
            rows = db.execute("SELECT status, COUNT(*) FROM videos WHERE job_id=? GROUP BY status",
                              (origin_id,)).fetchall()
            counts = {r[0]: r[1] for r in rows}
            total = sum(counts.values())
            old_total = db.execute("SELECT total FROM jobs WHERE job_id=?", (origin_id,)).fetchone()[0] or 0
            total = max(total, old_total)
            done = counts.get("done", 0)
            failed = counts.get("error", 0)
            status = "done" if failed == 0 else "error"
            error = None if failed == 0 else f"{failed} video(s) could not be downloaded"
            db.execute("""UPDATE jobs SET total=?, completed=?, failed=?, status=?, error=?, updated_ts=?
                          WHERE job_id=?""",
                       (total, done, failed, status, error, int(time.time() * 1000), origin_id))
            db.execute("DELETE FROM videos WHERE job_id=?", (retry_id,))
            db.execute("DELETE FROM jobs WHERE job_id=?", (retry_id,))
        with jobs_lock:
            orig = jobs.get(origin_id)
            if orig:
                by_id = {v.get("id"): v for v in videos}
                for v in orig.get("videos", []):
                    new = by_id.get(v.get("id"))
                    if new:
                        for k in ("status", "filepath", "filesize", "error", "percent"):
                            v[k] = new.get(k, v.get(k))
                orig["completed_videos"] = done
                orig["failed_videos"] = failed
                orig["status"] = status
                orig["error"] = error
                if failed == 0:
                    orig["done"] = True
    except Exception as exc:
        log.debug("retry merge failed: %s", exc)


def _merge_legacy_retries():
    """One-time cleanup for retry entries created by older versions."""
    try:
        _init_db()
        pairs = []
        orphans = []
        with _db() as db:
            retries = [dict(r) for r in db.execute(
                "SELECT * FROM jobs WHERE mode='retry' ORDER BY ts").fetchall()]
            for r in retries:
                if not r.get("url"):
                    continue
                origin = db.execute(
                    "SELECT job_id FROM jobs WHERE mode<>'retry' AND url=? AND job_id<>? "
                    "ORDER BY ts DESC LIMIT 1", (r["url"], r["job_id"])).fetchone()
                if not origin:
                    orphans.append(r["job_id"])
                    continue
                origin_id = origin[0]
                vids = [dict(v) for v in db.execute(
                    "SELECT * FROM videos WHERE job_id=? ORDER BY idx", (r["job_id"],)).fetchall()]
                existing = {v[0] for v in db.execute(
                    "SELECT video_id FROM videos WHERE job_id=?", (origin_id,)).fetchall()}
                max_idx = db.execute(
                    "SELECT COALESCE(MAX(idx),0) FROM videos WHERE job_id=?", (origin_id,)).fetchone()[0] or 0
                mapped = []
                for v in vids:
                    idx = v["idx"]
                    if v["video_id"] not in existing:
                        max_idx += 1
                        idx = max_idx
                    mapped.append({
                        "id": v["video_id"], "index": idx, "title": v["title"],
                        "url": v["url"], "thumbnail": v["thumbnail"],
                        "duration": v["duration"], "status": v["status"],
                        "filepath": v["filepath"], "filesize": v["filesize"],
                        "error": v["error"], "percent": 100 if v["status"] == "done" else 0,
                    })
                pairs.append((origin_id, r["job_id"], mapped))
        for origin_id, retry_id, vids in pairs:
            _merge_retry_results(origin_id, retry_id, vids)
            log.info("Merged legacy retry %s into %s", retry_id, origin_id)
        for retry_id in orphans:
            _promote_retry_entry(retry_id)
            log.info("Promoted orphaned retry %s to a normal entry", retry_id)
    except Exception as exc:
        log.warning("Legacy retry cleanup failed: %s", exc)


# ── History API ───────────────────────────────────────────────────────────────
@app.route("/api/history")
def history_list():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "all")
    mode = request.args.get("mode", "all")
    try:
        limit = min(200, max(1, int(request.args.get("limit", 60))))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        return jsonify({"error": "Invalid pagination"}), 400
    return jsonify(_history_query(q, status, mode, limit, offset))


@app.route("/api/history/<job_id>")
def history_item(job_id):
    job = _history_job(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify(job)


@app.route("/api/history/<job_id>", methods=["DELETE"])
def history_delete(job_id):
    if not _delete_history_job(job_id):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"success": True})


@app.route("/api/history", methods=["DELETE"])
def history_clear():
    status = request.args.get("status", "all")
    removed = _clear_history(status)
    return jsonify({"success": True, "removed": removed})


@app.route("/api/history/<job_id>/relaunch", methods=["POST"])
def history_relaunch(job_id):
    row = _history_job(job_id)
    if not row:
        return jsonify({"error": "Not found"}), 404
    url = normalize_url(row.get("url") or "")
    if not url:
        return jsonify({"error": "This entry has no stored URL"}), 400
    ok_, why = _is_public_url(url)
    if not ok_:
        return jsonify({"error": f"Blocked: {why}"}), 400
    mode = row.get("mode") if row.get("mode") in ("single", "playlist", "channel") else "single"
    quality = row.get("quality") if row.get("quality") in QUALITY_MAP else "best"
    new_id = _spawn_job(url, mode, quality, row.get("section") or "videos",
                        row.get("date_after") or "", row.get("date_before") or "",
                        row.get("subtitles") or "")
    return jsonify({"jobId": new_id})


@app.route("/api/history/<job_id>/retry", methods=["POST"])
def history_retry_failed(job_id):
    """Re-download only the failed videos of a finished job."""
    row = _history_job(job_id)
    if not row:
        return jsonify({"error": "Not found"}), 404
    with jobs_lock:
        live = jobs.get(job_id)
    if live and not live.get("done"):
        return jsonify({"error": "This job is still running"}), 409
    failed = [v for v in row.get("videos", []) if v.get("status") in ("error", "skipped")]
    if not failed:
        return jsonify({"error": "Nothing to retry"}), 400

    cfg = get_cfg()
    dl_dir = Path(cfg["download_dir"]).expanduser()
    out = Path(row.get("out_dir") or dl_dir / "Videos")
    try:
        out.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return jsonify({"error": f"Cannot create {out}: {exc}"}), 400
    quality = row.get("quality") if row.get("quality") in QUALITY_MAP else "best"
    base_opts, ext = _base_opts(cfg, quality, out, "retry", subs=row.get("subtitles") or "")
    videos = []
    for i, v in enumerate(failed, 1):
        videos.append({
            "id": v["video_id"], "index": v.get("idx") or i, "title": v.get("title") or pget(v, "url"),
            "url": v.get("url"), "thumbnail": v.get("thumbnail"),
            "duration": v.get("duration") or 0, "status": "queued", "percent": 0,
            "speed": "", "eta": "", "downloaded_bytes": 0, "total_bytes": 0,
            "error": None, "retry_count": 0, "filepath": None, "filesize": 0,
        })
    videos = [v for v in videos if v["url"]]
    if not videos:
        return jsonify({"error": "Stored URLs are missing"}), 400

    new_id = _new_job_id()
    ts = int(time.time() * 1000)
    with jobs_lock:
        jobs[new_id] = {
            "id": new_id, "ts": ts, "status": "running", "log": [f"Retrying {len(videos)} video(s)"],
            "done": False, "error": None, "videos": videos, "scan_count": len(videos),
            "total_videos": len(videos), "completed_videos": 0, "failed_videos": 0,
            "active_slots": 0, "max_concurrent": max(1, int(cfg["max_concurrent"])),
            "base_opts": base_opts, "mode": "retry",
            "url": row.get("url") or "", "title": f"Retry · {row.get('title') or job_id}",
            "quality": quality, "section": row.get("section") or "",
            "date_after": "", "date_before": "", "out_dir": str(out),
            "origin_job_id": job_id,
        }
    # Do not persist the temporary retry job: its results are merged back
    # into the original entry when it finishes.

    def _run():
        success = False
        try:
            run_pool(new_id, videos, base_opts, ext)
            success = True
        except Exception as exc:
            with jobs_lock:
                jobs[new_id]["error"] = str(exc)
        finally:
            with jobs_lock:
                j = jobs[new_id]
                j["done"] = True
                j["status"] = ("done" if j.get("failed_videos", 0) == 0 and success
                               else "error")
                j["log"].append("Retry finished.")
            _merge_retry_results(job_id, new_id, videos)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"jobId": new_id})


def pget(d, k, default=""):
    return (d or {}).get(k, default)


# ── Stats / storage ─────────────────────────────────────────────────────────
@app.route("/api/stats")
def stats():
    _purge_old_jobs_db()
    _init_db()
    with _db() as db:
        total_jobs = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        video_rows = db.execute("SELECT status, COUNT(*) c, SUM(COALESCE(filesize,0)) s "
                                "FROM videos GROUP BY status").fetchall()
        mode_rows = db.execute("SELECT mode, COUNT(*) c FROM jobs GROUP BY mode").fetchall()
        recent = db.execute("SELECT ts, completed, failed, total FROM jobs WHERE ts > ?",
                            (int((time.time() - 14 * 86400) * 1000),)).fetchall()
    by_status, total_videos, total_bytes = {}, 0, 0
    for status, c, s in video_rows:
        by_status[status] = c
        total_videos += c
        total_bytes += int(s or 0)
    modes = {m: c for m, c in mode_rows}
    daily = {}
    for i in range(14):
        d = (datetime.now() - timedelta(days=13 - i)).strftime("%Y-%m-%d")
        daily[d] = 0
    for ts, completed, failed, total in recent:
        try:
            d = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
        except Exception:
            continue
        if d in daily:
            daily[d] += int(completed or 0) or 0
    cfg = get_cfg()
    dl_dir = Path(cfg["download_dir"]).expanduser()
    try:
        usage = shutil.disk_usage(dl_dir)
        disk = {"free": usage.free, "total": usage.total}
    except Exception:
        disk = {"free": 0, "total": 0}
    return jsonify({
        "total_jobs": total_jobs, "total_videos": total_videos,
        "done": by_status.get("done", 0), "failed": by_status.get("error", 0),
        "total_bytes": total_bytes, "modes": modes,
        "activity": [{"date": d, "count": c} for d, c in daily.items()],
        "disk": disk,
    })


# ── Open files / folders ──────────────────────────────────────────────────────
def _within_download_dir(path: Path) -> bool:
    try:
        base = Path(get_cfg()["download_dir"]).expanduser().resolve()
        p = path.resolve()
        return p == base or base in p.parents
    except Exception:
        return False


def _open_in_os(target: Path, reveal: bool):
    system = platform.system()
    if system == "Linux" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("No desktop session available on this machine")
    if system == "Darwin":
        cmd = ["open", "-R", str(target)] if reveal else ["open", str(target)]
    elif system == "Windows":
        cmd = ["explorer", f"/select,{target}"] if reveal else ["explorer", str(target)]
    else:
        cmd = ["xdg-open", str(target.parent if reveal else target)]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@app.route("/api/open", methods=["POST"])
def open_path():
    data = request.json or {}
    job_id = data.get("jobId")
    video_id = data.get("videoId")
    reveal = bool(data.get("reveal"))
    target = None

    if job_id:
        with jobs_lock:
            job = jobs.get(job_id)
            job = copy.deepcopy(job) if job else None
        if job and video_id:
            vid = next((v for v in job.get("videos", []) if v["id"] == video_id), None)
            target = Path(vid["filepath"]) if vid and vid.get("filepath") else None
        elif job and job.get("out_dir"):
            target = Path(job["out_dir"])
        if target is None:
            row = _history_job(job_id)
            if row:
                if video_id:
                    vid = next((v for v in row.get("videos", [])
                                if v["video_id"] == video_id), None)
                    target = Path(vid["filepath"]) if vid and vid.get("filepath") else None
                elif row.get("out_dir"):
                    target = Path(row["out_dir"])
    elif data.get("path"):
        target = Path(os.path.expanduser(str(data["path"]).strip()))

    if target is None:
        return jsonify({"error": "Nothing to open"}), 400
    if not _within_download_dir(target):
        return jsonify({"error": "Path is outside the download folder"}), 403
    if not target.exists():
        return jsonify({"error": f"Not found: {target}"}), 404
    if target.is_dir() and reveal:
        reveal = False
    try:
        _open_in_os(target, reveal)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"success": True})


if __name__ == "__main__":
    Path(settings["download_dir"]).expanduser().mkdir(parents=True, exist_ok=True)
    _merge_legacy_retries()
    port = int(os.environ.get("YT_PORT", "8080"))
    host = os.environ.get("YT_HOST", "127.0.0.1")
    print(f"\n  ytdpro is running!")
    print(f"  Saving to: {settings['download_dir']}")
    if host != "127.0.0.1":
        print("  Warning: the UI has no authentication and is reachable from your network.")
        print("           Set YT_HOST=127.0.0.1 to restrict it to this machine.")
    print(f"  Open http://localhost:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
