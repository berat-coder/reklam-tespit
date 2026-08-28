import os
import json
import sqlite3
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash

# DATABASE_URL varsa PostgreSQL, yoksa yerel SQLite (kurulum gerektirmez)
DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_PG = DATABASE_URL.startswith("postgres")
DB_PATH = Path(os.environ.get("DATA_DIR", Path(__file__).parent.parent)) / "data.db"

# Otomatik artan birincil anahtar (detections.id) lehçeye göre
_AUTO_ID = "BIGSERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"

_SCHEMA = ("""
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
    id         __AUTO_ID__,
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

CREATE TABLE IF NOT EXISTS live_seen (
    video_id   TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    seen_at    TEXT,
    analyzed   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app_kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS scan_log (
    id      __AUTO_ID__,
    ts      TEXT,
    kind    TEXT NOT NULL DEFAULT '',
    target  TEXT NOT NULL DEFAULT '',
    status  TEXT NOT NULL DEFAULT 'info',
    code    TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT ''
);
""").replace("__AUTO_ID__", _AUTO_ID)

# Additive migration'lar (her iki lehçe) — (tablo, sütun, tip-tanım)
_MIGRATIONS = [
    ("channels", "avatar_url", "TEXT NOT NULL DEFAULT ''"),
    ("channels", "main_sponsors", "TEXT NOT NULL DEFAULT '[]'"),
    ("channels", "sponsor_active_only", "TEXT NOT NULL DEFAULT '[]'"),
    ("channels", "brand_aliases", "TEXT NOT NULL DEFAULT '{}'"),
    ("channels", "ignored_brands", "TEXT NOT NULL DEFAULT '[]'"),
    ("channels", "rule_state", "TEXT NOT NULL DEFAULT '{}'"),
    ("videos", "persistent_overlays", "TEXT NOT NULL DEFAULT '[]'"),
    ("detections", "manual_clean", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "role", "TEXT NOT NULL DEFAULT 'user'"),
    ("channels", "auto_main_sponsors", "TEXT NOT NULL DEFAULT '[]'"),
    ("live_seen", "status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("live_seen", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("live_seen", "last_attempt", "TEXT"),
    ("live_seen", "error", "TEXT NOT NULL DEFAULT ''"),
    # Süre/olay modeli: marka başına görünürlük saniyesi, olay sayısı, tür ve
    # belirginlik. Panel/trend/EMV bunları okur — okuma anında tüm videolar için
    # yeniden hesaplamak pahalı olurdu.
    # {"Nesine": {"sec": 2280.0, "app": 1, "kind": "sponsorluk", "prom": 0.35}}
    ("videos", "brand_exposure", "TEXT NOT NULL DEFAULT '{}'"),
    # Kanal başına saniye başına medya değeri (TL). 0 → global varsayılan.
    ("channels", "emv_rate", "TEXT NOT NULL DEFAULT ''"),
    # Canlı-bekle akışı: yayın SÜRERKEN analiz etmek yerine bitmesini bekle.
    # wait_since: live_wait durumuna geçiş anı (TTL için), last_check/check_count:
    # "bitti mi" kontrol takibi.
    ("live_seen", "wait_since", "TEXT"),
    ("live_seen", "last_check", "TEXT"),
    ("live_seen", "check_count", "INTEGER NOT NULL DEFAULT 0"),
    # Canlı yayın sürerken yapılan ~10 dk'lık kısmi önizleme analizi işareti.
    # is_partial=1 videolar istatistiklere girmez ve tam analiz üstüne yazar.
    ("videos", "is_partial", "INTEGER NOT NULL DEFAULT 0"),
    # 2. model doğrulaması (yanlış pozitif avcısı): ''|confirmed|rejected|uncertain|error
    ("detections", "verify_status", "TEXT NOT NULL DEFAULT ''"),
    ("detections", "verify_notes", "TEXT NOT NULL DEFAULT ''"),
]


# ── Kullanıcılar (çoklu giriş) ─────────────────────────────────────────────────

def create_user(username, password, role="user"):
    username = (username or "").strip()
    role = "admin" if (role or "").strip().lower() == "admin" else "user"
    if not username or not password:
        raise ValueError("Kullanıcı adı ve şifre gerekli")
    with get_db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise ValueError("Bu kullanıcı adı zaten var")
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at, role) "
            "VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password),
             datetime.utcnow().isoformat(), role),
        )


def get_user_role(username):
    """DB kullanıcısının rolü ('admin'|'user'); yoksa 'user'."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT role FROM users WHERE username = ?", ((username or "").strip(),)
        ).fetchone()
    if not row:
        return "user"
    return _col(row, "role", "user") or "user"


def verify_user(username, password):
    with get_db() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
    return bool(row) and check_password_hash(row["password_hash"], password)


def list_users():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT username, created_at, role FROM users ORDER BY created_at"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["role"] = _col(r, "role", "user") or "user"
            out.append(d)
        return out


def delete_user(username):
    with get_db() as conn:
        conn.execute("DELETE FROM users WHERE username = ?", ((username or "").strip(),))


# ── Canlı yayın dedup (gece otomatik taraması) ─────────────────────────────────

def is_live_seen(video_id):
    """Bu canlı yayın daha önce keşfedilip kuyruğa alındı mı?"""
    if not video_id:
        return False
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM live_seen WHERE video_id = ?", (video_id,)
        ).fetchone()
        return bool(row)


def mark_live_seen(video_id, channel_id="", title="", url="", analyzed=False):
    """Keşifte bir canlı yayını 'görüldü' olarak kaydet (idempotent upsert).
    Yeni satır → status='pending'. Mevcut satırın durumuna dokunmaz.
    analyzed=True (manuel tarama) → doğrudan 'done' işaretle."""
    if not video_id:
        return
    st = "done" if analyzed else "pending"
    with get_db() as conn:
        conn.execute("""
            INSERT INTO live_seen (video_id, channel_id, title, url, seen_at, analyzed, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                channel_id = COALESCE(NULLIF(excluded.channel_id, ''), live_seen.channel_id),
                title      = COALESCE(NULLIF(excluded.title, ''),      live_seen.title),
                url        = COALESCE(NULLIF(excluded.url, ''),        live_seen.url),
                analyzed   = CASE WHEN excluded.analyzed = 1 THEN 1 ELSE live_seen.analyzed END,
                status     = CASE WHEN excluded.analyzed = 1 THEN 'done' ELSE live_seen.status END
        """, (video_id, channel_id, title, url,
              datetime.utcnow().isoformat(), int(bool(analyzed)), st))


def mark_live_status(video_id, status, error="", inc_attempt=False):
    """Bir canlı yayının durumunu güncelle (satır varsa).
    status: pending|queued|done|failed|permanent. inc_attempt=True → deneme +1."""
    if not video_id:
        return
    now = datetime.utcnow().isoformat()
    analyzed = 1 if status == "done" else 0
    with get_db() as conn:
        if inc_attempt:
            conn.execute("""
                UPDATE live_seen
                SET status = ?, error = ?, last_attempt = ?,
                    attempts = attempts + 1, analyzed = ?
                WHERE video_id = ?
            """, (status, error or "", now, analyzed, video_id))
        else:
            conn.execute("""
                UPDATE live_seen SET status = ?, error = ?, analyzed = ?
                WHERE video_id = ?
            """, (status, error or "", analyzed, video_id))


def live_seen_ids():
    """Bilinen tüm canlı yayın id'leri (keşifte tekrar tarih sorgusunu önlemek için)."""
    with get_db() as conn:
        rows = conn.execute("SELECT video_id FROM live_seen").fetchall()
        return {r["video_id"] for r in rows}


def list_live_seen(since=None, limit=50):
    """Son görülen canlı yayınlar (panel için). since: ISO tarih filtresi.
    channel_name ve has_video (analiz gerçekten tamamlandı mı) join ile gelir —
    'analiz edildi' linki yalnızca has_video=1 iken gösterilmeli (404 önlemi)."""
    with get_db() as conn:
        q = """
            SELECT ls.*, c.name AS channel_name,
                   CASE WHEN v.id IS NOT NULL THEN 1 ELSE 0 END AS has_video,
                   v.ad_frame_count AS ad_frame_count
            FROM live_seen ls
            LEFT JOIN channels c ON c.id = ls.channel_id
            LEFT JOIN videos v ON v.id = ls.video_id AND v.completed = 1
        """
        params = []
        if since:
            q += " WHERE ls.seen_at >= ?"
            params.append(since)
        q += " ORDER BY ls.seen_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def _retry_cutoff(minutes=30):
    return (datetime.utcnow() - __import__("datetime").timedelta(minutes=minutes)).isoformat()


def next_pending_live(max_attempts=3):
    """Analize gönderilecek sıradaki canlı yayın:
    status='pending' VEYA (status='failed' & attempts<max & son deneme 30dk+ önce).
    FIFO (seen_at ASC) — en yeni önce olsaydı eski bekleyenler sıra alamadan
    budama TTL'ine takılıp sessizce siliniyordu."""
    cut = _retry_cutoff(30)
    with get_db() as conn:
        row = conn.execute("""
            SELECT * FROM live_seen
            WHERE status = 'pending'
               OR (status = 'failed' AND attempts < ?
                   AND (last_attempt IS NULL OR last_attempt < ?))
            ORDER BY seen_at ASC LIMIT 1
        """, (max_attempts, cut)).fetchone()
        return dict(row) if row else None


def requeue_stale_queued(minutes=90):
    """Worker ölürse 'queued' satırlar sonsuza dek takılı kalıyordu — hiçbir kod
    yolu onları tekrar 'pending' yapmıyordu. Son denemesi `minutes` dk'dan eski
    queued satırları yeniden kuyruğa alınabilir yap."""
    cut = _retry_cutoff(minutes)
    with get_db() as conn:
        cur = conn.execute("""
            UPDATE live_seen SET status = 'pending'
            WHERE status = 'queued'
              AND (last_attempt IS NULL OR last_attempt < ?)
        """, (cut,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def count_pending_live(max_attempts=3):
    cut = _retry_cutoff(30)
    with get_db() as conn:
        row = conn.execute("""
            SELECT COUNT(*) AS n FROM live_seen
            WHERE status = 'pending'
               OR (status = 'failed' AND attempts < ?
                   AND (last_attempt IS NULL OR last_attempt < ?))
        """, (max_attempts, cut)).fetchone()
        return int(row["n"]) if row else 0


def list_failed_live(limit=30):
    """Başarısız/kalıcı-hata canlı yayınlar (Durum paneli için)."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM live_seen WHERE status IN ('failed', 'permanent')
            ORDER BY last_attempt DESC NULLS LAST LIMIT ?
        """, (limit,)).fetchall() if IS_PG else conn.execute("""
            SELECT * FROM live_seen WHERE status IN ('failed', 'permanent')
            ORDER BY last_attempt DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def set_live_wait(video_id):
    """Satırı 'canlı — bitmesi bekleniyor' durumuna al. wait_since yalnızca ilk
    geçişte damgalanır (TTL sayacı sıfırlanmasın). 'done' satırlara dokunmaz."""
    if not video_id:
        return
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        conn.execute("""
            UPDATE live_seen
            SET status = 'live_wait',
                wait_since = COALESCE(wait_since, ?)
            WHERE video_id = ? AND status != 'done'
        """, (now, video_id))


def list_live_waits(recheck_min=45, limit=5):
    """'Bitti mi' kontrolü zamanı gelmiş canlı-bekle satırları (en eski bekleyen önce)."""
    cut = _retry_cutoff(recheck_min)
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM live_seen
            WHERE status = 'live_wait'
              AND (last_check IS NULL OR last_check < ?)
            ORDER BY wait_since ASC LIMIT ?
        """, (cut, limit)).fetchall()
        return [dict(r) for r in rows]


def mark_live_check(video_id):
    """Bir canlı-bekle satırının kontrol damgasını/sayacını güncelle."""
    with get_db() as conn:
        conn.execute("""
            UPDATE live_seen SET last_check = ?, check_count = check_count + 1
            WHERE video_id = ?
        """, (datetime.utcnow().isoformat(), video_id))


def count_waiting_live():
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM live_seen WHERE status = 'live_wait'").fetchone()
        return int(row["n"]) if row else 0


def prune_live_seen(hours=36):
    """Eski live_seen kayıtlarını DURUM-DUYARLI buda. Eski hali her şeyi 36 saatte
    siliyordu — analiz sırası gelmemiş 'pending' kayıtlar sessizce kayboluyordu.
    done → `hours` (36s; yeniden keşif is_video_completed ile zaten atlanır)
    failed/permanent → 7 gün (Durum panelinde görünür kalsın)
    pending/queued/live_wait → 72s (yalnızca emniyet supabı)"""
    _td = __import__("datetime").timedelta
    now = datetime.utcnow()
    cut_done = (now - _td(hours=hours)).isoformat()
    cut_err = (now - _td(days=7)).isoformat()
    cut_wait = (now - _td(hours=72)).isoformat()
    with get_db() as conn:
        conn.execute("""
            DELETE FROM live_seen WHERE seen_at IS NOT NULL AND (
                   (status = 'done' AND seen_at < ?)
                OR (status IN ('failed', 'permanent') AND seen_at < ?)
                OR (status NOT IN ('done', 'failed', 'permanent') AND seen_at < ?)
            )
        """, (cut_done, cut_err, cut_wait))


# ── Basit anahtar/değer durum deposu (scheduler runtime state) ─────────────────

def kv_get(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT v FROM app_kv WHERE k = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except (ValueError, TypeError):
        return default


def kv_set(key, value):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO app_kv (k, v) VALUES (?, ?)
            ON CONFLICT(k) DO UPDATE SET v = excluded.v
        """, (key, json.dumps(value, ensure_ascii=False)))


# ── Tarama/olay günlüğü + sağlık ───────────────────────────────────────────────

def log_event(kind, target="", status="info", code="", message=""):
    """Bir tarama/analiz olayını kaydet. kind: 'channel_scan'|'video'|'auto_tick'
    status: 'ok'|'error'|'info'. code: 'cookie_expired'|'video_unavailable'|..."""
    with get_db() as conn:
        conn.execute("""
            INSERT INTO scan_log (ts, kind, target, status, code, message)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (datetime.utcnow().isoformat(), kind, (target or "")[:300],
              status, code, (message or "")[:500]))
    # Ara sıra buda (log şişmesin)
    try:
        purge_scan_log(300)
    except Exception:
        pass


def get_scan_log(limit=100):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM scan_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def purge_scan_log(keep=300, keep_discover=60):
    """scan_log budaması — TÜRE DUYARLI.

    Eskiden düz "son N satırı tut" idi. Zamanlayıcının ürettiği 'discover'
    satırları (her tick × her kanal) logu sel gibi doldurduğu için kullanıcının
    MANUEL tarama sonucu 30 dakikada siliniyordu; "Durum" sayfasına baksa bile
    göremiyordu. Artık gürültülü 'discover' ayrı ve agresif budanır, anlamlı
    kayıtlar (kanal taraması, video analizi, hatalar) korunur."""
    with get_db() as conn:
        # 1) Gürültü: yalnız son `keep_discover` discover satırı kalsın
        conn.execute("""
            DELETE FROM scan_log WHERE kind = 'discover' AND id NOT IN (
                SELECT id FROM scan_log WHERE kind = 'discover'
                ORDER BY id DESC LIMIT ?
            )
        """, (keep_discover,))
        # 2) Anlamlı kayıtlar: kendi içinde son `keep` satır
        conn.execute("""
            DELETE FROM scan_log WHERE kind != 'discover' AND id NOT IN (
                SELECT id FROM scan_log WHERE kind != 'discover'
                ORDER BY id DESC LIMIT ?
            )
        """, (keep,))


def get_health():
    """scan_log'dan türetilmiş sistem sağlığı: cookie durumu, son başarı,
    son 24s hata sayısı, son hata."""
    day_ago = (datetime.utcnow() - __import__("datetime").timedelta(hours=24)).isoformat()
    with get_db() as conn:
        last_ok = conn.execute(
            "SELECT ts FROM scan_log WHERE status='ok' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_cookie = conn.execute(
            "SELECT ts FROM scan_log WHERE code='cookie_expired' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_quota = conn.execute(
            "SELECT ts FROM scan_log WHERE code='quota_daily' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        errs = conn.execute(
            "SELECT COUNT(*) AS n FROM scan_log WHERE status='error' AND ts >= ?",
            (day_ago,)
        ).fetchone()
        last_err = conn.execute(
            "SELECT ts, code, message, target FROM scan_log WHERE status='error' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    last_ok_ts = last_ok["ts"] if last_ok else None
    last_cookie_ts = last_cookie["ts"] if last_cookie else None
    last_quota_ts = last_quota["ts"] if last_quota else None
    # Cookie: son cookie hatasından sonra başarılı tarama olduysa tekrar OK sayılır
    cookie_ok = (last_cookie_ts is None) or (bool(last_ok_ts) and last_ok_ts > last_cookie_ts)
    # Gemini günlük kota: son 12 saatte kota hatası var ve sonrasında başarı yoksa
    # "tükenmiş" say (kota gece yarısı PT ≈ TSİ 10:00'da sıfırlanır).
    half_day_ago = (datetime.utcnow() - __import__("datetime").timedelta(hours=12)).isoformat()
    quota_exhausted = (bool(last_quota_ts) and last_quota_ts >= half_day_ago
                       and not (bool(last_ok_ts) and last_ok_ts > last_quota_ts))
    return {
        "cookie_ok": cookie_ok,
        "cookie_error_at": last_cookie_ts,
        "quota_exhausted": quota_exhausted,
        "quota_error_at": last_quota_ts,
        "last_success_iso": last_ok_ts,
        "errors_24h": int(errs["n"]) if errs else 0,
        "last_error": dict(last_err) if last_err else None,
    }


class _PGConn:
    """psycopg2 bağlantısını sqlite3'ün .execute() API'sine benzetir; '?'→'%s'."""
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        from psycopg2.extras import RealDictCursor
        cur = self._raw.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def executemany(self, sql, seq):
        cur = self._raw.cursor()
        cur.executemany(sql.replace("?", "%s"), list(seq))
        return cur

    def executescript(self, script):
        cur = self._raw.cursor()
        for stmt in script.split(";"):
            if stmt.strip():
                cur.execute(stmt)
        return cur

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


@contextmanager
def get_db():
    if IS_PG:
        import psycopg2
        conn = _PGConn(psycopg2.connect(DATABASE_URL))
    else:
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
        if IS_PG:
            for stmt in _SCHEMA.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            for t, c, typ in _MIGRATIONS:
                conn.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {c} {typ}")
        else:
            conn.executescript(_SCHEMA)
            for t, c, typ in _MIGRATIONS:
                try:
                    conn.execute(f"ALTER TABLE {t} ADD COLUMN {c} {typ}")
                except Exception:
                    pass
        # Backfill: eski live_seen kayıtlarında analyzed=1 → status='done'
        try:
            conn.execute(
                "UPDATE live_seen SET status = 'done' "
                "WHERE analyzed = 1 AND status = 'pending'")
        except Exception:
            pass


def migrate_sqlite_to_pg():
    """Tek seferlik: eski SQLite (DATA_DIR/data.db) verisini PostgreSQL'e kopyalar.
    Yalnız PG aktif, SQLite dosyası var ve PG boşsa çalışır (idempotent)."""
    if not IS_PG:
        return {"skipped": "Postgres aktif değil"}
    if not DB_PATH.exists():
        return {"skipped": "SQLite dosyası yok", "path": str(DB_PATH)}
    with get_db() as pg:
        n = pg.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"]
    if n and int(n) > 0:
        return {"skipped": "PG zaten dolu", "channels": int(n)}

    src = sqlite3.connect(str(DB_PATH))
    src.row_factory = sqlite3.Row
    counts = {}
    try:
        with get_db() as pg:
            # FK sırası: channels → videos → users → detections (id hariç, PG üretir)
            for table, skip in (("channels", ()), ("videos", ()),
                                ("users", ()), ("detections", ("id",))):
                try:
                    rows = src.execute(f"SELECT * FROM {table}").fetchall()
                except Exception:
                    counts[table] = 0
                    continue
                c = 0
                for r in rows:
                    d = {k: r[k] for k in r.keys() if k not in skip}
                    if not d:
                        continue
                    cols = list(d.keys())
                    ph = ",".join(["?"] * len(cols))
                    pg.execute(
                        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})",
                        tuple(d[k] for k in cols),
                    )
                    c += 1
                counts[table] = c
    finally:
        src.close()
    return {"migrated": counts}


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
    "auto_main_sponsor": "auto_main_sponsors",  # otomatik tespit kaydı (rozet için)
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
             "sponsor_active_only": "sponsor_active_only",
             "auto_main_sponsors": "auto_main_sponsors"}[col]
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


def get_live_streams_archive(limit=200):
    """Otomatik/canlı taramayla analiz edilmiş yayınlar (live_seen ∩ tamamlanmış
    videolar) — 'Canlı Yayınlar' sayfası için, en yeni analiz önce. Frontend
    bunları güne göre gruplar."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT v.id AS id, v.title AS title, v.thumbnail AS thumbnail,
                   v.ad_frame_count AS ad_frame_count, v.analyzed_at AS analyzed_at,
                   v.duration AS duration, v.channel_id AS channel_id,
                   c.name AS channel_name
            FROM live_seen ls
            JOIN videos v ON v.id = ls.video_id
            LEFT JOIN channels c ON c.id = v.channel_id
            WHERE v.completed = 1
            ORDER BY v.analyzed_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["channel_name"] = d.get("channel_name") or d.get("channel_id")
            out.append(d)
        return out


def list_recent_auto_scan_video_ids(hours=30):
    """Son `hours` saatte otomatik taramayla (live_seen) analiz edilmiş video
    id'leri — 'dün geceki taramaları sil' için."""
    cut = (datetime.utcnow() - __import__("datetime").timedelta(hours=hours)).isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT v.id AS id FROM videos v
            JOIN live_seen ls ON ls.video_id = v.id
            WHERE v.analyzed_at >= ?
        """, (cut,)).fetchall()
        return [r["id"] for r in rows]


def delete_video(video_id):
    """Bir videoyu/analizi TAMAMEN siler: tespitler + video kaydı + live_seen
    izi. Tekrar taranabilsin diye live_seen de temizlenir. (Yönetici işlemi.)"""
    if not video_id:
        return
    with get_db() as conn:
        conn.execute("DELETE FROM detections WHERE video_id = ?", (video_id,))
        conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        conn.execute("DELETE FROM live_seen WHERE video_id = ?", (video_id,))


def is_video_completed(video_id):
    """TAM analiz bitti mi? Canlı önizleme (is_partial=1) tamamlanmış SAYILMAZ —
    yayın bitince tam analiz üstüne yazabilsin."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT completed, is_partial FROM videos WHERE id = ?", (video_id,)
        ).fetchone()
        return bool(row and row["completed"] and not row["is_partial"])


def upsert_video(video_id, channel_id, title="", url="", duration=0,
                 thumbnail="", analyzed_at=None, total_frames=0, api_calls=0,
                 ad_frame_count=0, type_counts=None, brand_counts=None,
                 desc_brands=None, persistent_overlays=None, completed=False,
                 brand_exposure=None, is_partial=False):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO videos (
                id, channel_id, title, url, duration, thumbnail, analyzed_at,
                total_frames, api_calls, ad_frame_count,
                type_counts, brand_counts, desc_brands, persistent_overlays, completed,
                brand_exposure, is_partial
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                brand_exposure = excluded.brand_exposure,
                completed     = excluded.completed,
                is_partial    = excluded.is_partial
        """, (
            video_id, channel_id, title, url, duration, thumbnail,
            analyzed_at, total_frames, api_calls, ad_frame_count,
            json.dumps(type_counts or {}, ensure_ascii=False),
            json.dumps(brand_counts or {}, ensure_ascii=False),
            json.dumps(desc_brands or [], ensure_ascii=False),
            json.dumps(persistent_overlays or [], ensure_ascii=False),
            int(completed),
            json.dumps(brand_exposure or {}, ensure_ascii=False),
            int(is_partial),
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


def update_detection_verify(video_id, idx, status, notes="", guven=None):
    """2. model doğrulama sonucunu yaz. İnsan düzeltmesi (manual_clean) her zaman
    üstündür — o satırlara DOKUNMAZ. guven verilirse birlikte güncellenir
    (rejected → 'Düşük': MIN_CONFIDENCE filtresi sayımdan düşürür, kanıt kalır)."""
    with get_db() as conn:
        if guven:
            conn.execute("""
                UPDATE detections SET verify_status = ?, verify_notes = ?, guven = ?
                WHERE video_id = ? AND idx = ? AND COALESCE(manual_clean, 0) = 0
            """, (status, notes, guven, video_id, idx))
        else:
            conn.execute("""
                UPDATE detections SET verify_status = ?, verify_notes = ?
                WHERE video_id = ? AND idx = ? AND COALESCE(manual_clean, 0) = 0
            """, (status, notes, video_id, idx))


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


def _exposure_map(agg):
    """compute_aggregates çıktısındaki brand_report'u kompakt bir süre haritasına
    çevirir; panel/trend/EMV bunu okur (her okumada tüm videoları yeniden
    hesaplamak pahalı olurdu).
    {"Nesine": {"sec": 2280.0, "app": 1, "kind": "sponsorluk", "prom": 0.35}}"""
    out = {}
    for b in (agg or {}).get("brand_report") or []:
        out[b["marka"]] = {
            "sec": b.get("exposure_seconds", 0.0),
            "app": b.get("appearance_count", 0),
            "kind": b.get("kind", "spot"),
            "prom": b.get("prominence", 0.5),
        }
    return out


def emv_of(seconds, prominence, rate):
    """Tahmini medya değeri (TL) = süre × saniye ücreti × belirginlik.
    Belirginlik: tam ekran reklam, köşe logosundan değerlidir."""
    try:
        return round(float(seconds) * float(rate) * float(prominence), 2)
    except (TypeError, ValueError):
        return 0.0


def channel_emv_rate(ch):
    """Kanalın saniye başına TL değeri; tanımlı değilse global varsayılan."""
    from config import DEFAULT_EMV_RATE
    try:
        r = float((ch or {}).get("emv_rate") or 0)
    except (TypeError, ValueError):
        r = 0.0
    return r if r > 0 else DEFAULT_EMV_RATE


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
                             ignored_brands=ch.get("ignored_brands", []),
                             channel_name=ch.get("name", ""),
                             auto_main_sponsors=ch.get("auto_main_sponsors", []))
    upsert_video(
        video_id=video_id,
        channel_id=v["channel_id"],
        total_frames=v.get("total_frames", 0),   # koşulsuz yazılır → koru
        api_calls=v.get("api_calls", 0),          # koşulsuz yazılır → koru
        ad_frame_count=agg["ad_frame_count"],
        type_counts=agg["type_counts"],
        brand_counts=agg["brand_counts"],
        persistent_overlays=agg["persistent_overlays"],
        brand_exposure=_exposure_map(agg),
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
            # Canlı önizlemeler (is_partial) istatistiklere girmez — 10 dk'lık
            # örnek tam yayının verisi gibi görünüp panelleri çarpıtıyordu.
            q += " WHERE v.completed = 1 AND COALESCE(v.is_partial, 0) = 0"
        q += " ORDER BY v.analyzed_at DESC"
        rows = conn.execute(q).fetchall()
        out = []
        for r in rows:
            d = _vid(r)
            d["channel_name"] = r["channel_name"] or d["channel_id"]
            d["channel_avatar"] = r["channel_avatar"] or ""
            out.append(d)
        return out


def get_sponsor_matrix(since=None):
    """Marka → ana sponsoru olduğu kanallar (kanallar-arası sponsorluk matrisi).
    Örn: NESİNE → [NutSpor, Vole] (2 kanal). 'auto' = en az bir kanalda otomatik
    tespit edildi (uzamsal-zamansal). En çok kanala sahip marka önce.

    since verilirse yalnız o tarihten sonra analiz edilmiş videosu olan kanallar
    dahil edilir — panelin tarih filtresi matrise de uygulansın diye (eskiden
    filtre değişince üstteki blok sabit kalıyordu)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, main_sponsors, auto_main_sponsors FROM channels"
        ).fetchall()
        active_ids = None
        if since:
            active_ids = {
                r[0] for r in conn.execute(
                    "SELECT DISTINCT channel_id FROM videos "
                    "WHERE completed = 1 AND analyzed_at >= ?", (since,)).fetchall()
            }
    import re as _re
    _tr = str.maketrans("çğıöşü", "cgiosu")
    def _sk(s):
        return _re.sub(r"[^a-z0-9]", "", (s or "").casefold().translate(_tr))
    acc = {}
    for r in rows:
        d = dict(r)
        if active_ids is not None and d["id"] not in active_ids:
            continue
        cname = d.get("name") or d["id"]
        autos = {(x or "").casefold()
                 for x in json.loads(_col(d, "auto_main_sponsors", None) or "[]")}
        for m in json.loads(d.get("main_sponsors") or "[]"):
            # Eski hatalı kayıtlar: kanalın KENDİ ADI sponsor işaretlenmiş olabilir
            # ('NEO SPOR' → NEO Spor kanalı) — matrisi kirletmesin, atla.
            if _sk(m) and (_sk(m) == _sk(cname) or _sk(m) == _sk(d["id"])):
                continue
            e = acc.setdefault(m.casefold(), {"marka": m, "channels": [], "auto": False})
            # Aynı kanal iki kimlikle kayıtlıysa (@handle vs UC...) ada göre tekille
            if not any(c["name"].casefold() == cname.casefold() for c in e["channels"]):
                e["channels"].append({"id": d["id"], "name": cname})
            if m.casefold() in autos:
                e["auto"] = True
    return sorted(acc.values(), key=lambda x: -len(x["channels"]))


def _backfill_exposure(video_id):
    """Bir videonun süre/olay haritasını hesaplayıp YALNIZ brand_exposure
    kolonuna yazar (hedefli UPDATE — upsert_video diğer agregaları koşulsuz
    ezerdi). Gemini çağrısı yok; mevcut tespitlerden hesaplanır."""
    from services.aggregates import compute_aggregates
    v = get_video(video_id)
    if not v:
        return {}
    ch = get_channel(v["channel_id"]) or {}
    agg = compute_aggregates(get_detections(video_id), ch.get("channel_logos", []),
                             ch.get("main_sponsors", []),
                             ch.get("sponsor_active_only", []),
                             brand_aliases=ch.get("brand_aliases", {}),
                             ignored_brands=ch.get("ignored_brands", []),
                             channel_name=ch.get("name", ""),
                             auto_main_sponsors=ch.get("auto_main_sponsors", []))
    exp = _exposure_map(agg)
    with get_db() as conn:
        conn.execute("UPDATE videos SET brand_exposure = ? WHERE id = ?",
                     (json.dumps(exp, ensure_ascii=False), video_id))
    return exp


def get_intelligence(days=0):
    """Sponsorluk istihbarat katmanı — süre bazlı (brand_exposure) hesaplanır.

    Döner:
      brands   : marka bazında görünürlük, çıkış, pay, EMV, kanal/video sayısı
      matrix   : kanal × marka ısı haritası (rakip karşılaştırması)
      trend    : haftalık görünürlük eğrisi (marka bazında ilk 6)
      alerts   : yeni marka / kaybolan sponsor / ani değişim uyarıları
      totals   : toplam görünürlük ve EMV
    """
    from datetime import timedelta
    now = datetime.utcnow()
    since = (now - timedelta(days=days)).isoformat() if days else None
    # Uyarılar için bir önceki EŞİT uzunlukta dönem
    prev_since = (now - timedelta(days=days * 2)).isoformat() if days else None

    videos = get_all_videos(completed_only=True)
    ch_cache, ch_rate = {}, {}
    backfill_budget = 25      # istek başına en fazla bu kadar video doldurulur

    def _ch(cid):
        if cid not in ch_cache:
            c = get_channel(cid) or {}
            ch_cache[cid] = c
            ch_rate[cid] = channel_emv_rate(c)
        return ch_cache[cid]

    brands, matrix, weekly = {}, {}, {}
    prev_sec = {}
    last_seen = {}
    total_sec = total_emv = 0.0

    for v in videos:
        at = v.get("analyzed_at") or ""
        exp = v.get("brand_exposure") or {}
        if not exp:
            # Süre modeli öncesi kaydedilmiş video → SADECE brand_exposure
            # kolonunu doldur. Diğer agregalara (ad_frame_count, brand_counts)
            # dokunulmaz; kullanıcı "geçmiş veri değişmesin" dedi. Bu kolon
            # yeni olduğu için doldurmak mevcut hiçbir sayıyı değiştirmez.
            if backfill_budget <= 0:
                continue
            backfill_budget -= 1
            exp = _backfill_exposure(v["id"])
            if not exp:
                continue
        cid = v["channel_id"]
        cname = (_ch(cid).get("name") or cid)
        rate = ch_rate.get(cid, 0)
        in_period = (not since) or at >= since
        in_prev = bool(prev_since) and prev_since <= at < (since or "")

        for marka, e in exp.items():
            sec = float(e.get("sec") or 0)
            if sec <= 0:
                continue
            if at > last_seen.get(marka, ""):
                last_seen[marka] = at
            if in_prev:
                prev_sec[marka] = prev_sec.get(marka, 0.0) + sec
            if not in_period:
                continue
            emv = emv_of(sec, e.get("prom", 0.5), rate)
            b = brands.setdefault(marka, {
                "marka": marka, "seconds": 0.0, "appearances": 0, "emv": 0.0,
                "kind": e.get("kind", "spot"), "channels": set(), "videos": 0,
            })
            b["seconds"] += sec
            b["appearances"] += int(e.get("app") or 0)
            b["emv"] += emv
            b["channels"].add(cname)
            b["videos"] += 1
            if e.get("kind") == "sponsorluk":
                b["kind"] = "sponsorluk"
            matrix.setdefault(marka, {})
            matrix[marka][cname] = round(matrix[marka].get(cname, 0.0) + sec)
            total_sec += sec
            total_emv += emv
            # Haftalık trend (ISO hafta başlangıcı)
            try:
                dt = datetime.fromisoformat(at[:19])
                wk = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
                weekly.setdefault(marka, {})
                weekly[marka][wk] = round(weekly[marka].get(wk, 0.0) + sec)
            except Exception:
                pass

    out = []
    for b in brands.values():
        out.append({**b,
                    "channels": sorted(b["channels"]),
                    "channel_count": len(b["channels"]),
                    "seconds": round(b["seconds"]),
                    "emv": round(b["emv"]),
                    "sov_pct": round(100 * b["seconds"] / total_sec, 1) if total_sec else 0.0})
    out.sort(key=lambda x: -x["seconds"])

    # ── Uyarılar: yeni marka, kaybolan sponsor, ani değişim ──
    alerts = []
    if days:
        cutoff = (now - timedelta(days=days * 3)).isoformat()
        for b in out[:25]:
            m, cur = b["marka"], b["seconds"]
            old = prev_sec.get(m, 0.0)
            if old == 0 and cur > 0:
                alerts.append({"type": "new", "marka": m,
                               "text": f"{m} ilk kez göründü · {_dur(cur)}"})
            elif old > 0:
                chg = (cur - old) / old * 100
                if chg >= 100:
                    alerts.append({"type": "up", "marka": m,
                                   "text": f"{m} görünürlüğü %{round(chg)} arttı"})
                elif chg <= -60:
                    alerts.append({"type": "down", "marka": m,
                                   "text": f"{m} görünürlüğü %{abs(round(chg))} azaldı"})
        for m, seen_at in last_seen.items():
            if seen_at < cutoff and m not in {b["marka"] for b in out}:
                alerts.append({"type": "gone", "marka": m,
                               "text": f"{m} bu dönemde hiç görünmedi"})
    alerts = alerts[:6]

    return {
        "brands": out[:30],
        "matrix": [{"marka": m, "channels": c}
                   for m, c in sorted(matrix.items(),
                                      key=lambda x: -sum(x[1].values()))][:15],
        "trend": {m: weekly[m] for m in [b["marka"] for b in out[:6]] if m in weekly},
        "alerts": alerts,
        "totals": {"exposure_seconds": round(total_sec),
                   "exposure_label": _dur(total_sec),
                   "emv": round(total_emv),
                   "brand_count": len(out)},
    }


def _dur(sec):
    sec = int(round(sec or 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}s {m}dk" if h else (f"{m}dk {s}sn" if m else f"{s}sn")


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


def _brand_summary(videos):
    """Verilen videolardan top marka + sayaçlar (kanal logosu/sponsor zaten agregada)."""
    brands = {}
    ad_frames = 0
    channels = set()
    for v in videos:
        ad_frames += v.get("ad_frame_count", 0)
        channels.add(v["channel_id"])
        for m, c in (v.get("brand_counts") or {}).items():
            brands[m] = brands.get(m, 0) + c
    top = sorted(({"marka": m, "count": c} for m, c in brands.items()),
                 key=lambda x: -x["count"])
    return {
        "video_count": len(videos),
        "ad_frames": ad_frames,
        "channel_count": len(channels),
        "top_brands": top,
    }


_TR_UTC_OFFSET = 3  # Türkiye sabit UTC+3 (2016'dan beri DST yok)


def _tr_date(iso):
    """Naive-UTC ISO string → İstanbul tarihi 'YYYY-MM-DD' (gün kovalama için)."""
    try:
        return (datetime.fromisoformat(iso)
                + __import__("datetime").timedelta(hours=_TR_UTC_OFFSET)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def get_daily_report(day=None):
    """Ana sayfa günlük raporu: bugünün ve son 7 günün öne çıkan markaları + aktivite.
    'Bugün' İstanbul gününe göredir — UTC kullanmak 00:00-03:00 TR arasında yanlış gün verir."""
    now_tr = datetime.utcnow() + __import__("datetime").timedelta(hours=_TR_UTC_OFFSET)
    today = day or now_tr.strftime("%Y-%m-%d")
    week_cut = (now_tr - __import__("datetime").timedelta(days=7)).strftime("%Y-%m-%d")
    videos = get_all_videos(completed_only=True)
    today_v = [v for v in videos if _tr_date(v.get("analyzed_at")) == today]
    week_v = [v for v in videos if _tr_date(v.get("analyzed_at")) >= week_cut]
    last_day = max((_tr_date(v.get("analyzed_at")) for v in videos), default="")
    return {
        "date": today,
        "today": _brand_summary(today_v),
        "week": _brand_summary(week_v),
        "last_active_day": last_day,
        "total_channels": len({v["channel_id"] for v in videos}),
        "total_videos": len(videos),
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
        day = _tr_date(v.get("analyzed_at"))
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
        "auto_main_sponsors": json.loads(_col(d, "auto_main_sponsors", None) or "[]"),
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
        "brand_exposure": json.loads(_col(row, "brand_exposure") or "{}"),
        "completed": bool(row["completed"]),
        "is_partial": bool(_col(row, "is_partial", 0)),
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
        "verify_status": _col(row, "verify_status", "") or "",
        "verify_notes": _col(row, "verify_notes", "") or "",
    }
