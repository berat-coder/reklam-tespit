import os
import io
import csv
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, Response

from config import load_config, save_config
from services.youtube import fetch_channel_videos, channel_id_from_url, has_cookies
from services.job_manager import JOB_MANAGER
from models.database import (
    get_channel, get_channel_videos, get_video, get_detections, get_recent_videos,
    delete_video, list_recent_auto_scan_video_ids,
    update_detection, recompute_video_aggregates, set_channel_brand_flag,
    edit_brand_global, get_dashboard_data, get_brand_appearances, get_all_videos,
    get_daily_report,
    create_user, list_users, delete_user,
    add_brand_alias, bump_ignore_and_maybe_suggest, approve_suggestion,
    reject_suggestion, remove_rule, get_channel_rules,
    migrate_sqlite_to_pg,
)
from services.aggregates import compute_aggregates


def _csv_response(filename, header, rows):
    """UTF-8 BOM'lu CSV indirme yanıtı (Excel Türkçe karakter için)."""
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

api_bp = Blueprint("api", __name__)


# ── Yapılandırma ──────────────────────────────────────────────────────────────

@api_bp.route("/api/config", methods=["GET", "POST"])
def config_endpoint():
    if request.method == "POST":
        data = request.get_json()
        cfg = load_config()
        if "gemini_api_key" in data:
            cfg["gemini_api_key"] = data["gemini_api_key"]
        if "channels" in data:
            cfg["channels"] = data["channels"]
        if "global_ignored_brands" in data and isinstance(data["global_ignored_brands"], list):
            cfg["global_ignored_brands"] = [str(x).strip() for x in data["global_ignored_brands"]
                                            if str(x).strip()]
        if "excluded_placements" in data and isinstance(data["excluded_placements"], list):
            cfg["excluded_placements"] = [str(x).strip() for x in data["excluded_placements"]
                                          if str(x).strip()]
        if isinstance(data.get("frame_retention"), dict):
            fr = dict(cfg.get("frame_retention") or {})
            if "enabled" in data["frame_retention"]:
                fr["enabled"] = bool(data["frame_retention"]["enabled"])
            if "days" in data["frame_retention"]:
                try:
                    fr["days"] = max(0, int(data["frame_retention"]["days"]))
                except (TypeError, ValueError):
                    pass
            cfg["frame_retention"] = fr
        save_config(cfg)
        return jsonify({"ok": True})
    cfg = load_config()
    key = cfg.get("gemini_api_key", "")
    return jsonify({
        "has_key": bool(key),
        "key_preview": (key[:8] + "..." + key[-4:]) if len(key) > 12 else "",
        "channels": cfg.get("channels", []),
        "global_ignored_brands": cfg.get("global_ignored_brands", []),
        "excluded_placements": cfg.get("excluded_placements", []),
        "frame_retention": cfg.get("frame_retention", {"enabled": True, "days": 2}),
    })


# ── Kanallar ──────────────────────────────────────────────────────────────────

@api_bp.route("/api/channels")
def list_channels():
    cfg = load_config()
    out = []
    for url in cfg.get("channels", []):
        ch_id = channel_id_from_url(url)
        ch = get_channel(ch_id) or {}
        videos = get_channel_videos(ch_id, completed_only=True)
        total_ads = sum(v.get("ad_frame_count", 0) for v in videos)

        brand_totals: dict = {}
        for v in videos:
            for marka, count in (v.get("brand_counts") or {}).items():
                brand_totals[marka] = brand_totals.get(marka, 0) + count
        top_brands = sorted(brand_totals.items(), key=lambda x: -x[1])[:5]

        out.append({
            "id": ch_id,
            "url": url,
            "name": ch.get("name", ch_id),
            "video_count": len(videos),
            "total_ads": total_ads,
            "channel_logos": ch.get("channel_logos", []),
            "avatar_url": ch.get("avatar_url", ""),
            "top_brands": [{"name": b, "count": c} for b, c in top_brands],
            "last_scanned": ch.get("last_scanned"),
        })
    return jsonify({"channels": out})


@api_bp.route("/api/channel/<path:ch_id>")
@api_bp.route("/api/channel-info")
def channel_detail(ch_id=None):
    if ch_id is None:
        ch_id = request.args.get("id", "")
    if not ch_id:
        return jsonify({"error": "ch_id gerekli"}), 400

    ch = get_channel(ch_id)
    if not ch:
        cfg = load_config()
        for url in cfg.get("channels", []):
            if channel_id_from_url(url) == ch_id:
                return jsonify({
                    "channel": {"id": ch_id, "name": ch_id, "url": url,
                                "channel_logos": [], "last_scanned": None},
                    "videos": [], "brand_totals": [], "type_totals": [],
                })
        return jsonify({"error": "Kanal bulunamadı"}), 404

    videos = get_channel_videos(ch_id)
    brand_totals: dict = {}
    type_totals: dict = {}
    for v in videos:
        for marka, count in (v.get("brand_counts") or {}).items():
            brand_totals[marka] = brand_totals.get(marka, 0) + count
        for tur, count in (v.get("type_counts") or {}).items():
            type_totals[tur] = type_totals.get(tur, 0) + count

    return jsonify({
        "channel": {
            "id": ch_id,
            "name": ch.get("name", ""),
            "url": ch.get("url", ""),
            "channel_logos": ch.get("channel_logos", []),
            "last_scanned": ch.get("last_scanned"),
        },
        "videos": [{
            "id": v["id"],
            "title": v["title"],
            "thumbnail": v["thumbnail"],
            "duration": v["duration"],
            "analyzed_at": v["analyzed_at"],
            "ad_frame_count": v["ad_frame_count"],
            "total_frames": v["total_frames"],
            "type_counts": v["type_counts"],
            "top_brands": sorted((v.get("brand_counts") or {}).items(),
                                  key=lambda x: -x[1])[:3],
        } for v in videos],
        "brand_totals": [
            {"name": b, "count": c}
            for b, c in sorted(brand_totals.items(), key=lambda x: -x[1])
        ],
        "type_totals": [
            {"name": t, "count": c}
            for t, c in sorted(type_totals.items(), key=lambda x: -x[1])
        ],
    })


# ── Kullanıcı yönetimi ─────────────────────────────────────────────────────────

@api_bp.route("/api/users", methods=["GET", "POST"])
def users_endpoint():
    admin = os.environ.get("APP_USERNAME", "admin")
    if request.method == "POST":
        data = request.get_json() or {}
        try:
            create_user(data.get("username"), data.get("password"),
                        role=data.get("role", "user"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True})
    return jsonify({"users": list_users(), "admin": admin})


@api_bp.route("/api/users/<path:username>", methods=["DELETE"])
def delete_user_endpoint(username):
    admin = os.environ.get("APP_USERNAME", "admin")
    if username == admin:
        return jsonify({"error": "Ana hesap silinemez"}), 400
    delete_user(username)
    return jsonify({"ok": True})


# ── Sponsorluk İstihbarat Paneli ───────────────────────────────────────────────

def _since_from_days():
    """?days=N → o kadar gün öncesinin ISO tarihi; 0/yok → None (tümü)."""
    try:
        days = int(request.args.get("days", 0))
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return None
    return (datetime.utcnow() - timedelta(days=days)).isoformat()


@api_bp.route("/api/maintenance/migrate-db", methods=["POST"])
def maintenance_migrate_db():
    """SQLite → PostgreSQL tek seferlik veri taşıma."""
    return jsonify(migrate_sqlite_to_pg())


@api_bp.route("/api/maintenance/disk")
def maintenance_disk():
    """Disk/volume durumu teşhisi."""
    import shutil as _sh
    from config import DATA_DIR, FRAMES_DIR
    def _dirsize(p):
        t = 0
        try:
            for root, _, files in os.walk(p):
                for f in files:
                    try: t += os.path.getsize(os.path.join(root, f))
                    except OSError: pass
        except Exception: pass
        return t
    du = _sh.disk_usage(str(DATA_DIR))
    old_frames = DATA_DIR / "frames"
    return jsonify({
        "data_dir": str(DATA_DIR), "frames_dir": str(FRAMES_DIR),
        "disk_total_mb": round(du.total / 1e6, 1),
        "disk_used_mb": round(du.used / 1e6, 1),
        "disk_free_mb": round(du.free / 1e6, 1),
        "old_volume_frames_exists": old_frames.exists(),
        "old_volume_frames_mb": round(_dirsize(old_frames) / 1e6, 1),
        "ephemeral_frames_mb": round(_dirsize(FRAMES_DIR) / 1e6, 1),
    })


@api_bp.route("/api/maintenance/clean-frames", methods=["POST"])
def maintenance_clean_frames():
    """Kare bakımını ŞİMDİ çalıştır: retention (N günden eski kareler) + boyut
    cap'i uygula, diski aç. Rapor/veri silinmez. Önce/sonra disk döner."""
    import shutil as _sh
    from config import DATA_DIR, FRAMES_DIR, FRAME_STORAGE_CAP_MB
    from services.storage import prune_frames_by_age, prune_frames

    def _dirsize(p):
        t = 0
        try:
            for root, _, files in os.walk(p):
                for f in files:
                    try: t += os.path.getsize(os.path.join(root, f))
                    except OSError: pass
        except Exception: pass
        return t

    def _nvids():
        try: return sum(1 for x in FRAMES_DIR.iterdir() if x.is_dir())
        except Exception: return 0

    du0 = _sh.disk_usage(str(DATA_DIR))
    frames_before = _dirsize(FRAMES_DIR)
    vids_before = _nvids()

    cfg = load_config()
    fr = cfg.get("frame_retention") or {}
    aged = prune_frames_by_age(int(fr.get("days", 2))) if fr.get("enabled", True) else 0
    capped = prune_frames(FRAME_STORAGE_CAP_MB)

    du1 = _sh.disk_usage(str(DATA_DIR))
    frames_after = _dirsize(FRAMES_DIR)
    return jsonify({
        "ok": True,
        "deleted_video_dirs": aged + capped,
        "freed_mb": round((frames_before - frames_after) / 1e6, 1),
        "video_dirs_before": vids_before, "video_dirs_after": _nvids(),
        "frames_mb_before": round(frames_before / 1e6, 1),
        "frames_mb_after": round(frames_after / 1e6, 1),
        "disk_used_mb": round(du1.used / 1e6, 1),
        "disk_total_mb": round(du1.total / 1e6, 1),
        "disk_free_mb": round(du1.free / 1e6, 1),
        "retention_days": int(fr.get("days", 2)),
        "retention_enabled": bool(fr.get("enabled", True)),
        "cap_mb": FRAME_STORAGE_CAP_MB,
    })


@api_bp.route("/api/maintenance/auto-sponsors", methods=["POST"])
def maintenance_auto_sponsors():
    """Mevcut tüm videolarda eşik üstü VEYA sürekli ekranda olan (köşe logosu)
    markaları geriye dönük ana sponsor + 'köşe logosunu sayma' yapar → şişik
    geçmiş veriyi (ör. 700 A101) düzeltir. Bir kez çalıştırmak yeterli."""
    from config import AUTO_SPONSOR_THRESHOLD
    from services.aggregates import auto_sponsor_candidates
    from models.database import get_detections
    videos = get_all_videos(completed_only=True)
    flagged = {}
    for v in videos:
        ch = get_channel(v["channel_id"]) or {}
        agg = compute_aggregates(
            get_detections(v["id"]), ch.get("channel_logos", []),
            ch.get("main_sponsors", []), ch.get("sponsor_active_only", []),
            brand_aliases=ch.get("brand_aliases", {}),
            ignored_brands=ch.get("ignored_brands", []))
        cands = auto_sponsor_candidates(agg, AUTO_SPONSOR_THRESHOLD,
                                        ch.get("main_sponsors", []))
        for m in cands:
            set_channel_brand_flag(v["channel_id"], m, "main_sponsor", True)
            set_channel_brand_flag(v["channel_id"], m, "active_only", True)
            flagged.setdefault(v["channel_id"], [])
            if m not in flagged[v["channel_id"]]:
                flagged[v["channel_id"]].append(m)
    for v in videos:
        recompute_video_aggregates(v["id"])
    return jsonify({"ok": True, "flagged": flagged,
                    "threshold": AUTO_SPONSOR_THRESHOLD})


# ── Otomatik gece taraması ──────────────────────────────────────────────────

@api_bp.route("/api/auto-scan/settings", methods=["GET", "POST"])
def auto_scan_settings():
    from config import DEFAULT_AUTO_SCAN
    cfg = load_config()
    if request.method == "POST":
        data = request.get_json() or {}
        cur = dict(cfg.get("auto_scan") or DEFAULT_AUTO_SCAN)
        # Tip-güvenli alan alımı
        if "enabled" in data:
            cur["enabled"] = bool(data["enabled"])
        for key in ("start", "end"):
            if key in data and isinstance(data[key], str) and ":" in data[key]:
                cur[key] = data[key].strip()
        for key in ("interval_min", "lookback_hours", "nightly_cap", "tz_offset"):
            if key in data:
                try:
                    cur[key] = int(data[key])
                except (TypeError, ValueError):
                    pass
        if data.get("content_type") in ("all", "live", "video"):
            cur["content_type"] = data["content_type"]
        cfg["auto_scan"] = cur
        save_config(cfg)
        return jsonify({"ok": True, "auto_scan": cur})
    return jsonify({"auto_scan": cfg.get("auto_scan") or DEFAULT_AUTO_SCAN})


@api_bp.route("/api/auto-scan/status")
def auto_scan_status():
    from services.scheduler import get_status
    from models.database import list_live_seen
    st = get_status()
    # Bu gece yakalanan canlı yayınlar (panel listesi)
    from datetime import datetime as _dt, timedelta as _td
    since = (_dt.utcnow() - _td(hours=30)).isoformat()
    st["recent_lives"] = list_live_seen(since=since, limit=30)
    return jsonify(st)


@api_bp.route("/api/health")
def health_endpoint():
    """Sistem sağlığı: cookie durumu, son başarılı tarama, son 24s hata."""
    from models.database import get_health
    return jsonify(get_health())


@api_bp.route("/api/scan-log")
def scan_log_endpoint():
    """Tarama/olay geçmişi + başarısız canlı yayınlar (Durum paneli)."""
    from models.database import get_scan_log, list_failed_live, get_health
    try:
        limit = min(300, max(1, int(request.args.get("limit", 100))))
    except (TypeError, ValueError):
        limit = 100
    return jsonify({
        "health": get_health(),
        "log": get_scan_log(limit),
        "failed_lives": list_failed_live(30),
    })


@api_bp.route("/api/maintenance/clear-auto-scans", methods=["POST"])
def clear_auto_scans():
    """Son N saatte otomatik taramayla analiz edilmiş videoları TAMAMEN sil
    (yanlış/şişik gece taramasını temizle). Yönetici (before_request zorlar)."""
    data = request.get_json() or {}
    try:
        hours = int(data.get("hours", 30))
    except (TypeError, ValueError):
        hours = 30
    ids = list_recent_auto_scan_video_ids(hours)
    import shutil as _sh
    from config import FRAMES_DIR
    for vid in ids:
        delete_video(vid)   # tespit + video + live_seen kaydını siler
        try:
            d = FRAMES_DIR / vid
            if d.exists():
                _sh.rmtree(d, ignore_errors=True)
        except Exception:
            pass
    return jsonify({"ok": True, "deleted": len(ids), "hours": hours})


@api_bp.route("/api/live-archive")
def live_archive():
    """Gece/canlı taramayla analiz edilmiş yayınlar (güne göre gruplamak için)."""
    from models.database import get_live_streams_archive
    return jsonify({"videos": get_live_streams_archive(200)})


@api_bp.route("/api/auto-scan/run-now", methods=["POST"])
def auto_scan_run_now():
    from services.scheduler import run_tick_now, get_status
    try:
        run_tick_now()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, **get_status()})


@api_bp.route("/api/daily-report")
def daily_report():
    return jsonify(get_daily_report())


@api_bp.route("/api/dashboard")
def dashboard():
    return jsonify(get_dashboard_data(since=_since_from_days()))


@api_bp.route("/api/brand/<path:name>")
def brand_detail(name):
    return jsonify(get_brand_appearances(name))


@api_bp.route("/api/analyses")
def analyses():
    """Tüm tamamlanmış analizler (tarih filtreli) — 'Son Analizler' tam sayfası."""
    since = _since_from_days()
    videos = get_all_videos(completed_only=True)
    out = []
    for v in videos:
        if since and (v.get("analyzed_at") or "") < since:
            continue
        out.append({
            "id": v["id"], "title": v["title"], "thumbnail": v["thumbnail"],
            "channel_id": v["channel_id"], "channel_name": v.get("channel_name", ""),
            "channel_avatar": v.get("channel_avatar", ""),
            "analyzed_at": v["analyzed_at"], "ad_frame_count": v["ad_frame_count"],
            "total_frames": v["total_frames"], "duration": v["duration"],
            "top_brands": sorted((v.get("brand_counts") or {}).items(),
                                 key=lambda x: -x[1])[:3],
        })
    return jsonify({"videos": out})


# ── Dışa aktarma (CSV) ─────────────────────────────────────────────────────────

@api_bp.route("/api/video/<video_id>/export.csv")
def export_video_csv(video_id):
    v = get_video(video_id)
    if not v:
        return jsonify({"error": "Video bulunamadı"}), 404
    ch = get_channel(v["channel_id"]) or {}
    agg = compute_aggregates(get_detections(video_id), ch.get("channel_logos", []),
                             ch.get("main_sponsors", []), ch.get("sponsor_active_only", []),
                             brand_aliases=ch.get("brand_aliases", {}),
                             ignored_brands=ch.get("ignored_brands", []))
    rows = [[
        b["marka"], b["appearances"], b["frame_count"],
        " / ".join(f"{t}:{n}" for t, n in (b.get("tur_counts") or {}).items()),
        b.get("first_ts", ""), b.get("last_ts", ""), b.get("max_guven", ""),
    ] for b in agg["brand_report"]]
    return _csv_response(
        f"rapor_{video_id}.csv",
        ["Marka", "Görünüm", "Kare", "Türler", "İlk", "Son", "Güven"], rows)


@api_bp.route("/api/channel/<path:ch_id>/export.csv")
def export_channel_csv(ch_id):
    videos = get_channel_videos(ch_id)
    brand_totals = {}
    for v in videos:
        for marka, count in (v.get("brand_counts") or {}).items():
            brand_totals[marka] = brand_totals.get(marka, 0) + count
    rows = [[m, c] for m, c in sorted(brand_totals.items(), key=lambda x: -x[1])]
    ch = get_channel(ch_id) or {}
    safe = (ch.get("name") or ch_id).replace(" ", "_")
    return _csv_response(f"kanal_{safe}.csv", ["Marka", "Toplam Reklam"], rows)


@api_bp.route("/api/brand/<path:name>/export.csv")
def export_brand_csv(name):
    data = get_brand_appearances(name)
    rows = [[v["channel_name"], v["title"], v["count"], (v.get("analyzed_at") or "")[:10]]
            for v in data["videos"]]
    safe = (data["marka"] or name).replace(" ", "_").replace("/", "-")
    return _csv_response(f"marka_{safe}.csv",
                         ["Kanal", "Video", "Reklam Sayısı", "Tarih"], rows)


@api_bp.route("/api/dashboard/export.csv")
def export_dashboard_csv():
    data = get_dashboard_data(since=_since_from_days())
    rows = [[b["marka"], b["count"], b["channel_count"], b["video_count"]]
            for b in sorted(data["top_brands"],
                            key=lambda x: (-x["channel_count"], -x["count"]))]
    return _csv_response("panel_markalar.csv",
                         ["Marka", "Toplam Reklam", "Kanal Sayısı", "Video Sayısı"], rows)


@api_bp.route("/api/channel/<path:ch_id>/browse")
@api_bp.route("/api/channel-browse")
def channel_browse(ch_id=None):
    if ch_id is None:
        ch_id = request.args.get("id", "")
    if not ch_id:
        return jsonify({"error": "ch_id gerekli"}), 400

    cfg = load_config()
    channel_url = next(
        (u for u in cfg.get("channels", []) if channel_id_from_url(u) == ch_id),
        None,
    )
    if not channel_url:
        ch = get_channel(ch_id)
        channel_url = ch.get("url") if ch else None
    if not channel_url:
        return jsonify({"error": "Kanal bulunamadı"}), 404

    # fetch_channel_videos bloklar — thread'de çalıştır, 90s timeout
    import threading
    result_box = [None]
    error_box = [None]

    def _fetch():
        try:
            result_box[0] = fetch_channel_videos(channel_url)
        except Exception as e:
            error_box[0] = str(e)

    t = threading.Thread(target=_fetch, daemon=True)
    t.start()
    t.join(timeout=90)

    if t.is_alive():
        return jsonify({"error": "YouTube zaman aşımı — birazdan tekrar dene"}), 504
    if error_box[0]:
        return jsonify({"error": f"Kanal taranamadı: {error_box[0]}"}), 500

    res = result_box[0]

    analyzed_ids = {v["id"] for v in get_channel_videos(ch_id)}
    qs = JOB_MANAGER.queue_status()
    current_url = None
    queued_urls: set = set()

    if not JOB_MANAGER.__class__.__dict__.get("USE_REDIS", False):
        # thread mode — inspect internal queue
        from services.job_manager import USE_REDIS
        if not USE_REDIS:
            with JOB_MANAGER._lock:
                queued_urls = {j.get("url", "") for j in JOB_MANAGER._queue
                               if j.get("type") == "video"}
                if JOB_MANAGER._current and JOB_MANAGER._current.get("type") == "video":
                    current_url = JOB_MANAGER._current.get("url", "")

    videos = []
    for v in res["videos"]:
        if v["id"] in analyzed_ids:
            status = "analyzed"
        elif v["url"] == current_url:
            status = "processing"
        elif v["url"] in queued_urls:
            status = "queued"
        else:
            status = "not_analyzed"
        videos.append({**v, "status": status})

    return jsonify({
        "channel_name": res["channel_name"],
        "channel_id": ch_id,
        "channel_url": channel_url,
        "videos": videos,
    })


# ── Video ─────────────────────────────────────────────────────────────────────

@api_bp.route("/api/video/<video_id>")
def video_detail(video_id):
    v = get_video(video_id)
    if not v:
        return jsonify({"error": "Video bulunamadı"}), 404
    ch = get_channel(v["channel_id"]) or {}
    detections = get_detections(video_id)
    # brand_report + güncel bayraklar + öğrenilen kurallar → okurken hesaplanır
    agg = compute_aggregates(detections, ch.get("channel_logos", []),
                             ch.get("main_sponsors", []),
                             ch.get("sponsor_active_only", []),
                             brand_aliases=ch.get("brand_aliases", {}),
                             ignored_brands=ch.get("ignored_brands", []))
    return jsonify({
        "video": {
            **v,
            "detections": detections,
            "brand_report": agg["brand_report"],
            "persistent_overlays": agg["persistent_overlays"],
        },
        "channel": {
            "id": v["channel_id"],
            "name": ch.get("name", ""),
            "channel_logos": ch.get("channel_logos", []),
            "main_sponsors": ch.get("main_sponsors", []),
            "sponsor_active_only": ch.get("sponsor_active_only", []),
        },
    })


@api_bp.route("/api/video/<video_id>", methods=["DELETE"])
def delete_video_endpoint(video_id):
    """Bir analizi/videoyu tamamen sil (yönetici). before_request DELETE'i
    kullanıcılara zaten kapatır; burada ek bir güvenlik gerekmiyor."""
    v = get_video(video_id)
    if not v:
        return jsonify({"error": "Video bulunamadı"}), 404
    delete_video(video_id)
    # Efemeral frame'leri de temizle (varsa)
    try:
        import shutil
        from config import FRAMES_DIR
        d = FRAMES_DIR / video_id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass
    return jsonify({"ok": True, "deleted": video_id})


@api_bp.route("/api/video/<video_id>/detection/<int:index>", methods=["POST"])
def correct_detection(video_id, index):
    """Manuel düzeltme: mark_clean | remove_tespit | remove_brand."""
    data = request.get_json() or {}
    action = data.get("action", "")
    try:
        update_detection(
            video_id, index, action,
            tespit_index=data.get("tespit_index"),
            marka=data.get("marka"),
            tur=data.get("tur"),
            konum=data.get("konum"),
            detay=data.get("detay"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    agg = recompute_video_aggregates(video_id)
    return jsonify({"ok": True, **(agg or {})})


@api_bp.route("/api/video/<video_id>/brand-flag", methods=["POST"])
def brand_flag(video_id):
    """Markayı kanal logosu / ana sponsor olarak işaretler veya geri alır.
    body: {marka, flag: 'channel_logo'|'main_sponsor', value: true|false}"""
    data = request.get_json() or {}
    marka = (data.get("marka") or "").strip()
    flag = data.get("flag")
    value = bool(data.get("value", True))
    if flag not in ("channel_logo", "main_sponsor", "active_only"):
        return jsonify({"error": "Geçersiz flag"}), 400
    v = get_video(video_id)
    if not v:
        return jsonify({"error": "Video bulunamadı"}), 404
    try:
        set_channel_brand_flag(v["channel_id"], marka, flag, value)
        # Ana sponsor yapılınca "sadece gerçek reklamları say" (köşe logosunu
        # sayma / active_only) da otomatik işaretlensin; geri alınınca kalksın.
        if flag == "main_sponsor":
            set_channel_brand_flag(v["channel_id"], marka, "active_only", value)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    agg = recompute_video_aggregates(video_id)
    return jsonify({"ok": True, **(agg or {})})


@api_bp.route("/api/video/<video_id>/brand-edit", methods=["POST"])
def brand_edit(video_id):
    """Bir markayı TÜM karelerde yeniden adlandırır veya kaldırır (reklam değil).
    body: {action: 'rename'|'remove', marka, new_marka?}"""
    data = request.get_json() or {}
    action = data.get("action")
    if action not in ("rename", "remove"):
        return jsonify({"error": "Geçersiz action"}), 400
    v = get_video(video_id)
    if not v:
        return jsonify({"error": "Video bulunamadı"}), 404
    try:
        edit_brand_global(video_id, action, data.get("marka"), data.get("new_marka"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # ── Kanal bazlı öğrenme ──
    learned = None
    cid = v["channel_id"]
    if action == "rename":
        add_brand_alias(cid, data.get("marka"), data.get("new_marka"))
        learned = "alias"  # hemen aktif
    elif action == "remove":
        bump_ignore_and_maybe_suggest(cid, data.get("marka"))
        rules = get_channel_rules(cid)
        if any(s.get("marka", "").casefold() == (data.get("marka") or "").strip().casefold()
               for s in rules.get("suggestions", [])):
            learned = "suggestion"

    agg = recompute_video_aggregates(video_id)
    return jsonify({"ok": True, "learned": learned, **(agg or {})})


# ── Öğrenilen kurallar (kanal bazlı) ───────────────────────────────────────────

def _recompute_channel(ch_id):
    for v in get_channel_videos(ch_id):
        recompute_video_aggregates(v["id"])


@api_bp.route("/api/channel/<path:ch_id>/rules")
def channel_rules(ch_id):
    return jsonify(get_channel_rules(ch_id))


@api_bp.route("/api/channel/<path:ch_id>/rules/approve", methods=["POST"])
def channel_rule_approve(ch_id):
    marka = (request.get_json() or {}).get("marka", "")
    approve_suggestion(ch_id, marka)
    _recompute_channel(ch_id)
    return jsonify({"ok": True})


@api_bp.route("/api/channel/<path:ch_id>/rules/reject", methods=["POST"])
def channel_rule_reject(ch_id):
    marka = (request.get_json() or {}).get("marka", "")
    reject_suggestion(ch_id, marka)
    return jsonify({"ok": True})


@api_bp.route("/api/channel/<path:ch_id>/rules/remove", methods=["POST"])
def channel_rule_remove(ch_id):
    data = request.get_json() or {}
    remove_rule(ch_id, data.get("kind"), data.get("key"))
    _recompute_channel(ch_id)
    return jsonify({"ok": True})


# ── Tarama / Analiz ───────────────────────────────────────────────────────────

def _content_type(data):
    ct = (data.get("content_type") or "all").lower()
    return ct if ct in ("all", "live", "video") else "all"


@api_bp.route("/api/scan/channel", methods=["POST"])
def scan_channel():
    data = request.get_json()
    url = data.get("url", "").strip()
    hours = int(data.get("hours", 24))
    if not url:
        return jsonify({"error": "URL gerekli"}), 400
    job_id = JOB_MANAGER.add_channel_scan(url, last_hours=hours,
                                          content_type=_content_type(data))
    return jsonify({"ok": True, "job_id": job_id})


@api_bp.route("/api/scan/all", methods=["POST"])
def scan_all():
    data = request.get_json() or {}
    hours = int(data.get("hours", 24))
    ct = _content_type(data)
    cfg = load_config()
    channels = cfg.get("channels", [])
    if not channels:
        return jsonify({"error": "Kanal listesi boş"}), 400
    job_ids = [JOB_MANAGER.add_channel_scan(url, last_hours=hours, content_type=ct)
               for url in channels]
    return jsonify({"ok": True, "job_ids": job_ids, "count": len(channels)})


@api_bp.route("/api/analyze/video", methods=["POST"])
def analyze_single_video():
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL gerekli"}), 400
    if "youtube.com" not in url and "youtu.be" not in url:
        return jsonify({"error": "Geçerli bir YouTube URL'si girin"}), 400
    if "/shorts/" in url:
        return jsonify({"error": "Shorts videoları analiz edilmiyor"}), 400
    if not has_cookies():
        return jsonify({
            "error": "YouTube cookie bulunamadı. Railway'de YOUTUBE_COOKIES "
                     "env var'ını ayarlaman gerekiyor.",
            "cookie_missing": True,
        }), 503
    job_id = JOB_MANAGER.add_video(url, priority=True)
    return jsonify({"ok": True, "job_id": job_id})


# ── Kuyruk & Canlı ────────────────────────────────────────────────────────────

@api_bp.route("/api/live-video")
def live_video():
    live = JOB_MANAGER.get_live_video()
    if live is None:
        return jsonify({"active": False})
    return jsonify({"active": True, **live})


@api_bp.route("/api/queue")
def queue_status():
    return jsonify(JOB_MANAGER.queue_status())


@api_bp.route("/api/cancel-queue", methods=["POST"])
def cancel_queue():
    JOB_MANAGER.cancel_all()
    return jsonify({"ok": True})


@api_bp.route("/api/recent-videos")
def recent_videos_endpoint():
    videos = get_recent_videos(10)
    return jsonify({"videos": videos})
