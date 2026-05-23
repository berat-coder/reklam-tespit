from flask import Blueprint, request, jsonify

from config import load_config, save_config
from services.youtube import fetch_channel_videos, channel_id_from_url
from services.job_manager import JOB_MANAGER
from models.database import (
    get_channel, get_channel_videos, get_video, get_detections,
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
    return jsonify({
        "video": {**v, "detections": detections},
        "channel": {"id": v["channel_id"], "name": ch.get("name", "")},
    })


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
