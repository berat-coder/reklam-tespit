"""
Video analiz ve kanal tarama görevleri.
RQ worker'lardan (process_video_rq / process_channel_scan_rq)
ve thread worker'lardan (process_video_sync / process_channel_scan_sync) çağrılır.
"""

import re
import time
import base64
import shutil
import subprocess
from datetime import datetime

import cv2
import numpy as np

from config import (
    load_config, FRAMES_DIR,
    FRAME_INTERVAL, FRAME_WIDTH, BATCH_SIZE,
    SCENE_DIFF_THRESHOLD, LOWER_BAND_THRESHOLD,
)
from services.gemini import gemini_analyze_batch, gemini_extract_brands
from services.aggregates import compute_aggregates, suggest_channel_logos
from services.youtube import get_ydl_opts, fetch_channel_videos, channel_id_from_url
from models.database import (
    upsert_channel, upsert_video, save_detections,
    get_channel, is_video_completed, update_channel_logos,
    set_channel_brand_flag, recompute_video_aggregates,
)
from config import AUTO_SPONSOR_THRESHOLD


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


def _b64_file(path):
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


# ── Frame çıkarımı ──────────────────────────────────────────────────────────────

def _ffmpeg_seek_frame(stream_url, out_path, t, width):
    """Tek bir zaman noktasından `-ss` (input seek = HTTP range) ile 1 frame çeker.
    Tüm videoyu indirmez — sadece o anın etrafındaki birkaç KB'ı indirir."""
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-ss", str(t), "-i", stream_url,
        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4",
        "-y", str(out_path),
    ]
    try:
        subprocess.run(cmd, timeout=90,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out_path if out_path.exists() and out_path.stat().st_size > 0 else None
    except Exception:
        return None


def _extract_frames_ffmpeg_linear(stream_url, frames_dir, interval, width,
                                  expected=0, on_progress=None, cancel_check=None):
    """Süre bilinmiyorsa (ör. canlı yayın) yedek: tek geçiş fps filtresiyle çıkarır."""
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-an",
        "-i", stream_url,
        "-vf", f"fps=1/{interval},scale={width}:-2",
        "-q:v", "4",
        str(frames_dir / "frame_%04d.jpg"),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    start = time.monotonic()
    while proc.poll() is None:
        time.sleep(1.5)
        if cancel_check and cancel_check():
            proc.kill()
            return []
        if time.monotonic() - start > 1800:
            proc.kill()
            break
        if on_progress:
            on_progress(len(list(frames_dir.glob("frame_*.jpg"))), expected)
    out = []
    for p in sorted(frames_dir.glob("frame_*.jpg")):
        m = re.search(r"frame_(\d+)\.jpg", p.name)
        n = int(m.group(1)) if m else len(out) + 1
        out.append((p, (n - 1) * interval))
    return out


def _extract_frames_ffmpeg(stream_url, frames_dir, interval, width, duration=0,
                           expected=0, on_progress=None, cancel_check=None):
    """Her örnek noktasına PARALEL `-ss` seek ile frame çıkarır — çok hızlı,
    sadece gerekli byte'ları indirir. Süre yoksa lineer fps yöntemine düşer.
    Döner: [(Path, ts_saniye), ...]"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not duration or duration <= 0:
        return _extract_frames_ffmpeg_linear(
            stream_url, frames_dir, interval, width,
            expected=expected, on_progress=on_progress, cancel_check=cancel_check)

    timestamps = list(range(0, int(duration), interval))
    if not timestamps:
        timestamps = [0]
    results = [None] * len(timestamps)
    done = 0

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {}
        for idx, t in enumerate(timestamps):
            out_path = frames_dir / f"frame_{idx + 1:04d}.jpg"
            futs[ex.submit(_ffmpeg_seek_frame, stream_url, out_path, t, width)] = (idx, t)
        for fut in as_completed(futs):
            idx, t = futs[fut]
            p = fut.result()
            if p:
                results[idx] = (p, t)
            done += 1
            if on_progress:
                on_progress(done, len(timestamps))
            if cancel_check and cancel_check():
                break

    return [r for r in results if r]


def _extract_frames_opencv(stream_url, frames_dir, interval, width):
    """ffmpeg yoksa yedek: OpenCV ile seek ederek frame çıkarır."""
    cap = cv2.VideoCapture(stream_url)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    frame_step = max(1, int(fps * interval))
    out = []
    current, n = 0, 0
    while True:
        cap.set(cv2.CAP_PROP_POS_FRAMES, current)
        ret, frame = cap.read()
        if not ret:
            break
        ts = current / fps if fps > 0 else n * interval
        fh, fw = frame.shape[:2]
        if fw > width:
            frame = cv2.resize(frame, (width, int(fh * width / fw)))
        p = frames_dir / f"frame_{n + 1:04d}.jpg"
        cv2.imwrite(str(p), frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        out.append((p, ts))
        n += 1
        current += frame_step
    cap.release()
    return out


def _extract_frames(stream_url, frames_dir, interval, width, duration=0,
                    expected=0, on_progress=None, cancel_check=None):
    if shutil.which("ffmpeg"):
        try:
            res = _extract_frames_ffmpeg(stream_url, frames_dir, interval, width,
                                         duration=duration, expected=expected,
                                         on_progress=on_progress,
                                         cancel_check=cancel_check)
            if res:
                return res
            print("[VIDEO] ffmpeg 0 frame döndü, opencv'ye düşülüyor")
        except Exception as e:
            print(f"[VIDEO] ffmpeg başarısız ({e}), opencv'ye düşülüyor")
    return _extract_frames_opencv(stream_url, frames_dir, interval, width)


def _select_candidates(frames_data):
    """Sahne kümeleme + alt-bant yükseltici ile Gemini'ye gönderilecek frame'leri seçer.
    Döner: (candidate_indices, rep_of) — rep_of[i] = i'nin sonucunu miras alacağı aday index'i."""
    candidates = []
    rep_of = {}
    rep_idx, rep_img, prev_img = None, None, None
    for fd in frames_data:
        i, img = fd["index"], fd["img"]
        if rep_idx is None:
            is_cand = True
        else:
            scene_change = _frame_diff(prev_img, img) >= SCENE_DIFF_THRESHOLD
            band_change = _lower_diff(rep_img, img) >= LOWER_BAND_THRESHOLD
            is_cand = scene_change or band_change
        if is_cand:
            candidates.append(i)
            rep_idx, rep_img = i, img
        rep_of[i] = rep_idx
        prev_img = img
    return candidates, rep_of


# ── Kanal tarama ──────────────────────────────────────────────────────────────

def process_channel_scan_rq(channel_url, last_hours=24, content_type="all"):
    """RQ worker'dan çağrılır. Kanalı tarayıp videoları sıraya ekler."""
    from services.job_manager import JOB_MANAGER
    _do_channel_scan(channel_url, last_hours, JOB_MANAGER, content_type)


def process_channel_scan_sync(job, _api_key, job_manager):
    """Thread worker'dan çağrılır."""
    job_manager._status = "scanning_channel"
    job_manager._message = f"Kanal taranıyor: {job['url']}"
    _do_channel_scan(job["url"], job.get("last_hours", 24), job_manager,
                     job.get("content_type", "all"))


def _do_channel_scan(channel_url, last_hours, job_manager, content_type="all"):
    try:
        res = fetch_channel_videos(channel_url, last_hours=last_hours,
                                   content_type=content_type)
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
    main_sponsors = ch.get("main_sponsors", [])
    active_only = ch.get("sponsor_active_only", [])

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

    # 3. Stream URL — düşük çözünürlük yeterli (640px'e küçültüyoruz) ve çok daha
    #    hızlı decode olur. H.264 mp4 tercih → AV1/VP9'dan kat kat hızlı.
    status(f"Stream alınıyor: {title[:40]}")
    stream_url = None
    try:
        with YoutubeDL(get_ydl_opts({
            "skip_download": True,
            "noplaylist": True,
            "ignore_no_formats_error": True,
            "format": (
                "bv*[height<=480][vcodec^=avc]/b[height<=480][vcodec^=avc]/"
                "bv*[height<=480]/b[height<=480]/worst[vcodec!=none]/worst"
            ),
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

    # 4. Frame çıkarımı — tek geçiş ffmpeg (yedek: opencv)
    interval = FRAME_INTERVAL
    job_frames_dir = FRAMES_DIR / video_id
    job_frames_dir.mkdir(exist_ok=True)

    expected_frames = max(1, int((duration or 0) / interval)) if duration else 0
    status(f"Frame'ler çıkarılıyor: {title[:35]}")
    on_set_live(status="extracting", message="Frame'ler çıkarılıyor...",
                total_steps=expected_frames, progress=0)

    def _extract_progress(n, total):
        pct = round(min(25, (n / total) * 25), 1) if total else 0
        on_set_live(status="extracting",
                    progress=pct, current_frame=n, total_frames=n,
                    total_steps=total or n,
                    message=f"Frame çıkarılıyor: {n}" + (f"/{total}" if total else ""))
        if on_status:
            on_status(f"Frame çıkarılıyor: {n}/{total or '?'}")

    try:
        frame_files = _extract_frames(
            stream_url, job_frames_dir, interval, FRAME_WIDTH,
            duration=duration, expected=expected_frames,
            on_progress=_extract_progress, cancel_check=cancel_check,
        )
    except Exception as e:
        status(f"Frame çıkarma hatası: {e}")
        on_set_live(status="error", message=str(e), progress=0)
        return

    if cancel_check():
        return

    # Diff/ön-filtre için frame'leri belleğe yükle
    frames_data = []
    for idx, (path, ts) in enumerate(frame_files):
        img = cv2.imread(str(path))
        if img is None:
            continue
        frames_data.append({"index": idx, "path": path, "ts": ts, "img": img})

    total_frames = len(frames_data)
    if total_frames == 0:
        status("Frame çıkarılamadı — video erişilemiyor olabilir")
        on_set_live(status="error", message="Frame çıkarılamadı", progress=0)
        return

    # 5. Sahne kümeleme + alt-bant yükseltici — sadece adayları Gemini'ye gönder
    candidate_indices, rep_of = _select_candidates(frames_data)
    candidate_set = set(candidate_indices)
    status(f"{total_frames} kare · {len(candidate_indices)} aday "
           f"(sahne+bant filtresi sonrası)")
    on_set_live(total_steps=total_frames, total_frames=0,
                message=f"{total_frames} kare · {len(candidate_indices)} analiz edilecek")

    analyzed = 0
    api_calls = 0
    detections = []
    ad_appearances = {}
    results = {}      # frame index -> gemini sonucu
    emit_pos = 0

    def flush_ready(final=False):
        nonlocal emit_pos
        while emit_pos < total_frames:
            fd = frames_data[emit_pos]
            rep = rep_of[fd["index"]]
            if rep not in results:
                break
            res = results[rep]

            filtered = []
            for t in res.get("tespitler", []):
                norm = _normalize_brand(t.get("marka", "")) + "|" + (t.get("tur", "") or "")
                if len(ad_appearances.get(norm, [])) < 3:
                    filtered.append(t)
                    ad_appearances.setdefault(norm, []).append(fd["index"])

            detection = {
                "index": fd["index"],
                "timestamp": _fmt_ts(fd["ts"]),
                "seconds": round(fd["ts"], 1),
                "frame_url": f"/frames/{video_id}/{fd['path'].name}",
                "reklam_var": res.get("reklam_var", False),
                "guven": res.get("guven", "Düşük"),
                "markalar": res.get("markalar", []),
                "tespitler": filtered,
                "ozet": res.get("ozet", ""),
                "_api_used": fd["index"] in candidate_set,
            }
            detections.append(detection)
            on_add_detection(detection)
            emit_pos += 1

    # 6. Batch'ler hâlinde Gemini analizi
    for b in range(0, len(candidate_indices), BATCH_SIZE):
        if cancel_check():
            return
        chunk = candidate_indices[b:b + BATCH_SIZE]
        batch_frames = [{
            "index": ci,
            "timestamp": _fmt_ts(frames_data[ci]["ts"]),
            "b64": _b64_file(frames_data[ci]["path"]),
        } for ci in chunk]

        results.update(gemini_analyze_batch(api_key, batch_frames,
                                            channel_logos, desc_brands))
        api_calls += 1
        flush_ready()

        msg = (f"{title[:35]} · {emit_pos}/{total_frames} kare · "
               f"API: {api_calls} · {len(candidate_indices)} aday")
        status(msg)
        on_set_live(
            status="analyzing",
            progress=round(min(95, 25 + (emit_pos / total_frames) * 70), 1),
            api_calls=api_calls,
            total_frames=emit_pos,
            current_frame=emit_pos,
            total_steps=total_frames,
            message=f"{emit_pos}/{total_frames} kare · {api_calls} API çağrısı",
        )

    flush_ready(final=True)
    analyzed = len(detections)

    # ── Kanal logosu öğrenme (ortak modül) ──
    new_logos = suggest_channel_logos(detections, channel_logos, analyzed)
    if new_logos:
        updated_logos = list(dict.fromkeys(channel_logos + new_logos))
        update_channel_logos(channel_id, updated_logos)
        channel_logos = updated_logos

    # ── Özet ve kayıt (ortak agregat modülü, mevcut sponsor/logo bayraklarıyla) ──
    agg = compute_aggregates(detections, channel_logos, main_sponsors, active_only)

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
        ad_frame_count=agg["ad_frame_count"],
        type_counts=agg["type_counts"],
        brand_counts=agg["brand_counts"],
        persistent_overlays=agg["persistent_overlays"],
        desc_brands=desc_brands,
        completed=True,
    )
    save_detections(video_id, detections)

    # ── Otomatik ANA SPONSOR: bir videoda eşik üstü görünen marka şişik veridir;
    #    ana sponsor + köşe-logosu-sayma işaretle, sayımdan düşür, detayda kalsın ──
    sponsor_keys = {s.casefold() for s in main_sponsors}
    auto = [m for m, c in agg["brand_counts"].items()
            if c >= AUTO_SPONSOR_THRESHOLD and m.casefold() not in sponsor_keys]
    if auto:
        for m in auto:
            set_channel_brand_flag(channel_id, m, "main_sponsor", True)
            set_channel_brand_flag(channel_id, m, "active_only", True)
        status(f"Otomatik ana sponsor: {', '.join(auto)}")
        agg = recompute_video_aggregates(video_id) or agg  # bayraklarla yeniden hesapla

    msg = f"✓ Tamamlandı: {title[:35]} · {agg['ad_frame_count']} reklam"
    status(msg)
    on_set_live(
        status="completed", progress=100,
        message=f"Tamamlandı — {agg['ad_frame_count']} reklam tespit",
    )
