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
    TARGET_SAMPLE_FRAMES, MAX_API_FRAMES,
)
from services.gemini import gemini_analyze_batch, gemini_extract_brands
from services.aggregates import compute_aggregates, suggest_channel_logos
from services.youtube import get_ydl_opts, fetch_channel_videos, channel_id_from_url
from models.database import (
    upsert_channel, upsert_video, save_detections,
    get_channel, is_video_completed, update_channel_logos,
    set_channel_brand_flag, recompute_video_aggregates,
    log_event, mark_live_status,
)
from config import AUTO_SPONSOR_THRESHOLD


def _vid_from_url(url):
    """watch?v=ID / youtu.be/ID / shorts/ID → video id (hata anında atıf için)."""
    if not url:
        return ""
    m = (re.search(r"[?&]v=([A-Za-z0-9_\-]{6,})", url)
         or re.search(r"youtu\.be/([A-Za-z0-9_\-]{6,})", url)
         or re.search(r"/(?:shorts|live|embed)/([A-Za-z0-9_\-]{6,})", url))
    return m.group(1) if m else ""


def _classify_error(err):
    """Hata mesajını sınıflandır → (code, kind). kind: 'cookie'|'permanent'|'transient'."""
    e = (err or "").lower()
    if "please sign in" in e or "sign in to confirm" in e or "sign in" in e:
        return "cookie_expired", "cookie"
    if any(k in e for k in ("not available", "private", "removed", "members-only",
                            "video unavailable", "this video is unavailable",
                            "terminated", "deleted")):
        return "video_unavailable", "permanent"
    return "stream_error", "transient"


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
    from config import LIVE_SAMPLE_SECONDS
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    start = time.monotonic()
    # Süresi bilinmeyen (şu an canlı) yayın: sonsuza dek okumamak için sınırlı süre
    # örnekle. Yayını okumak gerçek-zaman hızındadır → bu kadar saniyelik pencere.
    max_secs = max(60, LIVE_SAMPLE_SECONDS)
    while proc.poll() is None:
        time.sleep(1.5)
        if cancel_check and cancel_check():
            proc.kill()
            return []
        if time.monotonic() - start > max_secs:
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

    ex = ThreadPoolExecutor(max_workers=6)
    try:
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
    finally:
        # İptalde bekleyen işleri iptal et — "Tümünü Durdur" anında etki etsin
        ex.shutdown(wait=False, cancel_futures=True)

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


def _cap_candidates(candidate_indices, rep_of, max_n):
    """Aday kare sayısını max_n'e indir (eşit aralıkla örnekle). Atılan adaylara
    bağlı kareler en yakın TUTULAN adaya yeniden eşlenir → API isteği patlamaz.
    ÜCRETSİZ KATMAN günlük kota (RPD) için kritik."""
    import bisect
    if not max_n or len(candidate_indices) <= max_n:
        return candidate_indices, rep_of
    n = len(candidate_indices)
    step = n / float(max_n)
    kept = sorted({candidate_indices[min(n - 1, int(i * step))] for i in range(max_n)})
    kept_set = set(kept)

    def _nearest(idx):
        pos = bisect.bisect_left(kept, idx)
        opts = []
        if pos < len(kept):
            opts.append(kept[pos])
        if pos > 0:
            opts.append(kept[pos - 1])
        return min(opts, key=lambda c: abs(c - idx)) if opts else idx

    new_rep = {i: (rep if rep in kept_set else _nearest(rep)) for i, rep in rep_of.items()}
    return kept, new_rep


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


def _cancelled(job_manager):
    return bool(getattr(job_manager, "_cancel_flag", None)
                and job_manager._cancel_flag.is_set())


def _do_channel_scan(channel_url, last_hours, job_manager, content_type="all"):
    try:
        # Canlı içerik → SIKI tarih penceresi (sadece son `last_hours` saatteki
        # canlı yayınlar). 'all'/'video' → normal liste.
        if content_type == "live":
            from services.youtube import fetch_live_streams
            res = fetch_live_streams(channel_url, last_hours=last_hours)
        else:
            res = fetch_channel_videos(channel_url, last_hours=last_hours,
                                       content_type=content_type)
    except Exception as e:
        print(f"[KANAL-TARAMA] Hata: {e}")
        code, _ = _classify_error(str(e))
        log_event("channel_scan", channel_url, "error", code, str(e))
        return

    channel_id = channel_id_from_url(channel_url)
    channel_name = res["channel_name"] or channel_id

    upsert_channel(
        channel_id, name=channel_name, url=channel_url,
        avatar_url=res.get("channel_avatar", ""),
        last_scanned=datetime.utcnow().isoformat()
    )

    from models.database import mark_live_seen
    added = 0
    for v in res["videos"]:
        if _cancelled(job_manager):     # "Tümünü Durdur" → kuyruğu doldurmayı bırak
            print("[KANAL-TARAMA] iptal edildi — kalan videolar eklenmedi")
            break
        # MANUEL tarama: kullanıcı açıkça istedi → yalnız TAMAMLANMIŞ videoyu atla
        # (gece taraması 'görüldü' demiş olsa bile bekleyen/başarısızı yeniden analiz et).
        if is_video_completed(v["id"]):
            continue
        is_live = bool(v.get("is_live")) or v.get("tab") in ("streams", "live")
        job_manager.add_video(
            v["url"],
            channel_id=channel_id,
            channel_name=channel_name,
        )
        if is_live:
            mark_live_seen(v["id"], channel_id=channel_id,
                           title=v.get("title", ""), url=v.get("url", ""),
                           analyzed=True)
        added += 1
    print(f"[KANAL-TARAMA] {channel_name}: {added} yeni video sıraya alındı")
    log_event("channel_scan", channel_name, "ok", "success",
              f"{added} yeni video sıraya alındı ({len(res['videos'])} bulundu)")


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

    vid_guess = _vid_from_url(url)
    label_box = [url]   # title öğrenilince güncellenir (log etiketi)

    def _record_fail(err, vid=None):
        """Hatayı logla + (canlı yayınsa) live_seen durumunu güncelle."""
        code, kind = _classify_error(str(err))
        log_event("video", label_box[0], "error", code, str(err))
        target = vid or vid_guess
        if target:
            if kind == "cookie":
                mark_live_status(target, "pending", error=str(err))      # cookie düzelince tekrar
            elif kind == "permanent":
                mark_live_status(target, "permanent", error=str(err))    # bir daha deneme
            else:
                mark_live_status(target, "failed", error=str(err), inc_attempt=True)

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
        _record_fail(err)
        on_set_live(status="error", message=err, progress=0)
        return

    if not info:
        status("Video bilgisi alınamadı")
        _record_fail("Video bilgisi alınamadı")
        on_set_live(status="error", message="Video bilgisi alınamadı", progress=0)
        return

    video_id = info.get("id")
    title = info.get("title", "")
    label_box[0] = title or url
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
    brand_aliases = ch.get("brand_aliases", {})
    ignored_brands = ch.get("ignored_brands", [])

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
    #
    #    Datacenter IP'lerde (Railway) YouTube 'web' client'ı çoğu zaman format
    #    döndürmüyor (PO token ister) → "Stream URL bulunamadı". Cookie'li farklı
    #    player client'lar (tv/web_safari/mweb/ios) bu engeli genelde aşar.
    #    Sırayla dene, ilk format vereni kullan; hepsi başarısızsa GERÇEK hatayı
    #    logla (kör "bulunamadı" yerine).
    status(f"Stream alınıyor: {title[:40]}")

    def _pick_stream_url(si):
        # Katı format seçici KULLANMIYORUZ (bazı videolarda hiç eşleşmeyip
        # "Requested format is not available" veriyor). Onun yerine mevcut
        # formatlardan doğrudan URL'li, video codec'li olanı elle seçiyoruz;
        # ≤480p tercih, yoksa en düşük çözünürlük. ffmpeg zaten 640px'e küçültür.
        if not si:
            return None, 0
        if si.get("url"):
            return si["url"], 1
        fmts = si.get("formats") or []
        cand = [f for f in fmts
                if f.get("url") and f.get("vcodec") not in (None, "none")]
        if not cand:
            # DASH birleşik → video parçası
            for rf in (si.get("requested_formats") or []):
                if rf.get("vcodec") not in (None, "none") and rf.get("url"):
                    return rf["url"], len(fmts)
            return None, len(fmts)
        le480 = [f for f in cand if (f.get("height") or 99999) <= 480]
        pool = le480 or cand
        pool.sort(key=lambda f: (f.get("height") or 99999, f.get("tbr") or 0))
        return pool[0]["url"], len(fmts)

    class _YdlLog:
        """yt-dlp'nin uyarılarını yakalar. Asıl ret sebebi (bot kontrolü,
        'missing a url / SABR', PO token uyarısı) burada geliyor; ekrana
        yansımazsa kör kalıyoruz."""
        def __init__(self):
            self.msgs = []
        def debug(self, m):
            pass
        def info(self, m):
            pass
        def warning(self, m):
            self.msgs.append(str(m))
        def error(self, m):
            self.msgs.append(str(m))

    stream_url = None
    _last_err = None
    _diag = []          # her client'ın sonucu — hepsi tek mesajda gösterilir
    for _clients in (["tv"], ["web_safari"], ["web"], ["mweb"],
                     ["ios"], ["android_vr"], ["tv_embedded"]):
        cname = _clients[0]
        log = _YdlLog()
        try:
            with YoutubeDL(get_ydl_opts({
                "skip_download": True,
                "noplaylist": True,
                "ignore_no_formats_error": True,
                "logger": log,
                # get_ydl_opts varsayılanı no_warnings=True — uyarıları tamamen
                # susturuyor ve logger'a hiçbir şey ulaşmıyordu. Asıl ret sebebi
                # (bot kontrolü / SABR / PO token / JS challenge) uyarıda geliyor.
                "no_warnings": False,
                "quiet": False,
                "verbose": False,
                "extractor_args": {"youtube": {"player_client": _clients}},
            })) as ydl:
                si = ydl.extract_info(url, download=False)
            stream_url, nfmt = _pick_stream_url(si)
            if stream_url:
                status(f"Stream OK (client={cname}, format sayısı={nfmt})")
                break
            # Teşhis: video formatı var ama URL'i mi yok (SABR), yoksa hiç mi yok?
            fmts = (si or {}).get("formats") or []
            vids = [f for f in fmts if f.get("vcodec") not in (None, "none")]
            nourl = [f for f in vids if not f.get("url")]
            # En bilgilendirici uyarıyı öne al (jenerik "no formats" satırları değil)
            key_msgs = [m for m in log.msgs if not m.startswith(
                ("No video formats", "Requested format"))]
            warn = " | ".join(m[:180] for m in (key_msgs or log.msgs)[-2:]) or "uyarı yok"
            _last_err = (f"{cname}: format={len(fmts)}, video={len(vids)}, "
                         f"URL'siz video={len(nourl)} → {warn}")
            _diag.append(f"{cname}[f{len(fmts)}/v{len(vids)}/nourl{len(nourl)}] {warn[:110]}")
            status(_last_err)
        except Exception as e:
            warn = " | ".join(m[:180] for m in log.msgs[-2:])
            _last_err = f"{cname}: {str(e)[:200]}" + (f" | {warn}" if warn else "")
            _diag.append(f"{cname}[HATA] {(str(e) or warn)[:110]}")
            status(f"Stream client={cname} başarısız → {_last_err[:240]}")

    if not stream_url:
        msg = ("Stream URL bulunamadı — TÜM CLIENT'LAR: "
               + " ;; ".join(_diag)) if _diag else \
              f"Stream URL bulunamadı — {_last_err or 'hiçbir client format vermedi'}"
        status(msg)
        _record_fail(msg, video_id)
        on_set_live(status="error", message=msg, progress=0)
        return

    # 4. Frame çıkarımı — tek geçiş ffmpeg (yedek: opencv)
    # Uzun videolarda aralığı artır → en fazla ~TARGET_SAMPLE_FRAMES kare çıksın
    # (2.5 saatlik yayın 1100 kare yerine ~200). Süre yoksa (canlı) sabit aralık.
    interval = FRAME_INTERVAL
    if duration and duration > 0 and TARGET_SAMPLE_FRAMES > 0:
        interval = max(FRAME_INTERVAL,
                       -(-int(duration) // TARGET_SAMPLE_FRAMES))  # ceil bölme
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
        _record_fail(e, video_id)
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
        _record_fail("Frame çıkarılamadı", video_id)
        on_set_live(status="error", message="Frame çıkarılamadı", progress=0)
        return

    # 5. Sahne kümeleme + alt-bant yükseltici — sadece adayları Gemini'ye gönder
    candidate_indices, rep_of = _select_candidates(frames_data)
    # Günlük kota güvenliği: aday sayısını sınırla (uzun yayında patlamasın)
    candidate_indices, rep_of = _cap_candidates(candidate_indices, rep_of, MAX_API_FRAMES)
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
            # YALNIZ gerçekten Gemini'ye gönderilen (aday) kare reklam taşır.
            # Aday olmayan komşu kareler MİRAS ALMAZ → "8 karede var ama 2'sinde
            # gerçek" sorunu biter; gösterilen her kare gerçekten incelenmiştir.
            res = results[rep] if fd["index"] in candidate_set else {}

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

        # Kanalın kendi adı da 'reklam sayma' listesinde (prompt'a gider);
        # bilinen ana sponsorlar bağlam olarak enjekte edilir (kural → prompt)
        prompt_logos = list(dict.fromkeys(
            ([channel_name] if channel_name else []) + channel_logos))
        batch_res = gemini_analyze_batch(api_key, batch_frames,
                                          prompt_logos, desc_brands,
                                          main_sponsors=main_sponsors)
        # ── Günlük kota (RPD) doldu mu? Saatlerce 429 beklemek yerine HIZLI dur ──
        if any((r or {}).get("ozet") == "QUOTA_DAILY" for r in batch_res.values()):
            status("Gemini GÜNLÜK kota doldu — analiz durduruldu, yarın devam")
            log_event("video", title or url, "error", "quota_daily",
                      "Gemini günlük istek kotası (RPD) doldu")
            mark_live_status(video_id, "pending", error="Gemini günlük kota doldu")
            try:
                from services.job_manager import _set_pause
                _set_pause(6 * 3600)   # gece otomatik taramayı 6 saat duraklat
            except Exception:
                pass
            on_set_live(status="error",
                        message="Gemini günlük kota doldu — yarın devam eder", progress=0)
            return
        results.update(batch_res)
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

    # ── Özet ve kayıt (ortak agregat modülü, kanal bayrakları + öğrenilen kurallar) ──
    agg = compute_aggregates(detections, channel_logos, main_sponsors, active_only,
                             brand_aliases=brand_aliases, ignored_brands=ignored_brands,
                             channel_name=channel_name or "",
                             auto_main_sponsors=ch.get("auto_main_sponsors", []))

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

    # TÜM kareler diskte kalır (kullanıcı temiz kareleri de inceleyebilsin —
    # komşu karelere bakarak doğrulama yapıyor). Yer sınırı yalnızca cap aşılınca
    # EN ESKİ video klasörlerini budayarak yönetilir (kanıt-kare silme YOK).
    try:
        from services.storage import frame_maintenance
        frame_maintenance()
    except Exception as e:
        print(f"[FRAME] bakım atlandı: {e}")

    save_detections(video_id, detections)

    # ── Otomatik ANA SPONSOR: eşik üstü VEYA sürekli ekranda olan (köşe logosu /
    #    title sponsor, ör. A101) marka şişik veridir; ana sponsor + köşe-logosu-
    #    sayma işaretle → sayımdan düşür, sadece gerçek reklamlar (alt bant) kalsın ──
    from services.aggregates import auto_sponsor_candidates
    auto = auto_sponsor_candidates(agg, AUTO_SPONSOR_THRESHOLD, main_sponsors)
    if auto:
        for m in auto:
            set_channel_brand_flag(channel_id, m, "main_sponsor", True)
            set_channel_brand_flag(channel_id, m, "active_only", True)
            set_channel_brand_flag(channel_id, m, "auto_main_sponsor", True)  # rozet
        status(f"Otomatik ana sponsor: {', '.join(auto)}")
        agg = recompute_video_aggregates(video_id) or agg  # bayraklarla yeniden hesapla

    msg = f"✓ Tamamlandı: {title[:35]} · {agg['ad_frame_count']} reklam"
    status(msg)
    # Başarı: logla + (canlı yayınsa) durumu 'done' yap
    log_event("video", title or url, "ok", "success",
              f"{agg['ad_frame_count']} reklam · {analyzed} kare")
    mark_live_status(video_id, "done")
    on_set_live(
        status="completed", progress=100,
        message=f"Tamamlandı — {agg['ad_frame_count']} reklam tespit",
    )
