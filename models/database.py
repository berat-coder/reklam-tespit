import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    channel_logos TEXT NOT NULL DEFAULT '[]',
    last_scanned TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    id            TEXT PRIMARY KEY,
    channel_id    TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    duration      INTEGER NOT NULL DEFAULT 0,
    thumbnail     TEXT NOT NULL DEFAULT '',
    analyzed_at   TEXT,
    total_frames  INTEGER NOT NULL DEFAULT 0,
    api_calls     INTEGER NOT NULL DEFAULT 0,
    ad_frame_count INTEGER NOT NULL DEFAULT 0,
    type_counts   TEXT NOT NULL DEFAULT '{}',
    brand_counts  TEXT NOT NULL DEFAULT '{}',
    desc_brands   TEXT NOT NULL DEFAULT '[]',
    completed     INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (channel_id) REFERENCES channels(id)
);

CREATE TABLE IF NOT EXISTS detections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id   TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    timestamp  TEXT NOT NULL DEFAULT '',
    seconds    REAL NOT NULL DEFAULT 0,
    frame_url  TEXT NOT NULL DEFAULT '',
    reklam_var INTEGER NOT NULL DEFAULT 0,
    guven      TEXT NOT NULL DEFAULT 'Düşük',
    markalar   TEXT NOT NULL DEFAULT '[]',
    tespitler  TEXT NOT NULL DEFAULT '[]',
    ozet       TEXT NOT NULL DEFAULT '',
    api_used   INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (video_id) REFERENCES videos(id)
);
"""


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript(_SCHEMA)


# ── Kanal ─────────────────────────────────────────────────────────────────────

def get_channel(ch_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM channels WHERE id = ?", (ch_id,)).fetchone()
        return _ch(row) if row else None


def upsert_channel(ch_id, name="", url="", channel_logos=None, last_scanned=None):
    logos = json.dumps(channel_logos or [], ensure_ascii=False)
    with get_db() as conn:
        conn.execute("""
            INSERT INTO channels (id, name, url, channel_logos, last_scanned)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name         = COALESCE(NULLIF(excluded.name, ''), channels.name),
                url          = COALESCE(NULLIF(excluded.url, ''),  channels.url),
                channel_logos = excluded.channel_logos,
                last_scanned = COALESCE(excluded.last_scanned, channels.last_scanned)
        """, (ch_id, name, url, logos, last_scanned))


def update_channel_logos(ch_id, logos):
    with get_db() as conn:
        conn.execute(
            "UPDATE channels SET channel_logos = ? WHERE id = ?",
            (json.dumps(logos, ensure_ascii=False), ch_id),
        )


# ── Video ─────────────────────────────────────────────────────────────────────

def get_video(video_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        return _vid(row) if row else None


def get_channel_videos(ch_id, completed_only=False):
    with get_db() as conn:
        q = "SELECT * FROM videos WHERE channel_id = ?"
        params = [ch_id]
        if completed_only:
            q += " AND completed = 1"
        q += " ORDER BY analyzed_at DESC"
        return [_vid(r) for r in conn.execute(q, params).fetchall()]


def is_video_completed(video_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT completed FROM videos WHERE id = ?", (video_id,)
        ).fetchone()
        return bool(row and row["completed"])


def upsert_video(video_id, channel_id, title="", url="", duration=0,
                 thumbnail="", analyzed_at=None, total_frames=0, api_calls=0,
                 ad_frame_count=0, type_counts=None, brand_counts=None,
                 desc_brands=None, completed=False):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO videos (
                id, channel_id, title, url, duration, thumbnail, analyzed_at,
                total_frames, api_calls, ad_frame_count,
                type_counts, brand_counts, desc_brands, completed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title         = COALESCE(NULLIF(excluded.title, ''),     videos.title),
                url           = COALESCE(NULLIF(excluded.url, ''),       videos.url),
                duration      = COALESCE(NULLIF(excluded.duration, 0),   videos.duration),
                thumbnail     = COALESCE(NULLIF(excluded.thumbnail, ''), videos.thumbnail),
                analyzed_at   = COALESCE(excluded.analyzed_at,           videos.analyzed_at),
                total_frames  = excluded.total_frames,
                api_calls     = excluded.api_calls,
                ad_frame_count = excluded.ad_frame_count,
                type_counts   = excluded.type_counts,
                brand_counts  = excluded.brand_counts,
                desc_brands   = COALESCE(excluded.desc_brands,           videos.desc_brands),
                completed     = excluded.completed
        """, (
            video_id, channel_id, title, url, duration, thumbnail,
            analyzed_at, total_frames, api_calls, ad_frame_count,
            json.dumps(type_counts or {}, ensure_ascii=False),
            json.dumps(brand_counts or {}, ensure_ascii=False),
            json.dumps(desc_brands or [], ensure_ascii=False),
            int(completed),
        ))


# ── Detection ─────────────────────────────────────────────────────────────────

def save_detections(video_id, detections):
    with get_db() as conn:
        conn.execute("DELETE FROM detections WHERE video_id = ?", (video_id,))
        conn.executemany("""
            INSERT INTO detections (
                video_id, idx, timestamp, seconds, frame_url,
                reklam_var, guven, markalar, tespitler, ozet, api_used
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            (
                video_id,
                d.get("index", i),
                d.get("timestamp", ""),
                d.get("seconds", 0.0),
                d.get("frame_url", ""),
                int(d.get("reklam_var", False)),
                d.get("guven", "Düşük"),
                json.dumps(d.get("markalar", []), ensure_ascii=False),
                json.dumps(d.get("tespitler", []), ensure_ascii=False),
                d.get("ozet", ""),
                int(d.get("_api_used", True)),
            )
            for i, d in enumerate(detections)
        ])


def get_detections(video_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM detections WHERE video_id = ? ORDER BY idx",
            (video_id,),
        ).fetchall()
        return [_det(r) for r in rows]


# ── Row → dict dönüştürücüler ─────────────────────────────────────────────────

def _ch(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "channel_logos": json.loads(row["channel_logos"] or "[]"),
        "last_scanned": row["last_scanned"],
    }


def _vid(row):
    return {
        "id": row["id"],
        "channel_id": row["channel_id"],
        "title": row["title"],
        "url": row["url"],
        "duration": row["duration"],
        "thumbnail": row["thumbnail"],
        "analyzed_at": row["analyzed_at"],
        "total_frames": row["total_frames"],
        "api_calls": row["api_calls"],
        "ad_frame_count": row["ad_frame_count"],
        "type_counts": json.loads(row["type_counts"] or "{}"),
        "brand_counts": json.loads(row["brand_counts"] or "{}"),
        "desc_brands": json.loads(row["desc_brands"] or "[]"),
        "completed": bool(row["completed"]),
    }


def _det(row):
    return {
        "index": row["idx"],
        "timestamp": row["timestamp"],
        "seconds": row["seconds"],
        "frame_url": row["frame_url"],
        "reklam_var": bool(row["reklam_var"]),
        "guven": row["guven"],
        "markalar": json.loads(row["markalar"] or "[]"),
        "tespitler": json.loads(row["tespitler"] or "[]"),
        "ozet": row["ozet"],
        "_api_used": bool(row["api_used"]),
    }
