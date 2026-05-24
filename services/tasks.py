"""
Video analiz ve kanal tarama görevleri.
RQ worker'lardan (process_video_rq / process_channel_scan_rq)
ve thread worker'lardan (process_video_sync / process_channel_scan_sync) çağrılır.
"""

import re
import base64
import time
import uuid
from datetime import datetime

import cv2
import numpy as np

from config import load_config, FRAMES_DIR
from services.gemini import gemini_analyze_frame, gemini_extract_brands
from services.youtube import get_ydl_opts, fetch_channel_videos, channel_id_from_url
from models.database import (
    upsert_channel, upsert_video, save_detections,
    get_channel, is_video_completed, update_channel_logos,
)


# ── Yardımcı fonksiyonlar ─────────────────────────────────────────────────────

def _fmt_ts(s):
    t = int(s)
    h, r = divmod(t, 3600)
    m, ss = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{ss:02d}" if h else f"{m:02d}:{ss:02d}"


def _normalize_brand(b):
    if not b:
        return ""
    return re.sub(r"[^a-z0-9]", "", b.lower())


def _frame_diff(f1, f2):
    try:
        s1 = cv2.resize(cv2.cvtColor(f1, cv2.COLOR_BGR2GRAY), (64, 36))
        s2 = cv2.resize(cv2.cvtColor(f2, cv2.COLOR_BGR2GRAY), (64, 36))
        return float(np.mean(cv2.absdiff(s1, s2))) / 255.0
    except Exception:
        return 1.0


def _lower_diff(f1, f2):
    try:
        h = f1.shape[0]
        r1 = cv2.resize(cv2.cvtColor(f1[int(h * 0.78):, :], cv2.COLOR_BGR2GRAY), (128, 32))
        r2 = cv2.resize(cv2.cvtColor(f2[int(h * 0.78):, :], cv2.COLOR_BGR2GRAY), (128, 32))
        return float(np.mean(cv2.absdiff(r1, r2))) / 255.0
    except Exception:
        return 1.0


# ── Kanal tarama ──────────────────────────────────────────────────────────────

def process_channel_scan_rq(channel_url, last_hours=24):
    """RQ worker'dan çağrılır. Kanalı tarayıp videoları sıraya ekler."""
    from services.job_manager import JOB_MANAGER
    _do_channel_scan(channel_url, last_hours, JOB_MANAGER)


def process_channel_scan_sync(job, _api_key, job_manager):
    """Thread worker'dan çağrılır."""
    job_manager._status = "scanning_channel"
    job_manager._message = f"Kanal taranıyor: {job['url']}"
    _do_channel_scan(job["url"], job.get("last_hours", 24), job_manager)


def _do_channel_scan(channel_url, last_hours, job_manager):
    try:
        res = fetch_channel_videos(channel_url, last_hours=last_hours)
    except Exception as e:
        print(f"[KANAL-TARAMA] Hata: {e}")
        return

    channel_id = channel_id_from_url(channel_url)
    channel_name = res["channel_name"] or channel_id

    upsert_channel(
        channel_id, name=channel_name, url=channel_url,
        avatar_url=res.get("channel_avatar", ""),
        last_scanned=datetime.utcnow().isoformat()
    )

    added = 0
    for v in res["videos"]:
        if is_video_completed(v["id"]):
            continue
        job_manager.add_video(
            v["url"],
            channel_id=channel_id,
            channel_name=channel_name,
        )
        added += 1
    print(f"[KANAL-TARAMA] {channel_name}: {added} yeni video sıraya alındı")


# ── Video analizi ─────────────────────────────────────────────────────────────

def process_video_rq(url, channel_id=None, channel_name=None):
    """RQ worker'dan çağrılır."""
    cfg = load_config()
    api_key = cfg.get("gemini_api_key", "")
    if not api_key:
        print("[VIDEO] API key yok, atlanıyor")
        return

    from services.job_manager import _get_redis_live
    _analyze_video_core(
        url=url,
        channel_id=channel_id,
        channel_name=channel_name,
        api_key=api_key,
        on_set_live=_get_redis_live().set,
        on_add_detection=_get_redis_live().add_detection,
        on_clear_live=_get_redis_live().clear,
        cancel_check=lambda: False,
    )


def process_video_sync(job, api_key, job_manager):
    """Thread worker'dan çağrılır."""
    job_manager._status = "analyzing"
    job_manager._message = f"Video açılıyor: {job.get('video_title', '')[:40]}"

    def on_set_live(**kwargs):
        job_manager.set_live_video(**kwargs)

    def on_add_detection(d):
        job_manager.add_live_detection(d)

    def on_clear_live():
        job_manager.clear_live_video()

    def cancel_check():
        return job_manager._cancel_flag.is_set()

    def on_status(msg):
        job_manager._message = msg

    _analyze_video_core(
        url=job["url"],
        channel_id=job.get("channel_id"),
        channel_name=job.get("channel_name"),
        api_key=api_key,
        on_set_live=on_set_live,
        on_add_detection=on_add_detection,
        on_clear_live=on_clear_live,
        cancel_check=cancel_check,
        on_status=on_status,
    )


def _analyze_video_core(
    url, channel_id, channel_name, api_key,
    on_set_live, on_add_detection, on_clear_live,
    cancel_check, on_status=None,
):
    from yt_dlp import YoutubeDL

    def status(msg):
        print(f"[VIDEO] {msg}")
        if on_status:
            on_status(msg)

    # 1. Meta — format yoksa bile title/description alabilmek için ignore_no_formats_error
    try:
        with YoutubeDL(get_ydl_opts({
            "skip_download": True,
            "noplaylist": True,
            "ignore_no_formats_error": True,
        })) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        err = str(e)
        if "Please sign in" in err or "Sign in" in err:
            status("YouTube cookie gerekiyor — Railway'de YOUTUBE_COOKIES env var'ını ayarla")
        elif "not available" in err or "private" in err.lower() or "removed" in err.lower():
            status(f"Video erişilemiyor (özel/silinmiş/kısıtlı): {err}")
        else:
            status(f"Meta hatası: {err}")
        on_set_live(status="error", message=err, progress=0)
        return

    if not info:
        status("Video bilgisi alınamadı")
        on_set_live(status="error", message="Video bilgisi alınamadı", progress=0)
        return

    video_id = info.get("id")
    title = info.get("title", "")
    duration = info.get("duration", 0) or 0
    description = info.get("description", "") or ""
    thumb_url = info.get("thumbnail", "")

    if not channel_id:
        channel_id = channel_id_from_url(
            info.get("channel_url", "") or info.get("webpage_url", "")
        )
    if not channel_name:
        channel_name = info.get("channel", "")

    from services.youtube import _pick_channel_avatar
    avatar_url = (
        info.get("channel_thumbnail") or
        info.get("uploader_thumbnail") or
        _pick_channel_avatar(info.get("thumbnails") or []) or
        ""
    )

    upsert_channel(channel_id, name=channel_name, url=url,
                   avatar_url=avatar_url,
                   last_scanned=datetime.utcnow().isoformat())

    ch = get_channel(channel_id) or {}
    channel_logos = ch.get("channel_logos", [])

    # ── Canlı state başlat ──
    on_clear_live()
    on_set_live(
        video_id=video_id, title=title, url=url, duration=duration,
        thumbnail=thumb_url, channel_id=channel_id, channel_name=channel_name,
        status="preparing", progress=0, detections=[], api_calls=0,
        total_frames=0, current_frame=0, total_steps=0,
        message="Açıklama analiz ediliyor...",
    )

    # 2. Açıklamadan markalar
    status(f"Açıklama analiz: {title[:40]}")
    desc_brands = gemini_extract_brands(api_key, title, description)
    on_set_live(desc_brands=desc_brands, channel_logos=channel_logos,
                message="Stream URL alınıyor...")

    # 3. Stream URL
    status(f"Stream alınıyor: {title[:40]}")
    stream_url = None
    try:
        with YoutubeDL(get_ydl_opts({
            "skip_download": True,
            "noplaylist": True,
            "ignore_no_formats_error": True,
        })) as ydl:
            si = ydl.extract_info(url, download=False)

        # Tek URL'li format (mp4, HLS)
        stream_url = si.get("url") if si else None

        # Birleşik format (DASH: ayrı video+audio) → video parçasını al
        if not stream_url and si:
            for rf in (si.get("requested_formats") or []):
                if rf.get("vcodec") not in (None, "none"):
                    stream_url = rf["url"]
                    break

        # Hâlâ yoksa format listesinden en iyi video URL'ini seç
        if not stream_url and si:
            for f in reversed(si.get("formats") or []):
                if f.get("url") and f.get("vcodec") not in (None, "none"):
                    stream_url = f["url"]
                    break
    except Exception as e:
        status(f"Stream hatası: {e}")
        on_set_live(status="error", message=str(e), progress=0)
        return

    if not stream_url:
        status("Stream URL bulunamadı — video özel veya kısıtlı olabilir")
        on_set_live(status="error", message="Stream URL bulunamadı", progress=0)
        return

    # 4. Frame analizi
    cap = cv2.VideoCapture(stream_url)
    if not cap.isOpened():
        status("Video açılamadı")
        return

    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    total_f = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    vid_duration = total_f / fps if fps > 0 else duration or 60
    interval = 8
    frame_step = max(1, int(fps * interval))
    total_steps = max(1, int(vid_duration / interval))

    job_frames_dir = FRAMES_DIR / video_id
    job_frames_dir.mkdir(exist_ok=True)

    current_frame = 0
    analyzed = 0
    api_calls = 0
    detections = []
    ad_appearances = {}
    last_frame = None
    last_result = None

    while True:
        if cancel_check():
            cap.release()
            return

        cap.set(cv2.CAP_PROP_POS_FRAMES, current_frame)
        ret, frame = cap.read()
        if not ret:
            break

        ts = current_frame / fps if fps > 0 else analyzed * interval
        ts_str = _fmt_ts(ts)
        frame_filename = f"frame_{analyzed:04d}_{int(ts)}s.jpg"
        cv2.imwrite(str(job_frames_dir / frame_filename), frame,
                    [cv2.IMWRITE_JPEG_QUALITY, 78])

        # Skip API if frame is nearly identical AND last result was a clean API call
        last_reliable = last_result is not None and not last_result.get("_skipped")
        skip_api = (
            last_frame is not None
            and last_reliable
            and _frame_diff(last_frame, frame) < 0.03
            and _lower_diff(last_frame, frame) < 0.04
        )

        if skip_api:
            result = {
                "reklam_var": last_result.get("reklam_var", False),
                "guven": last_result.get("guven", "Orta"),
                "markalar": last_result.get("markalar", []),
                "tespitler": last_result.get("tespitler", []),
                "ozet": "[Önceki frame ile aynı]",
            }
        else:
            fh, fw = frame.shape[:2]
            fs = cv2.resize(frame, (720, int(fh * 720 / fw))) if fw > 720 else frame
            _, buf = cv2.imencode(".jpg", fs, [cv2.IMWRITE_JPEG_QUALITY, 88])
            b64 = base64.b64encode(buf.tobytes()).decode()
            result = gemini_analyze_frame(api_key, b64, channel_logos, desc_brands, ts_str)
            api_calls += 1
            # Only update last_result if the call succeeded (not rate-limited)
            if not result.get("_skipped"):
                last_result = result
            time.sleep(8)   # gemini-2.5-flash free tier: 10 RPM → ≥6s between calls

        last_frame = frame

        # Duplicate suppression — same brand+type capped at 3 appearances
        filtered = []
        for t in result.get("tespitler", []):
            norm = _normalize_brand(t.get("marka", "")) + "|" + (t.get("tur", "") or "")
            appearances = ad_appearances.get(norm, [])
            if len(appearances) < 3:
                filtered.append(t)
                ad_appearances.setdefault(norm, []).append(analyzed)

        # reklam_var = True if Gemini said so — don't require filtered detections
        # (Gemini may set reklam_var=true even with empty tespitler list)
        detection = {
            "index": analyzed,
            "timestamp": ts_str,
            "seconds": round(ts, 1),
            "frame_url": f"/frames/{video_id}/{frame_filename}",
            "reklam_var": result.get("reklam_var", False),
            "guven": result.get("guven", "Düşük"),
            "markalar": result.get("markalar", []),
            "tespitler": filtered,
            "ozet": result.get("ozet", ""),
            "_api_used": not skip_api,
        }
        detections.append(detection)
        on_add_detection(detection)

        analyzed += 1
        current_frame += frame_step
        msg = f"{title[:35]} · Frame {analyzed}/{total_steps} · API: {api_calls} · {ts_str}"
        status(msg)
        on_set_live(
            status="analyzing",
            progress=round(min(95, (analyzed / total_steps) * 95), 1),
            api_calls=api_calls,
            total_frames=analyzed,
            current_frame=analyzed,
            total_steps=total_steps,
            message=f"Frame {analyzed}/{total_steps} · {ts_str}",
        )

    cap.release()

    # ── Kanal logosu öğrenme ──
    appearance_counts = {}
    for d in detections:
        for t in d.get("tespitler", []):
            marka = t.get("marka", "").strip()
            if not marka:
                continue
            tur = (t.get("tur", "") or "").lower()
            konum = (t.get("konum", "") or "").lower()
            if "köşe" in tur or "logo" in tur or "üst" in konum or "alt" in konum:
                appearance_counts[marka] = appearance_counts.get(marka, 0) + 1

    threshold = max(3, analyzed * 0.30)
    new_logos = [
        m for m, c in appearance_counts.items()
        if c >= threshold and m not in channel_logos
    ]
    if new_logos:
        updated_logos = list(dict.fromkeys(channel_logos + new_logos))
        update_channel_logos(channel_id, updated_logos)

    # ── Özet ve kayıt ──
    ad_detections = [d for d in detections if d["reklam_var"]]
    type_counts = {}
    brand_counts = {}
    for d in ad_detections:
        for t in d.get("tespitler", []):
            tur = t.get("tur", "Bilinmiyor")
            type_counts[tur] = type_counts.get(tur, 0) + 1
            marka = t.get("marka", "").strip()
            if marka:
                brand_counts[marka] = brand_counts.get(marka, 0) + 1

    upsert_video(
        video_id=video_id,
        channel_id=channel_id,
        title=title,
        url=url,
        duration=duration,
        thumbnail=thumb_url,
        analyzed_at=datetime.utcnow().isoformat(),
        total_frames=analyzed,
        api_calls=api_calls,
        ad_frame_count=len(ad_detections),
        type_counts=type_counts,
        brand_counts=brand_counts,
        desc_brands=desc_brands,
        completed=True,
    )
    save_detections(video_id, detections)

    msg = f"✓ Tamamlandı: {title[:35]} · {len(ad_detections)} reklam"
    status(msg)
    on_set_live(
        status="completed", progress=100,
        message=f"Tamamlandı — {len(ad_detections)} reklam tespit",
    )
