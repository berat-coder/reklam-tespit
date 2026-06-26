import os
import json
import sqlite3
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = Path(os.environ.get("DATA_DIR", Path(__file__).parent.parent)) / "data.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    channel_logos TEXT NOT NULL DEFAULT '[]',
    main_sponsors TEXT NOT NULL DEFAULT '[]',
    sponsor_active_only TEXT NOT NULL DEFAULT '[]',
    brand_aliases TEXT NOT NULL DEFAULT '{}',
    ignored_brands TEXT NOT NULL DEFAULT '[]',
    rule_state TEXT NOT NULL DEFAULT '{}',
    avatar_url   TEXT NOT NULL DEFAULT '',
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
    persistent_overlays TEXT NOT NULL DEFAULT '[]',
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
    manual_clean INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (video_id) REFERENCES videos(id)
);

CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at    TEXT
);
"""


# ── Kullanıcılar (çoklu giriş) ─────────────────────────────────────────────────

def create_user(username, password):
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("Kullanıcı adı ve şifre gerekli")
    with get_db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise ValueError("Bu kullanıcı adı zaten var")
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), datetime.utcnow().isoformat()),
        )


def verify_user(username, password):
    with get_db() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
    return bool(row) and check_password_hash(row["password_hash"], password)


def list_users():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT username, created_at FROM users ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_user(username):
    with get_db() as conn:
        conn.execute("DELETE FROM users WHERE username = ?", ((username or "").strip(),))


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
        # Additive migration'lar — eski data.db'ler için (sütun varsa sessizce geç)
        for stmt in (
            "ALTER TABLE channels ADD COLUMN avatar_url TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE channels ADD COLUMN main_sponsors TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE channels ADD COLUMN sponsor_active_only TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE channels ADD COLUMN brand_aliases TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE channels ADD COLUMN ignored_brands TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE channels ADD COLUMN rule_state TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE videos ADD COLUMN persistent_overlays TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE detections ADD COLUMN manual_clean INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                conn.execute(stmt)
            except Exception:
                pass


# ── Kanal ─────────────────────────────────────────────────────────────────────

def get_channel(ch_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM channels WHERE id = ?", (ch_id,)).fetchone()
        return _ch(row) if row else None


def upsert_channel(ch_id, name="", url="", channel_logos=None, avatar_url="", last_scanned=None):
    logos = json.dumps(channel_logos or [], ensure_ascii=False)
    with get_db() as conn:
        conn.execute("""
            INSERT INTO channels (id, name, url, channel_logos, avatar_url, last_scanned)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name         = COALESCE(NULLIF(excluded.name, ''), channels.name),
                url          = COALESCE(NULLIF(excluded.url, ''),  channels.url),
                channel_logos = excluded.channel_logos,
                avatar_url   = COALESCE(NULLIF(excluded.avatar_url, ''), channels.avatar_url),
                last_scanned = COALESCE(excluded.last_scanned, channels.last_scanned)
        """, (ch_id, name, url, logos, avatar_url, last_scanned))


def update_channel_logos(ch_id, logos):
    with get_db() as conn:
        conn.execute(
            "UPDATE channels SET channel_logos = ? WHERE id = ?",
            (json.dumps(logos, ensure_ascii=False), ch_id),
        )


_FLAG_COL = {
    "channel_logo": "channel_logos",
    "main_sponsor": "main_sponsors",
    "active_only": "sponsor_active_only",
}


def set_channel_brand_flag(ch_id, marka, flag, value):
    """Bir markayı kanal logosu / ana sponsor / 'sadece aktif reklam' olarak
    işaretler/kaldırır.
    flag: 'channel_logo' | 'main_sponsor' | 'active_only'  ·  value: True/False"""
    col = _FLAG_COL.get(flag)
    if not col:
        raise ValueError("Geçersiz flag")
    marka = (marka or "").strip()
    if not marka:
        raise ValueError("marka gerekli")
    ch = get_channel(ch_id) or {}
    field = {"channel_logos": "channel_logos", "main_sponsors": "main_sponsors",
             "sponsor_active_only": "sponsor_active_only"}[col]
    cur = ch.get(field, [])
    key = marka.casefold()
    others = [m for m in cur if m.casefold() != key]
    new_list = (others + [marka]) if value else others
    with get_db() as conn:
        conn.execute(
            f"UPDATE channels SET {col} = ? WHERE id = ?",
            (json.dumps(new_list, ensure_ascii=False), ch_id),
        )
    return new_list


# ── Kanal bazlı öğrenilen kurallar (alias / yok say / öneri) ───────────────────

def _ckey(s):
    return (s or "").strip().casefold()


def add_brand_alias(ch_id, src, dst):
    """Yeniden adlandırma kuralı: src markası → dst (hemen aktif)."""
    src, dst = (src or "").strip(), (dst or "").strip()
    if not src or not dst or _ckey(src) == _ckey(dst):
        return
    ch = get_channel(ch_id) or {}
    aliases = ch.get("brand_aliases", {})
    aliases[_ckey(src)] = {"to": dst}
    with get_db() as conn:
        conn.execute("UPDATE channels SET brand_aliases = ? WHERE id = ?",
                     (json.dumps(aliases, ensure_ascii=False), ch_id))


def bump_ignore_and_maybe_suggest(ch_id, marka, threshold=2):
    """'Reklam değil' sayacını artır; eşik dolunca öneri oluştur (zaten aktif/önerili değilse)."""
    key = _ckey(marka)
    if not key:
        return
    ch = get_channel(ch_id) or {}
    if key in {_ckey(x) for x in ch.get("ignored_brands", [])}:
        return  # zaten aktif
    rs = ch.get("rule_state", {}) or {}
    counts = rs.get("ignore_counts", {})
    counts[key] = counts.get(key, 0) + 1
    sugg = rs.get("suggestions", [])
    if counts[key] >= threshold and not any(_ckey(s.get("marka")) == key for s in sugg):
        sugg.append({"type": "ignore", "marka": (marka or "").strip(), "count": counts[key]})
    rs["ignore_counts"] = counts
    rs["suggestions"] = sugg
    with get_db() as conn:
        conn.execute("UPDATE channels SET rule_state = ? WHERE id = ?",
                     (json.dumps(rs, ensure_ascii=False), ch_id))


def approve_suggestion(ch_id, marka):
    """Öneriyi aktif 'yok say' kuralına çevirir."""
    key = _ckey(marka)
    ch = get_channel(ch_id) or {}
    ignored = ch.get("ignored_brands", [])
    if key not in {_ckey(x) for x in ignored}:
        ignored.append((marka or "").strip())
    rs = ch.get("rule_state", {}) or {}
    rs["suggestions"] = [s for s in rs.get("suggestions", []) if _ckey(s.get("marka")) != key]
    with get_db() as conn:
        conn.execute(
            "UPDATE channels SET ignored_brands = ?, rule_state = ? WHERE id = ?",
            (json.dumps(ignored, ensure_ascii=False), json.dumps(rs, ensure_ascii=False), ch_id))


def reject_suggestion(ch_id, marka):
    key = _ckey(marka)
    ch = get_channel(ch_id) or {}
    rs = ch.get("rule_state", {}) or {}
    rs["suggestions"] = [s for s in rs.get("suggestions", []) if _ckey(s.get("marka")) != key]
    # tekrar önermesin diye sayacı sıfırla
    counts = rs.get("ignore_counts", {})
    counts.pop(key, None)
    rs["ignore_counts"] = counts
    with get_db() as conn:
        conn.execute("UPDATE channels SET rule_state = ? WHERE id = ?",
                     (json.dumps(rs, ensure_ascii=False), ch_id))


def remove_rule(ch_id, kind, key):
    """Aktif kuralı sil. kind: 'alias' | 'ignore'."""
    ch = get_channel(ch_id) or {}
    if kind == "alias":
        aliases = ch.get("brand_aliases", {})
        aliases.pop(_ckey(key), None)
        with get_db() as conn:
            conn.execute("UPDATE channels SET brand_aliases = ? WHERE id = ?",
                         (json.dumps(aliases, ensure_ascii=False), ch_id))
    elif kind == "ignore":
        ignored = [x for x in ch.get("ignored_brands", []) if _ckey(x) != _ckey(key)]
        with get_db() as conn:
            conn.execute("UPDATE channels SET ignored_brands = ? WHERE id = ?",
                         (json.dumps(ignored, ensure_ascii=False), ch_id))


def get_channel_rules(ch_id):
    ch = get_channel(ch_id) or {}
    return {
        "aliases": ch.get("brand_aliases", {}),
        "ignored": ch.get("ignored_brands", []),
        "suggestions": (ch.get("rule_state", {}) or {}).get("suggestions", []),
        "channel_logos": ch.get("channel_logos", []),
        "main_sponsors": ch.get("main_sponsors", []),
        "sponsor_active_only": ch.get("sponsor_active_only", []),
    }


def edit_brand_global(video_id, action, marka, new_marka=None):
    """Bir videodaki TÜM karelerde bir markayı yeniden adlandırır veya kaldırır.
    action: 'rename' | 'remove'. Etkilenen satırları tek tek günceller
    (manual_clean korunur ve düzenlenen satırlara işaretlenir)."""
    key = (marka or "").strip().casefold()
    if not key:
        raise ValueError("marka gerekli")
    if action == "rename":
        new_marka = (new_marka or "").strip()
        if not new_marka:
            raise ValueError("yeni marka gerekli")

    dets = get_detections(video_id)
    with get_db() as conn:
        for d in dets:
            tespitler = d.get("tespitler", []) or []
            markalar = d.get("markalar", []) or []
            has = (any((t.get("marka") or "").strip().casefold() == key for t in tespitler)
                   or any((m or "").strip().casefold() == key for m in markalar))
            if not has:
                continue

            if action == "rename":
                for t in tespitler:
                    if (t.get("marka") or "").strip().casefold() == key:
                        t["marka"] = new_marka
                markalar = [new_marka if (m or "").strip().casefold() == key else m for m in markalar]
            else:  # remove
                tespitler = [t for t in tespitler if (t.get("marka") or "").strip().casefold() != key]
                markalar = [m for m in markalar if (m or "").strip().casefold() != key]

            markalar = list(dict.fromkeys(m for m in markalar if (m or "").strip()))
            reklam_var = 1 if (tespitler or markalar) else 0
            conn.execute("""
                UPDATE detections
                SET reklam_var = ?, markalar = ?, tespitler = ?, manual_clean = 1
                WHERE video_id = ? AND idx = ?
            """, (
                int(reklam_var),
                json.dumps(markalar, ensure_ascii=False),
                json.dumps(tespitler, ensure_ascii=False),
                video_id, d["index"],
            ))


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
                 desc_brands=None, persistent_overlays=None, completed=False):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO videos (
                id, channel_id, title, url, duration, thumbnail, analyzed_at,
                total_frames, api_calls, ad_frame_count,
                type_counts, brand_counts, desc_brands, persistent_overlays, completed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                persistent_overlays = excluded.persistent_overlays,
                completed     = excluded.completed
        """, (
            video_id, channel_id, title, url, duration, thumbnail,
            analyzed_at, total_frames, api_calls, ad_frame_count,
            json.dumps(type_counts or {}, ensure_ascii=False),
            json.dumps(brand_counts or {}, ensure_ascii=False),
            json.dumps(desc_brands or [], ensure_ascii=False),
            json.dumps(persistent_overlays or [], ensure_ascii=False),
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


def get_detection(video_id, index):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM detections WHERE video_id = ? AND idx = ?",
            (video_id, index),
        ).fetchone()
        return _det(row) if row else None


def _rebuild_markalar(tespitler):
    return list(dict.fromkeys(
        (t.get("marka") or "").strip()
        for t in tespitler if (t.get("marka") or "").strip()))


def update_detection(video_id, index, action, tespit_index=None, marka=None,
                     tur=None, konum=None, detay=None):
    """Tek bir tespit satırına manuel düzeltme uygular (tek UPDATE).
    action: mark_clean | remove_tespit | remove_brand | add_tespit
    ValueError fırlatır → çağıran 400 döndürür."""
    det = get_detection(video_id, index)
    if det is None:
        raise ValueError("Tespit bulunamadı")

    tespitler = det.get("tespitler", []) or []

    if action == "add_tespit":
        marka_v = (marka or "").strip()
        tur_v = (tur or "").strip() or "Reklam"
        if not marka_v and not (detay or "").strip():
            raise ValueError("En az marka veya açıklama gerekli")
        tespitler = tespitler + [{
            "tur": tur_v, "marka": marka_v,
            "konum": (konum or "").strip(), "detay": (detay or "").strip(),
        }]
        markalar = _rebuild_markalar(tespitler)
        reklam_var = 1
        guven = "Yüksek"  # manuel ekleme = kesin
    elif action == "mark_clean":
        tespitler, markalar, reklam_var, guven = [], [], 0, "Düşük"
    elif action == "remove_tespit":
        if tespit_index is None or not (0 <= tespit_index < len(tespitler)):
            raise ValueError("Geçersiz tespit_index")
        tespitler = [t for i, t in enumerate(tespitler) if i != tespit_index]
        markalar = _rebuild_markalar(tespitler)
        reklam_var = 1 if tespitler else 0
        guven = det.get("guven", "Düşük") if tespitler else "Düşük"
    elif action == "remove_brand":
        key = (marka or "").strip().casefold()
        if not key:
            raise ValueError("marka gerekli")
        tespitler = [t for t in tespitler if (t.get("marka") or "").strip().casefold() != key]
        markalar = _rebuild_markalar(tespitler)
        reklam_var = 1 if tespitler else 0
        guven = det.get("guven", "Düşük") if tespitler else "Düşük"
    else:
        raise ValueError(f"Bilinmeyen action: {action}")

    with get_db() as conn:
        conn.execute("""
            UPDATE detections
            SET reklam_var = ?, guven = ?, markalar = ?, tespitler = ?, manual_clean = 1
            WHERE video_id = ? AND idx = ?
        """, (
            int(reklam_var), guven,
            json.dumps(markalar, ensure_ascii=False),
            json.dumps(tespitler, ensure_ascii=False),
            video_id, index,
        ))


def recompute_video_aggregates(video_id):
    """Mevcut tespitlerden video agregatlarını sıfırdan yeniden hesaplar ve kaydeder.
    Düzeltme/silme veya 'kanal logosu' işaretleme sonrası çağrılır. agg döner."""
    from services.aggregates import compute_aggregates
    v = get_video(video_id)
    if not v:
        return None
    ch = get_channel(v["channel_id"]) or {}
    detections = get_detections(video_id)
    agg = compute_aggregates(detections, ch.get("channel_logos", []),
                             ch.get("main_sponsors", []),
                             ch.get("sponsor_active_only", []),
                             brand_aliases=ch.get("brand_aliases", {}),
                             ignored_brands=ch.get("ignored_brands", []))
    upsert_video(
        video_id=video_id,
        channel_id=v["channel_id"],
        total_frames=v.get("total_frames", 0),   # koşulsuz yazılır → koru
        api_calls=v.get("api_calls", 0),          # koşulsuz yazılır → koru
        ad_frame_count=agg["ad_frame_count"],
        type_counts=agg["type_counts"],
        brand_counts=agg["brand_counts"],
        persistent_overlays=agg["persistent_overlays"],
        completed=v.get("completed", True),
    )
    return agg


def get_recent_videos(limit=10):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT v.id, v.title, v.thumbnail, v.ad_frame_count, v.analyzed_at,
                   v.channel_id, c.name AS channel_name, c.avatar_url
            FROM videos v
            LEFT JOIN channels c ON c.id = v.channel_id
            WHERE v.completed = 1
            ORDER BY v.analyzed_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


# ── Global / kanallar arası toplama (Sponsorluk İstihbarat Paneli) ─────────────

def get_all_videos(completed_only=True):
    with get_db() as conn:
        q = """
            SELECT v.*, c.name AS channel_name, c.avatar_url AS channel_avatar
            FROM videos v
            LEFT JOIN channels c ON c.id = v.channel_id
        """
        if completed_only:
            q += " WHERE v.completed = 1"
        q += " ORDER BY v.analyzed_at DESC"
        rows = conn.execute(q).fetchall()
        out = []
        for r in rows:
            d = _vid(r)
            d["channel_name"] = r["channel_name"] or d["channel_id"]
            d["channel_avatar"] = r["channel_avatar"] or ""
            out.append(d)
        return out


def get_dashboard_data(since=None):
    """Tüm tamamlanmış videolardan kanallar arası özet üretir.
    since: ISO tarih string'i — bu tarihten sonra analiz edilenler (analyzed_at)."""
    videos = get_all_videos(completed_only=True)
    if since:
        videos = [v for v in videos if (v.get("analyzed_at") or "") >= since]
    channels = {}
    brand_acc = {}   # key -> {marka, count, videos:set, channels:set}
    type_totals = {}
    total_ads = 0

    for v in videos:
        cid = v["channel_id"]
        total_ads += v.get("ad_frame_count", 0)
        ch = channels.get(cid)
        if ch is None:
            ch = channels[cid] = {
                "id": cid, "name": v.get("channel_name", cid),
                "avatar": v.get("channel_avatar", ""),
                "video_count": 0, "ad_frame_count": 0, "_brands": {},
            }
        ch["video_count"] += 1
        ch["ad_frame_count"] += v.get("ad_frame_count", 0)

        for marka, count in (v.get("brand_counts") or {}).items():
            k = marka.strip().casefold()
            if not k:
                continue
            b = brand_acc.get(k)
            if b is None:
                b = brand_acc[k] = {"marka": marka, "count": 0,
                                    "videos": set(), "channels": set()}
            b["count"] += count
            b["videos"].add(v["id"])
            b["channels"].add(cid)
            ch["_brands"][marka] = ch["_brands"].get(marka, 0) + count

        for tur, count in (v.get("type_counts") or {}).items():
            type_totals[tur] = type_totals.get(tur, 0) + count

    top_brands = sorted(
        ({"marka": b["marka"], "count": b["count"],
          "video_count": len(b["videos"]), "channel_count": len(b["channels"])}
         for b in brand_acc.values()),
        key=lambda x: -x["count"],
    )
    top_channels = sorted((
        {"id": c["id"], "name": c["name"], "avatar": c["avatar"],
         "video_count": c["video_count"], "ad_frame_count": c["ad_frame_count"],
         "top_brand": (max(c["_brands"].items(), key=lambda x: x[1])[0]
                       if c["_brands"] else "")}
        for c in channels.values()
    ), key=lambda x: -x["ad_frame_count"])

    return {
        "totals": {
            "channels": len(channels),
            "videos": len(videos),
            "ad_frames": total_ads,
            "brands": len(brand_acc),
        },
        "top_brands": top_brands,
        "top_channels": top_channels,
        "type_totals": [{"name": t, "count": c}
                        for t, c in sorted(type_totals.items(), key=lambda x: -x[1])],
        "recent": get_recent_videos(8),
    }


def get_brand_appearances(marka):
    """Bir markanın tüm kanal/videolardaki görünümleri + haftalık trend."""
    key = (marka or "").strip().casefold()
    videos = get_all_videos(completed_only=True)
    out_videos = []
    channels = set()
    total = 0
    timeline = {}
    display = marka
    for v in videos:
        bc = v.get("brand_counts") or {}
        match = next((m for m in bc if m.strip().casefold() == key), None)
        if not match:
            continue
        cnt = bc[match]
        display = match
        total += cnt
        channels.add(v["channel_id"])
        out_videos.append({
            "video_id": v["id"], "title": v["title"],
            "channel_id": v["channel_id"], "channel_name": v.get("channel_name", ""),
            "thumbnail": v["thumbnail"], "analyzed_at": v["analyzed_at"],
            "count": cnt,
        })
        day = (v.get("analyzed_at") or "")[:10]
        if day:
            timeline[day] = timeline.get(day, 0) + cnt

    return {
        "marka": display,
        "total": total,
        "video_count": len(out_videos),
        "channel_count": len(channels),
        "videos": out_videos,
        "timeline": [{"date": d, "count": c} for d, c in sorted(timeline.items())],
    }


# ── Row → dict dönüştürücüler ─────────────────────────────────────────────────

def _ch(row):
    d = dict(row)
    return {
        "id": d["id"],
        "name": d["name"],
        "url": d["url"],
        "channel_logos": json.loads(d.get("channel_logos") or "[]"),
        "main_sponsors": json.loads(_col(d, "main_sponsors", None) or "[]"),
        "sponsor_active_only": json.loads(_col(d, "sponsor_active_only", None) or "[]"),
        "brand_aliases": json.loads(_col(d, "brand_aliases", None) or "{}"),
        "ignored_brands": json.loads(_col(d, "ignored_brands", None) or "[]"),
        "rule_state": json.loads(_col(d, "rule_state", None) or "{}"),
        "avatar_url": d.get("avatar_url", ""),
        "last_scanned": d.get("last_scanned"),
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
        "persistent_overlays": json.loads(_col(row, "persistent_overlays") or "[]"),
        "completed": bool(row["completed"]),
    }


def _col(row, key, default=None):
    """Eski şemalarda olmayabilecek sütunu güvenle okur."""
    try:
        v = row[key]
        return v if v is not None else default
    except (IndexError, KeyError):
        return default


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
        "manual_clean": bool(_col(row, "manual_clean", 0)),
    }
