import io
import csv
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, Response

from config import load_config, save_config
from services.youtube import fetch_channel_videos, channel_id_from_url, has_cookies
from services.job_manager import JOB_MANAGER
from models.database import (
    get_channel, get_channel_videos, get_video, get_detections, get_recent_videos,
    update_detection, recompute_video_aggregates, set_channel_brand_flag,
    edit_brand_global, get_dashboard_data, get_brand_appearances, get_all_videos,
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
        save_config(cfg)
        return jsonify({"ok": True})
    cfg = load_config()
    key = cfg.get("gemini_api_key", "")
    return jsonify({
        "has_key": bool(key),
        "key_preview": (key[:8] + "..." + key[-4:]) if len(key) > 12 else "",
        "channels": cfg.get("channels", []),
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


@api_bp.route("/api/maintenance/auto-sponsors", methods=["POST"])
def maintenance_auto_sponsors():
    """Mevcut tüm videolarda eşik üstü markaları geriye dönük ana sponsor yapar
    (şişik geçmiş veriyi düzeltir). Bir kez çalıştırmak yeterli."""
    from config import AUTO_SPONSOR_THRESHOLD
    videos = get_all_videos(completed_only=True)
    flagged = {}
    for v in videos:
        ch = get_channel(v["channel_id"]) or {}
        sk = {s.casefold() for s in ch.get("main_sponsors", [])}
        for m, c in (v.get("brand_counts") or {}).items():
            if c >= AUTO_SPONSOR_THRESHOLD and m.casefold() not in sk:
                set_channel_brand_flag(v["channel_id"], m, "main_sponsor", True)
                set_channel_brand_flag(v["channel_id"], m, "active_only", True)
                sk.add(m.casefold())
                flagged.setdefault(v["channel_id"], []).append(m)
    for v in videos:
        recompute_video_aggregates(v["id"])
    return jsonify({"ok": True, "flagged": flagged,
                    "threshold": AUTO_SPONSOR_THRESHOLD})


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
                             ch.get("main_sponsors", []), ch.get("sponsor_active_only", []))
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
    # brand_report + güncel bayraklar → okurken hesaplanır (canlı, kayıttan bağımsız)
    agg = compute_aggregates(detections, ch.get("channel_logos", []),
                             ch.get("main_sponsors", []),
                             ch.get("sponsor_active_only", []))
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
    if not get_video(video_id):
        return jsonify({"error": "Video bulunamadı"}), 404
    try:
        edit_brand_global(video_id, action, data.get("marka"), data.get("new_marka"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    agg = recompute_video_aggregates(video_id)
    return jsonify({"ok": True, **(agg or {})})


# ── Tarama / Analiz ───────────────────────────────────────────────────────────

@api_bp.route("/api/scan/channel", methods=["POST"])
def scan_channel():
    data = request.get_json()
    url = data.get("url", "").strip()
    hours = int(data.get("hours", 24))
    if not url:
        return jsonify({"error": "URL gerekli"}), 400
    job_id = JOB_MANAGER.add_channel_scan(url, last_hours=hours)
    return jsonify({"ok": True, "job_id": job_id})


@api_bp.route("/api/scan/all", methods=["POST"])
def scan_all():
    data = request.get_json() or {}
    hours = int(data.get("hours", 24))
    cfg = load_config()
    channels = cfg.get("channels", [])
    if not channels:
        return jsonify({"error": "Kanal listesi boş"}), 400
    job_ids = [JOB_MANAGER.add_channel_scan(url, last_hours=hours) for url in channels]
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
