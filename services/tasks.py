"""
Video analiz ve kanal tarama görevleri.
RQ worker'lardan (process_video_rq / process_channel_scan_rq)
ve thread worker'lardan (process_video_sync / process_channel_scan_sync) çağrılır.
"""

import os
import re
import json
import time
import base64
import shutil
import subprocess
import threading
from datetime import datetime

import cv2
import numpy as np

from config import (
    load_config, FRAMES_DIR,
    FRAME_INTERVAL, FRAME_WIDTH, SOURCE_MIN_HEIGHT, BATCH_SIZE,
    SCENE_DIFF_THRESHOLD, LOWER_BAND_THRESHOLD,
    TARGET_SAMPLE_FRAMES, MAX_API_FRAMES,
    FRAME_SEEK_WORKERS, FRAME_SEEK_TIMEOUT, FRAME_SEEK_FAST,
    FRAME_SEEK_RW_TIMEOUT_SEC, BRAND_TUR_FRAME_CAP,
    OPENCV_FALLBACK_TIMEOUT,
)
from services import frame_sync
from services.gemini import gemini_analyze_batch, gemini_extract_brands
from services.aggregates import compute_aggregates, suggest_channel_logos
from services.youtube import get_ydl_opts, fetch_channel_videos, channel_id_from_url
from models.database import (
    upsert_channel, upsert_video, save_detections,
    get_channel, is_video_completed, update_channel_logos,
    log_event, mark_live_status, mark_live_seen, set_live_wait,
    get_live_attempts, _exposure_map, kv_get, kv_set,
)



def _is_real_completion(frame_count, duration):
    """Analiz gerçekten tamamlandı mı? Kare çıkarımı çökmüşse (ör. 2 saatlik
    videodan 1 kare) bunu 'tamamlandı' saymak videoyu kalıcı olarak görünmez
    yapar. Eşik: beklenen karenin ~%10'u, en az 3 kare."""
    try:
        d = float(duration or 0)
        f = int(frame_count or 0)
    except (TypeError, ValueError):
        return False
    if f <= 0:
        return False
    if d <= 0:                      # süre bilinmiyor (canlı) → 3 kare yeter
        return f >= 3
    # Beklenen kare sayısı, çıkarıcının GERÇEK aralığıyla hesaplanmalı.
    # Aralık TARGET_SAMPLE_FRAMES'e göre büyütülüyor (tasks.py:852): uzun
    # videoda 8 sn değil, ör. 2 saatlik yayında ~35 sn. Sabit 8 varsaymak
    # beklenen kareyi 4-5 KAT şişirir ve BAŞARILI analizleri "eksik" sayıp
    # sonsuza dek yeniden taratır (kota israfı).
    interval = max(FRAME_INTERVAL, -(-int(d) // max(1, TARGET_SAMPLE_FRAMES)))
    expected = max(1.0, d / interval)
    return f >= max(3, int(expected * 0.10))


def _vid_from_url(url):
    """watch?v=ID / youtu.be/ID / shorts/ID → video id (hata anında atıf için)."""
    if not url:
        return ""
    m = (re.search(r"[?&]v=([A-Za-z0-9_\-]{6,})", url)
         or re.search(r"youtu\.be/([A-Za-z0-9_\-]{6,})", url)
         or re.search(r"/(?:shorts|live|embed)/([A-Za-z0-9_\-]{6,})", url))
    return m.group(1) if m else ""


# Aynı canlı yayın en fazla bu kadar denenir; sonra 'failed' olur.
# Üretimde tavan YOKTU: cookie olarak sınıflanan hata her tick'te (~80 sn)
# yeniden analiz tetikliyordu — tek videoda 18 deneme, sonsuz döngü ve
# boşa Gemini kotası.
MAX_LIVE_ATTEMPTS = 5


def _classify_error(err):
    """Hata mesajını sınıflandır → (code, kind). kind: 'cookie'|'permanent'|'transient'."""
    e = (err or "").lower()
    if "please sign in" in e or "sign in to confirm" in e or "sign in" in e:
        # "cookie" sınıfı SONSUZ yeniden denemeye açıktır ("cookie düzelince
        # tekrar"). Bu yüzden yalnız cookie GERÇEKTEN kullanılıyorsa geçerli:
        # YT_USE_COOKIES=0 ise düzelecek bir cookie yok, sonsuz beklemek
        # anlamsız. Ayrıca 7 client'tan yalnız biri "sign in" derken diğerleri
        # başka sebep söylüyorsa (ör. "No title found in player responses")
        # bunu cookie sorunu saymak yanlış sınıflandırmaydı.
        from services.youtube import _use_cookies
        cookie_relevant = _use_cookies() and "no title found" not in e
        if cookie_relevant:
            return "cookie_expired", "cookie"
        return "stream_error", "transient"
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

def _ffmpeg_proxy_args():
    """YouTube stream URL'leri, onları ÜRETEN IP'ye kilitlidir. yt-dlp URL'i
    YT_PROXY üzerinden aldıysa ffmpeg de AYNI proxy'den indirmek zorunda —
    yoksa googlevideo 403 döner. `-http_proxy` bir girdi (protokol) seçeneğidir,
    `-i`'den ÖNCE gelmeli."""
    proxy = os.environ.get("YT_PROXY", "").strip()
    return ["-http_proxy", proxy] if proxy else []


# Seek hatalarını AZ ama YETERLİ logla: 198 başarısız seek 198 satır basmasın,
# ama sebep de kaybolmasın. Video başına ilk _SEEK_ERR_MAX benzersiz hata yazılır.
_SEEK_ERR_MAX = 3
# Onarım geçişi yalnız bu orandan AZ kare boşsa çalışır (üstü = bozuk akış).
REPAIR_MAX_MISSING_RATIO = 0.5
# Seek'ler ThreadPoolExecutor içinde koştuğu için sayaç KİLİTLİ olmalı.
_seek_lock = threading.Lock()
_seek_errs = {"n": 0, "seen": set(), "lines": []}


def reset_seek_errors():
    with _seek_lock:
        _seek_errs["n"] = 0
        _seek_errs["seen"] = set()
        _seek_errs["lines"] = []


def seek_error_summary(sep=" | "):
    """Toplanan ffmpeg sebeplerini metin olarak döndür.

    Bu ÖNEMLİ: sebepler eskiden yalnız print ile Railway loguna gidiyordu,
    kaydedilen hata ise düpedüz "Frame çıkarılamadı" idi. Üretimde 52 kayıt
    böyle teşhissiz kaldı — 403 mü, zaman aşımı mı, IP kilidi mi, anlaşılmıyordu.
    Artık sebep hata mesajına ekleniyor."""
    with _seek_lock:
        return sep.join(_seek_errs["lines"])[:600]


def _log_seek_error(raw):
    msg = (raw or b"").decode("utf-8", "replace").strip()
    if not msg:
        return
    # Son anlamlı satır genelde asıl sebeptir (403, DNS, timeout...)
    line = [l for l in msg.splitlines() if l.strip()][-1][:200]
    key = re.sub(r"\d+", "#", line)[:80]     # sayıları sil → aynı hata tekrar etmesin
    with _seek_lock:
        if _seek_errs["n"] >= _SEEK_ERR_MAX or key in _seek_errs["seen"]:
            return
        _seek_errs["seen"].add(key)
        _seek_errs["lines"].append(line)
        _seek_errs["n"] += 1
    print(f"[VIDEO] ffmpeg seek hatası: {line}")


# ── YouTube hız sınırı (bot-flag / 429) geri çekilmesi ───────────────────────
# Railway'in datacenter IP'si belirli bir istek hacminden sonra YouTube
# tarafından bot olarak işaretleniyor: tüm client'lar "Sign in to confirm
# you're not a bot" + 0 format döndürüyor, biri de HTTP 429 alıyor.
# ÖLÇÜLDÜ (2026-09-01): aynı videolar yerel bağlantıdan PO token olmadan bile
# sorunsuz çekiliyor → video erişilebilir, engel IP kaynaklı ve hacme bağlı.
#
# Sistem bunu KENDİ KENDİNE derinleştiriyordu: bir video bot-flag alınca kod
# kalan 6 client'ı da deniyor (hepsi aynı cevabı alıyor) ve sıradaki videoya
# geçip yine 7 istek atıyordu. Logda 4 dakikada 3 video × 7 = 21 işaretli istek.
_RATE_PAT = re.compile(
    r"sign in to confirm|not a bot|http error 429|too many requests", re.I)
_COOLDOWN_KEY = "yt_rate_limit"
def _cd_env(name, default):
    try:
        return max(60, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


YT_COOLDOWN_BASE = _cd_env("YT_COOLDOWN_BASE_SEC", 600)      # ilk bekleme 10 dk
YT_COOLDOWN_MAX = _cd_env("YT_COOLDOWN_MAX_SEC", 7200)       # tavan 2 saat


def is_rate_limit_msg(msg):
    """Mesaj YouTube'un bot-flag/429 cevabı mı?"""
    return bool(_RATE_PAT.search(msg or ""))


def yt_cooldown_remaining():
    """Bot-flag sonrası kalan bekleme (sn); 0 ise serbest."""
    try:
        st = kv_get(_COOLDOWN_KEY, {}) or {}
        return max(0, int(float(st.get("until") or 0) - time.time()))
    except Exception:
        return 0


def note_rate_limit():
    """Bot-flag/429 görüldü → üstel geri çekilme başlat. Döner: bekleme (sn)."""
    try:
        st = kv_get(_COOLDOWN_KEY, {}) or {}
        streak = int(st.get("streak") or 0) + 1
    except Exception:
        streak = 1
    wait = min(YT_COOLDOWN_MAX, YT_COOLDOWN_BASE * (2 ** (streak - 1)))
    try:
        kv_set(_COOLDOWN_KEY, {"until": time.time() + wait, "streak": streak})
    except Exception:
        pass
    print(f"[VIDEO] YouTube hız sınırı ({streak}. kez) — {wait // 60} dk "
          f"boyunca YouTube isteği yapılmayacak")
    return wait


def clear_rate_limit():
    """Başarılı çekimden sonra geri çekilmeyi sıfırla."""
    try:
        if (kv_get(_COOLDOWN_KEY, {}) or {}).get("streak"):
            kv_set(_COOLDOWN_KEY, {"until": 0, "streak": 0})
    except Exception:
        pass


def _stream_url_ok(url, headers=None, timeout=10):
    """Seçilen akış URL'si GERÇEKTEN çekilebiliyor mu? İlk 1 KB istenir.

    Bir client URL DÖNDÜRÜP o URL 403 verebiliyor. Üretimde ölçüldü
    (xn6yUkD2hGg): mweb itag 18 URL'si veriyor ama googlevideo 403 Forbidden
    diyor; aynı videoda tv_simply'nin URL'si sorunsuz kare veriyor. İstemci
    sırasında mweb önce geldiği için kod çalışmayan URL'yi kabul edip aramayı
    durduruyordu → "Frame çıkarılamadı" (üretimde 52 kayıt). "URL bulundu"
    ile "URL çalışıyor" aynı şey değil.

    Döner: (ok, sebep)."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url)
    req.add_header("Range", "bytes=0-1023")
    for k, v in (headers or {}).items():
        if v and k.lower() not in ("host", "range", "accept-encoding", "connection"):
            try:
                req.add_header(k, str(v))
            except (ValueError, TypeError):
                pass
    # Mevcut _ffmpeg_proxy_args ile aynı kaynak: ortam değişkeni.
    proxy = os.environ.get("YT_PROXY", "").strip()
    try:
        opener = (urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            if proxy else urllib.request.build_opener())
        with opener.open(req, timeout=timeout) as r:
            code = getattr(r, "status", None) or r.getcode()
            return code in (200, 206), f"HTTP {code}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, type(e).__name__


def _ffmpeg_header_args(headers):
    """yt-dlp'nin o format için ürettiği HTTP başlıklarını ffmpeg'e aktar.

    KRİTİK: bunlar aktarılmadığında ffmpeg kendi varsayılan kimliğiyle
    (Lavf/...) istek atıyor ve YouTube'un kenar sunucusu, tarayıcı istemcisi
    için üretilmiş URL'ye 403 döndürüyor. Üretimde ölçülen hata birebir buydu:
    "Frame çıkarılamadı — ffmpeg: Error opening input files: Server returned
    403 Forbidden (access denied)" — 52 kayıt bu sebeple battı.

    Host/Range/Accept-Encoding ffmpeg'in kendi işi; onları geçmiyoruz."""
    if not headers:
        return []
    ua = ""
    other = {}
    for k, v in headers.items():
        if not v:
            continue
        kl = k.lower()
        if kl == "user-agent":
            ua = str(v)
        elif kl in ("host", "range", "accept-encoding", "connection"):
            continue
        else:
            other[k] = str(v)
    args = []
    if ua:
        args += ["-user_agent", ua]
    if other:
        args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in other.items())]
    return args


def _ffmpeg_seek_frame(stream_url, out_path, t, width, fast=True, headers=None):
    """Tek bir zaman noktasından `-ss` (input seek = HTTP range) ile 1 frame çeker.
    Tüm videoyu indirmez — sadece o anın etrafındaki byte'ları indirir.

    fast=True (keyframe modu): -noaccurate_seek + -skip_frame nokey → t'den önceki
    en yakın keyframe alınır. Hassas mod keyframe'den t'ye kadar TÜM 1080p kareleri
    decode ediyordu (kare başı 2-4 sn CPU) ve aradaki byte'ları da indiriyordu;
    keyframe modu ikisini de sıfırlar. Zaman kayması ≤ ~5 sn (YouTube keyint),
    örnekleme aralığı ≥8 sn olduğundan kare kaybı/tekrarı olmaz."""
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        *_ffmpeg_proxy_args(),
        *_ffmpeg_header_args(headers),
        # Takılan ağ okuması slotu bekletmesin; index + range tek TCP bağlantısını
        # yeniden kullansın; kopan bağlantı sessizce yeniden denensin.
        "-rw_timeout", str(FRAME_SEEK_RW_TIMEOUT_SEC * 1_000_000),
        "-multiple_requests", "1",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        *(["-noaccurate_seek", "-skip_frame", "nokey"] if fast else []),
        "-ss", str(t), "-i", stream_url,
        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4",
        "-y", str(out_path),
    ]
    try:
        # stderr YAKALANIYOR: eskiden DEVNULL'a gidiyordu ve seek'ler boş
        # döndüğünde SEBEBİ hiç görünmüyordu ("ffmpeg 0 frame döndü" deyip
        # opencv'ye düşüyorduk, kör kalıyorduk). 403 mü, DNS mi, PO token mı,
        # anlaşılmıyordu. Artık ilk birkaç hata özetlenip loglanıyor.
        r = subprocess.run(cmd, timeout=FRAME_SEEK_TIMEOUT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if out_path.exists() and out_path.stat().st_size > 0:
            return out_path
        _log_seek_error(r.stderr)
        return None
    except subprocess.TimeoutExpired:
        _log_seek_error(b"zaman asimi (FRAME_SEEK_TIMEOUT)")
        return None
    except Exception as e:
        _log_seek_error(str(e).encode("utf-8", "replace"))
        return None


def _extract_frames_ffmpeg_linear(stream_url, frames_dir, interval, width,
                                  expected=0, on_progress=None, cancel_check=None,
                                  headers=None):
    """Süre bilinmiyorsa (ör. canlı yayın) yedek: tek geçiş fps filtresiyle çıkarır."""
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-an",
        *_ffmpeg_proxy_args(),
        *_ffmpeg_header_args(headers),
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
                           expected=0, on_progress=None, cancel_check=None,
                           headers=None):
    """Her örnek noktasına PARALEL `-ss` seek ile frame çıkarır — çok hızlı,
    sadece gerekli byte'ları indirir. Süre yoksa lineer fps yöntemine düşer.
    Döner: [(Path, ts_saniye), ...]"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not duration or duration <= 0:
        return _extract_frames_ffmpeg_linear(
            stream_url, frames_dir, interval, width,
            expected=expected, on_progress=on_progress, cancel_check=cancel_check,
            headers=headers)

    timestamps = list(range(0, int(duration), interval))
    if not timestamps:
        timestamps = [0]
    results = [None] * len(timestamps)
    cancelled = False

    def _run_pass(indices, fast, workers, done_offset=0):
        nonlocal cancelled
        done = done_offset
        ex = ThreadPoolExecutor(max_workers=workers)
        try:
            futs = {}
            for idx in indices:
                out_path = frames_dir / f"frame_{idx + 1:04d}.jpg"
                futs[ex.submit(_ffmpeg_seek_frame, stream_url, out_path,
                               timestamps[idx], width, fast, headers)] = idx
            for fut in as_completed(futs):
                idx = futs[fut]
                p = fut.result()
                if p:
                    results[idx] = (p, timestamps[idx])
                done += 1
                if on_progress:
                    on_progress(min(done, len(timestamps)), len(timestamps))
                if cancel_check and cancel_check():
                    cancelled = True
                    break
        finally:
            # İptalde bekleyen işleri iptal et — "Tümünü Durdur" anında etki etsin
            ex.shutdown(wait=False, cancel_futures=True)

    fast = bool(FRAME_SEEK_FAST)

    # ÖN YOKLAMA — akış URL'si ölüyse (403 / süresi geçmiş imza / IP kilidi)
    # 121 seek'in HEPSİ boş döner, ardından onarım geçişi aynı 121 noktayı
    # yavaş modda TEKRAR dener. Üretimde bunun sonucu 97 dakikalık yayından
    # 1 kare + boşa bir Gemini çağrısı + iki tur bant genişliği oldu.
    # Videoya yayılmış birkaç noktayı önce dene: hiçbiri kare vermiyorsa
    # URL bozuk demektir, kalan seek'leri hiç yapma.
    # Son yoklama noktası videonun EN SONUNA yapışmasın: bildirilen süre
    # gerçek akıştan birkaç saniye uzunsa (canlı/DVR kayıtlarında olağan) o
    # nokta boş döner ve yanlışlıkla "ölü akış" kararına katkı yapar.
    # Ölçüldü: 858 sn'lik videoda son nokta sona 2 sn kalıyordu. %90'da kes.
    _son = int((len(timestamps) - 1) * 0.9)
    probe = sorted({int(i * _son / 3) for i in range(4)}) \
        if len(timestamps) >= 8 else []
    if probe:
        _run_pass(probe, fast=fast, workers=min(len(probe), FRAME_SEEK_WORKERS))
        if not cancelled and not any(results[i] for i in probe):
            print(f"[VIDEO] ön yoklama {len(probe)} noktada da kare vermedi — akış "
                  f"URL'si erişilemez; kalan {len(timestamps) - len(probe)} seek "
                  f"ATLANDI. Sebep: {seek_error_summary() or 'bilinmiyor'}")
            return []

    if not cancelled:
        rest = [i for i in range(len(timestamps)) if results[i] is None]
        _run_pass(rest, fast=fast, workers=FRAME_SEEK_WORKERS,
                  done_offset=len(timestamps) - len(rest))

    # Onarım geçişi: hızlı (keyframe) modda başarısız kalan noktaları bir kez
    # hassas modla dene — bazı format/proxy kombinasyonlarında keyframe seek
    # tek tük 403/EOF verebiliyor. "Tek tük" şartı artık ZORUNLU: çoğunluk
    # boşken akış zaten bozuktur, tekrar denemek maliyeti ikiye katlar.
    if fast and not cancelled:
        missing = [i for i, r in enumerate(results) if r is None]
        got = len(timestamps) - len(missing)
        if missing and got and len(missing) <= len(timestamps) * REPAIR_MAX_MISSING_RATIO:
            print(f"[VIDEO] hızlı seek {len(missing)} karede boş kaldı — hassas modla onarılıyor")
            _run_pass(missing, fast=False, workers=4, done_offset=got)
        elif missing:
            print(f"[VIDEO] {len(missing)}/{len(timestamps)} kare boş, alınan {got} — "
                  f"onarım geçişi ATLANDI (akış bozuk görünüyor). "
                  f"Sebep: {seek_error_summary() or 'bilinmiyor'}")

    return [r for r in results if r]


def _extract_frames_opencv(stream_url, frames_dir, interval, width,
                           cancel_check=None, deadline_sec=OPENCV_FALLBACK_TIMEOUT):
    """ffmpeg yoksa/0 kare döndürdüyse yedek: OpenCV ile seek ederek kare çıkarır.

    DİKKAT — bu yol daha önce ÜRETİMİ KİLİTLİYORDU: cv2.VideoCapture canlı bir
    HLS akışına zaman aşımı OLMADAN bağlanmaya çalışıyor ve süresiz bekliyordu.
    ffmpeg (IP bloğu yüzünden) 0 kare döndürdüğünde her iş buraya düşüp tek
    işçi slotunu sonsuza dek işgal ediyordu → kuyruk tıkanıyor, "hiçbir tarama
    başarılı olmuyor". Artık: açılış/okuma zaman aşımı + toplam süre sınırı +
    iptal kontrolü var; süre dolarsa elde ne varsa onunla döner."""
    import time as _t
    start = _t.monotonic()
    # FFMPEG backend'i açılış/okuma zaman aşımını destekler (ms). Eski OpenCV
    # sürümlerinde sabitler olmayabilir → varsa uygula.
    params = []
    for name, val in (("CAP_PROP_OPEN_TIMEOUT_MSEC", 15000),
                      ("CAP_PROP_READ_TIMEOUT_MSEC", 15000)):
        pid = getattr(cv2, name, None)
        if pid is not None:
            params += [int(pid), int(val)]
    try:
        cap = (cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG, params) if params
               else cv2.VideoCapture(stream_url))
    except Exception:
        cap = cv2.VideoCapture(stream_url)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    frame_step = max(1, int(fps * interval))
    out = []
    current, n = 0, 0
    while True:
        # Süre sınırı ve iptal — biri devreye girerse elde olanla dön
        if _t.monotonic() - start > deadline_sec:
            print(f"[VIDEO] opencv yedeği {deadline_sec}sn sınırına takıldı "
                  f"({n} kare) — işçi kilitlenmesin diye bırakılıyor")
            break
        if cancel_check and cancel_check():
            break
        cap.set(cv2.CAP_PROP_POS_FRAMES, current)
        ret, frame = cap.read()
        if not ret:
            break
        ts = current / fps if fps > 0 else n * interval
        fh, fw = frame.shape[:2]
        if fw > width:
            frame = cv2.resize(frame, (width, int(fh * width / fw)))
        p = frames_dir / f"frame_{n + 1:04d}.jpg"
        # 88: küçük marka yazıları JPEG artefaktında kaybolmasın (ffmpeg -q:v 4
        # ile aynı kalite bandı). Yedek (opencv) yol da aynı okunurlukta olmalı.
        cv2.imwrite(str(p), frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
        out.append((p, ts))
        n += 1
        current += frame_step
    cap.release()
    return out


def _extract_frames(stream_url, frames_dir, interval, width, duration=0,
                    expected=0, on_progress=None, cancel_check=None, headers=None):
    reset_seek_errors()   # hata sayacı her VİDEO için sıfırlanır
    if shutil.which("ffmpeg"):
        try:
            res = _extract_frames_ffmpeg(stream_url, frames_dir, interval, width,
                                         duration=duration, expected=expected,
                                         on_progress=on_progress,
                                         cancel_check=cancel_check,
                                         headers=headers)
            if res:
                return res
            print("[VIDEO] ffmpeg 0 frame döndü, opencv'ye düşülüyor")
        except Exception as e:
            print(f"[VIDEO] ffmpeg başarısız ({e}), opencv'ye düşülüyor")
    return _extract_frames_opencv(stream_url, frames_dir, interval, width,
                                  cancel_check=cancel_check)


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
    return _do_channel_scan(channel_url, last_hours, JOB_MANAGER, content_type)


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
        return {"ok": False, "error": str(e)[:200], "found": 0, "queued": 0}

    channel_id = channel_id_from_url(channel_url)
    channel_name = res["channel_name"] or channel_id

    upsert_channel(
        channel_id, name=channel_name, url=channel_url,
        avatar_url=res.get("channel_avatar", ""),
        last_scanned=datetime.utcnow().isoformat()
    )

    added = 0
    waiting = 0
    for v in res["videos"]:
        if _cancelled(job_manager):     # "Tümünü Durdur" → kuyruğu doldurmayı bırak
            print("[KANAL-TARAMA] iptal edildi — kalan videolar eklenmedi")
            break
        # MANUEL tarama: kullanıcı açıkça istedi → yalnız TAMAMLANMIŞ videoyu atla
        # (gece taraması 'görüldü' demiş olsa bile bekleyen/başarısızı yeniden analiz et).
        if is_video_completed(v["id"]):
            continue
        was_live = v.get("tab") in ("streams", "live")
        if v.get("is_live"):
            # Yayın SÜRÜYOR → kuyruklama; bitince zamanlayıcı tam analize gönderir
            # (canlı-kenar örneklemesi yalnız ~10 dk görür, yanlış veri üretir).
            mark_live_seen(v["id"], channel_id=channel_id,
                           title=v.get("title", ""), url=v.get("url", ""))
            set_live_wait(v["id"])
            waiting += 1
            continue
        job_manager.add_video(
            v["url"],
            channel_id=channel_id,
            channel_name=channel_name,
            title=v.get("title", ""),
        )
        if was_live:
            # 'done' DEĞİL — analiz gerçekten bitince tasks.py 'done' yazar;
            # erken 'done' UI'da videos kaydı olmayan "analiz edildi" 404 linki üretiyordu.
            mark_live_seen(v["id"], channel_id=channel_id,
                           title=v.get("title", ""), url=v.get("url", ""))
            mark_live_status(v["id"], "queued")
        added += 1
    wait_note = f" · {waiting} canlı (bitince analiz)" if waiting else ""
    print(f"[KANAL-TARAMA] {channel_name}: {added} yeni video sıraya alındı{wait_note}")
    log_event("channel_scan", channel_name, "ok", "success",
              f"{added} yeni video sıraya alındı ({len(res['videos'])} bulundu){wait_note}")
    # SONUCU DÖNDÜR: RQ bunu işin result'ına yazar, /api/job/<id> okur ve UI
    # gerçek sonucu gösterir. Eskiden UI yalnız "Tarama başladı" diyordu; iş
    # 6 saniye sonra "0 yeni video" ile bitiyor ama kullanıcıya HİÇBİR bildirim
    # gitmiyordu → "sistem tarama yapmıyor" algısının başlıca sebebi buydu.
    return {"ok": True, "channel": channel_name, "found": len(res["videos"]),
            "queued": added, "waiting": waiting,
            "content_type": content_type, "hours": last_hours}


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
                # TAVAN: pending'e geri koymak sonsuz döngü demek. Tavana
                # gelindiyse 'failed' yaz → zamanlayıcı bir daha kuyruğa almaz.
                # inc_attempt YOK: sayaç kuyruğa alınırken (scheduler
                # _analyze_one) zaten arttı. İkisi birden artırınca tek gerçek
                # deneme sayacı 2 artıyordu; "attempts < 3" eşiği pratikte TEK
                # denemeye izin veriyor, kayıtlar attempts=4'te kalıcı mahsur
                # kalıyordu (üretimde 42 kayıt tam orada donmuştu).
                if get_live_attempts(target) >= MAX_LIVE_ATTEMPTS:
                    mark_live_status(target, "failed",
                                     error=f"[{MAX_LIVE_ATTEMPTS} deneme aşıldı] {err}")
                else:
                    mark_live_status(target, "pending", error=str(err))
            elif kind == "permanent":
                mark_live_status(target, "permanent", error=str(err))    # bir daha deneme
            else:
                mark_live_status(target, "failed", error=str(err))

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

    # ── Canlı yayın kontrolü ──
    # Yayın SÜRERKEN yapılan analiz yalnız canlı-kenardan ~10 dk örnekler, sahte
    # zaman damgaları üretir ve agregatları çarpıtır. Zamanlayıcı bu videoları hiç
    # buraya göndermez (live_wait); yine de gelirse (manuel URL analizi) KISMİ
    # ÖNİZLEME olarak işaretlenir ve yayın bitince tam analiz üstüne yazar.
    live_status = info.get("live_status") or ("is_live" if info.get("is_live") else "")
    if live_status == "is_upcoming":
        msg = "Yayın henüz başlamadı — başlayıp bitince otomatik analiz edilecek"
        status(msg)
        mark_live_seen(video_id, channel_id=channel_id or "", title=title, url=url)
        set_live_wait(video_id)
        on_set_live(status="error", message=msg, progress=0)
        return
    is_partial = bool(live_status == "is_live" or duration <= 0)
    if is_partial:
        status("⚠️ Canlı yayın — ilk ~10 dk önizleme; yayın bitince tam analiz yapılır")
        mark_live_seen(video_id, channel_id=channel_id or "", title=title, url=url)
        set_live_wait(video_id)   # bitince tam analiz planlansın

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
        # formatlardan doğrudan URL'li, video codec'li olanı elle seçiyoruz.
        #
        # ÇÖZÜNÜRLÜK: eskiden ≤480p seçilip kare 640px'e küçültülüyordu. Köşe
        # sponsor logosunun MARKA YAZISI bu boyutta okunmuyor ve model tahmin
        # yürütüyordu (Migros Hemen → "n11" → "Misli"). 480p kaynağı 1280'e
        # büyütmek de işe yaramaz; olmayan detay geri gelmez. Bu yüzden 720p
        # (SOURCE_MIN_HEIGHT, varsayılan 1080p) tercih edilir; kare 1280'e
        # KÜÇÜLTÜLÜR (upscale değil) → metin keskin kalır. Bant genişliği artar
        # ama analiz normal internet bağlantısındaki işçide çalışıyor.
        if not si:
            return None, 0, 0, False, None
        if si.get("url"):
            return (si["url"], 1, si.get("height") or 0, False,
                    si.get("http_headers"))
        fmts = si.get("formats") or []
        cand = [f for f in fmts
                if f.get("url") and f.get("vcodec") not in (None, "none")]
        if not cand:
            # DASH birleşik → video parçası
            for rf in (si.get("requested_formats") or []):
                if rf.get("vcodec") not in (None, "none") and rf.get("url"):
                    return (rf["url"], len(fmts), rf.get("height") or 0, False,
                            rf.get("http_headers") or si.get("http_headers"))
            return None, len(fmts), 0, False, None
        # ── HLS'i ELE: ffmpeg `-ss` seek'i HLS manifest'inde çalışmıyor.
        # Üretimde ölçüldü: itag 96 (HLS) seçilince her seek
        # "Error when loading first segment" + "Invalid data found when
        # processing input" veriyor → 0 kare. DASH/progressive URL'ler tek
        # dosya + HTTP range olduğu için seek ile sorunsuz.
        def _is_hls(f):
            return (f.get("protocol") or "").startswith("m3u8") or \
                   ".m3u8" in (f.get("url") or "")
        non_hls = [f for f in cand if not _is_hls(f)]
        hls_only = not non_hls              # yalnız HLS → ZAYIF sonuç
        pool_all = non_hls or cand          # hiç DASH yoksa mecburen HLS

        # ── BİTRATE: en YÜKSEK tbr'yi seçmek pahalı ve gereksizdi.
        # Ölçüm (nettop, %0.24 hata): aynı videoda aynı 1080p içinde seçenekler
        # 4841k ile 1247k arasında — 3.9x fark. Düşük bitrate'li (AV1/VP9)
        # 1080p karede sponsor yazısı hâlâ net okunuyor ama seek başına veri
        # 5.80 MB yerine 0.29 MB. Bu yüzden hedefi karşılayan EN UCUZ varyant
        # seçilir (aylık ~810 GB → ~58 GB).
        target = SOURCE_MIN_HEIGHT
        ge = [f for f in pool_all if (f.get("height") or 0) >= target]
        if ge:
            ge.sort(key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 1e9)))
            return (ge[0]["url"], len(fmts), ge[0].get("height") or 0, hls_only,
                    ge[0].get("http_headers") or si.get("http_headers"))
        pool_all.sort(key=lambda f: (-(f.get("height") or 0), (f.get("tbr") or 1e9)))
        return (pool_all[0]["url"], len(fmts), pool_all[0].get("height") or 0,
                hls_only, pool_all[0].get("http_headers") or si.get("http_headers"))

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
    sheaders = None
    _last_err = None
    _weak = [None]      # yalnız-HLS sonucu: son çare olarak saklanır
    _diag = []          # her client'ın sonucu — hepsi tek mesajda gösterilir
    # CLIENT SIRASI — ölçüme göre (yt-dlp 2026.8.19 + deno/EJS ile):
    #   web_safari → TAM DASH [144…1080], PO token'a bile gerek yok  ← kazanan
    #   mweb, tv_simply → çalışır ama GVS PO token ister (bgutil verebilir)
    #   web → YouTube SABR zorluyor, çoğu zaman yalnız 360p (yt-dlp#12482)
    #   android_vr → JS gerektirmez ama DASH'i bgutil'den token ALAMAZ (yapısal:
    #                eklentinin _SUPPORTED_CLIENTS listesi yalnız WebPO client'ları)
    #                → tek 360p progressive; son çare olarak sonda
    #   tv → bu sürümde her koşulda "The page needs to be reloaded" veriyor
    # NOT: "tv_embedded" bu yt-dlp sürümünün INNERTUBE_CLIENTS'ında YOK — ölü
    # kayıttı, boşa bir tur döndürüyordu; çıkarıldı.
    for _clients in (["web_safari"], ["mweb"], ["tv_simply"], ["web"],
                     ["ios"], ["tv"], ["android_vr"]):
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
            stream_url, nfmt, sheight, hls_only, sheaders = _pick_stream_url(si)
            # ZAYIF SONUÇ: yalnız HLS bulunduysa kabul ETME, diğer client'ları
            # dene. Üretimde ölçüldü: web_safari tek bir HLS formatı (itag 96)
            # döndürüyor, ffmpeg seek onu okuyamıyor ("Error when loading first
            # segment") → 0 kare; oysa mweb aynı videoda 33 DASH formatı
            # veriyordu. İlk cevap veren client'ı kabul etmek hatalıydı.
            if stream_url and hls_only:
                if _weak[0] is None:
                    _weak[0] = (stream_url, nfmt, sheight, cname, sheaders)
                status(f"{cname}: yalnız HLS bulundu (seek uyumsuz) — "
                       f"diğer client'lar deneniyor")
                stream_url = None
            # ÇEKİLEBİLİRLİK DOĞRULAMASI: URL'nin varlığı yetmez. 1 KB'lik
            # deneme isteği 403 dönerse bu client'ı kabul etme, sıradakini dene.
            # Bu adım olmadan mweb'in 403'lü URL'si tv_simply'nin çalışan
            # URL'sinin önüne geçiyordu.
            if stream_url and not hls_only:
                ok, why = _stream_url_ok(stream_url, sheaders)
                if not ok:
                    if _weak[0] is None:
                        _weak[0] = (stream_url, nfmt, sheight, cname, sheaders)
                    status(f"{cname}: URL var ama çekilemiyor ({why}) — "
                           f"diğer client'lar deneniyor")
                    _diag.append(f"{cname}[url-{why.replace(' ', '')}]")
                    stream_url = None
            if stream_url:
                # Seçilen KAYNAK çözünürlüğü loglanır: köşe logolarının marka
                # yazısının okunabilmesi buna bağlı (düşükse tespit tahmine döner)
                clear_rate_limit()      # çalışıyoruz → geri çekilme sıfırlansın
                status(f"Stream OK (client={cname}, kaynak={sheight or '?'}p, "
                       f"format sayısı={nfmt})")
                if sheight and sheight < SOURCE_MIN_HEIGHT:
                    status(f"UYARI: kaynak yalnız {sheight}p — küçük marka "
                           f"yazıları okunamayabilir (hedef {SOURCE_MIN_HEIGHT}p)")
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
            # HIZ SINIRI: bot-flag/429 alındıysa KALAN client'ları deneme.
            # Hepsi aynı cevabı verir; 7 istek atmak yalnız engeli derinleştirir
            # (üretim logu: 4 dakikada 3 video × 7 = 21 işaretli istek).
            if is_rate_limit_msg(warn) or is_rate_limit_msg(_last_err):
                bekle = note_rate_limit()
                _last_err = (f"YouTube hız sınırı (bot doğrulaması / 429) — "
                             f"{bekle // 60} dk beklenecek. Son: {_last_err[:150]}")
                status(_last_err)
                break
        except Exception as e:
            warn = " | ".join(m[:180] for m in log.msgs[-2:])
            _last_err = f"{cname}: {str(e)[:200]}" + (f" | {warn}" if warn else "")
            _diag.append(f"{cname}[HATA] {(str(e) or warn)[:110]}")
            status(f"Stream client={cname} başarısız → {_last_err[:240]}")
            if is_rate_limit_msg(str(e)) or is_rate_limit_msg(warn):
                bekle = note_rate_limit()
                _last_err = (f"YouTube hız sınırı (bot doğrulaması / 429) — "
                             f"{bekle // 60} dk beklenecek. Son: {_last_err[:150]}")
                break

    if not stream_url and _weak[0]:
        # Hiçbir client DASH vermedi → son çare olarak HLS'i dene (0 kare
        # riski var ama hiç denememekten iyi).
        stream_url, nfmt, sheight, wname, sheaders = _weak[0]
        status(f"Stream OK (client={wname}, kaynak={sheight or '?'}p, "
               f"ZAYIF kaynak — HLS ya da doğrulanamayan URL, kare gelmeyebilir)")

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
            headers=sheaders,
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
        # Sebebi mesaja EKLE: 52 üretim kaydı düpedüz "Frame çıkarılamadı"
        # yazıyordu ve neden başarısız olduğu hiçbir yerde görünmüyordu.
        why = seek_error_summary()
        detail = "Frame çıkarılamadı" + (f" — ffmpeg: {why}" if why else
                                         " (ffmpeg sebep bildirmedi)")
        status(detail[:200])
        _record_fail(detail, video_id)
        on_set_live(status="error", message=detail[:300], progress=0)
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
                if len(ad_appearances.get(norm, [])) < BRAND_TUR_FRAME_CAP:
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
            # İşçi modunda kare Railway'e yüklenir (panelde kanıt kareleri
            # eksiksiz görünsün). Tek makine kurulumunda no-op.
            frame_sync.queue_frame(video_id, fd["path"])
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
                                          prompt_logos, desc_brands)
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
        brand_exposure=_exposure_map(agg),   # süre/olay → panel, trend, EMV
        desc_brands=desc_brands,
        # ── "TAMAMLANDI" YALANI ──
        # Eskiden completed=True KOŞULSUZ yazılıyordu. Kare çıkarımı başarısız
        # olsa bile (2 saatlik videoda 1 kare) kayıt "tamamlandı" sayılıyor ve
        # is_video_completed onu KALICI olarak eliyordu → kullanıcı o kanalı
        # tekrar taradığında video bir daha ASLA sıraya girmiyordu.
        # Üretimde bu şekilde 16 çöp kayıt oluşmuştu; kullanıcının "kanal
        # taraması hiçbir şey yapmıyor" şikayetinin sebeplerinden biri buydu.
        # Artık: beklenenin çok altında kare çıktıysa tamamlanmış SAYILMAZ,
        # bir sonraki taramada yeniden denenir.
        completed=_is_real_completion(analyzed, duration),
        is_partial=is_partial or not _is_real_completion(analyzed, duration),
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
    # NOT: Burada eskiden, bu videoda sürekli görünen marka KALICI KANAL KURALI
    # olarak yazılıyordu (main_sponsor + active_only). İki yönden yanlıştı:
    #  1) Bir kanalın her yayınında farklı sponsor olabilir — tek videodan
    #     çıkarılan gözlem kanalın tamamına genellenemez.
    #  2) Yanlış okunan bir marka (Migros Hemen → n11) kalıcı kural olunca
    #     prompt'a "bu kanalın ana sponsoru n11" diye geri besleniyor ve model
    #     sonraki taramalarda hatayı yüksek güvenle tekrarlıyordu.
    # Kalıcı logo baskılaması artık compute_aggregates içinde VİDEO BAZINDA
    # yapılıyor (auto_persistent) — kanal kuralı yazmaya gerek yok.
    # Elle işaretlenen ana sponsorlar (kullanıcı kararı) aynen geçerli.

    # İşçi modu: kalan kare yüklemeleri bitsin (panelde eksik kare kalmasın)
    if frame_sync.enabled():
        status("Kareler panele yükleniyor...")
        frame_sync.wait(180)

    if is_partial:
        msg = f"✓ Önizleme tamamlandı: {title[:35]} · {agg['ad_frame_count']} reklam (ilk ~10 dk)"
        status(msg)
        log_event("video", title or url, "ok", "partial",
                  f"canlı önizleme · {agg['ad_frame_count']} reklam · {analyzed} kare "
                  "— yayın bitince tam analiz yapılacak")
        # 'done' YAZMA: live_wait kalır → yayın bitince tam analiz kuyruğa girer
        set_live_wait(video_id)
        on_set_live(
            status="completed", progress=100,
            message=f"Önizleme tamamlandı — {agg['ad_frame_count']} reklam "
                    "(yayın bitince tam analiz yapılacak)",
        )
        return

    msg = f"✓ Tamamlandı: {title[:35]} · {agg['ad_frame_count']} reklam"
    status(msg)
    # Başarı: logla + (canlı yayınsa) durumu 'done' yap
    log_event("video", title or url, "ok", "success",
              f"{agg['ad_frame_count']} reklam · {analyzed} kare")
    mark_live_status(video_id, "done")

    # 2. model doğrulaması: reklam bulunan kareleri farklı bir görsel modele
    # tekrar sorar (yanlış pozitif avcısı). Hata birincil analizi ASLA bozmaz.
    if agg["ad_frame_count"] > 0:
        try:
            from services.job_manager import JOB_MANAGER
            JOB_MANAGER.add_verify(video_id)
        except Exception as e:
            print(f"[DOĞRULAMA] kuyruklanamadı (atlandı): {e}")

    on_set_live(
        status="completed", progress=100,
        message=f"Tamamlandı — {agg['ad_frame_count']} reklam tespit",
    )


# ── 2. model doğrulaması (yanlış pozitif avcısı) ──────────────────────────────

def process_verify_rq(video_id):
    """RQ worker'dan çağrılır (yeni iş türü — mevcut imzalar değişmedi)."""
    _verify_video_core(video_id)


def process_verify_sync(job, _api_key, job_manager):
    """Thread worker'dan çağrılır."""
    _verify_video_core(job.get("video_id", ""))


def _fetch_frame_from_web(video_id, fname, dest):
    """Worker diski geçici (redeploy'da silinir) — kare lokalde yoksa panelden
    (web) worker-token ile indir. Başarıysa dest'e yazar, True döner."""
    base = os.environ.get("FRAME_UPLOAD_URL", "").strip().rstrip("/")
    token = os.environ.get("WORKER_TOKEN", "").strip()
    if not base or not token:
        return False
    try:
        import requests
        r = requests.get(f"{base}/api/worker/frame/{video_id}/{fname}",
                         headers={"X-Worker-Token": token}, timeout=30)
        if r.status_code == 200 and r.content:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            return True
    except Exception:
        pass
    return False


_VERIFY_PROMPT = """GÖREV: Aşağıdaki karelerde bir yapay zeka REKLAM tespit etti.
Sen ikinci bir denetçisin. Her kare için soruyu yanıtla:

Bu karedeki marka görünümü YAYINA EKLENMİŞ/KASITLI bir reklam mı (overlay,
banner, alt bant, sponsor bandı, tam ekran reklam, kanalın stüdyosuna kasıtlı
yerleştirilmiş ürün), yoksa TESADÜFİ bir marka görünümü mü?

TESADÜFİ sayılır (reklam DEĞİLDİR):
- Sporcunun GİYDİĞİ formadaki sponsor markası (futbol/voleybol/basketbol fark etmez)
- Basın toplantısı masasındaki su şişesi/bardak — konuşmacının içme suyu
- Kulübün basın panosu / backdrop / stadyum tabelası
- Reklamın içinde görünen başka ürünlerin markaları, pazaryeri logoları
- Kanal logosu, kulüp arması, lig/turnuva logosu

(TASK: You are a second reviewer. For each frame decide whether the brand
appearance is a DELIBERATE broadcast ad (overlay/banner/sponsor strip/product
placement staged by the channel) or an INCIDENTAL appearance (athlete's jersey
sponsor, a water bottle on a press-conference desk, club press backdrop,
marketplace logo, channel/club identity). Incidental is NOT an ad.)

İDDİALAR (frame → tespitler):
{claims}

YANIT — SADECE şu JSON dizisi, başka hiçbir şey yazma:
[{{"frame": <numara>, "karar": "reklam" | "tesadufi" | "belirsiz", "neden": "<kısa Türkçe gerekçe>"}}]
Her kare için TEK nesne. Emin değilsen "belirsiz" de."""


def _verify_video_core(video_id):
    """Reklam bayraklı kareleri ikinci modele sorar, 'tesadufi' çıkanların güvenini
    'Düşük'e indirir (sayımdan düşer, kanıt kalır) ve agregatları yeniden hesaplar.
    Tamamı hata-yalıtımlıdır: ne olursa olsun birincil analiz sonucu değişmez."""
    try:
        from services.vision_providers import (
            get_verifier, VERIFY_BATCH_SIZE, verify_budget_left, verify_budget_spend)
        from models.database import (
            get_detections, update_detection_verify, recompute_video_aggregates)

        cfg = load_config()
        verifier = get_verifier(cfg)
        if verifier is None:
            return   # anahtar yok → doğrulama devre dışı (sessiz)

        excluded = set(cfg.get("excluded_placements") or [])
        dets = get_detections(video_id)
        targets = []
        for d in dets:
            if not d.get("reklam_var") or d.get("manual_clean"):
                continue
            if d.get("verify_status"):
                continue   # daha önce bakılmış
            tespitler = d.get("tespitler") or []
            # Yalnız SAYILAN tespiti olan kareler (Forma/Ürün Markası zaten sayılmıyor)
            if tespitler and not any((t.get("tur") or "") not in excluded for t in tespitler):
                continue
            targets.append(d)
        if not targets:
            return

        print(f"[DOĞRULAMA] {video_id}: {len(targets)} reklam karesi "
              f"{verifier.name}/{verifier.model} ile denetleniyor")
        confirmed = rejected = uncertain = 0
        for b in range(0, len(targets), VERIFY_BATCH_SIZE):
            chunk = targets[b:b + VERIFY_BATCH_SIZE]
            if verify_budget_left() <= 0:
                print("[DOĞRULAMA] günlük bütçe doldu — kalan kareler atlandı")
                break
            frames, claims = [], []
            for d in chunk:
                fname = (d.get("frame_url") or "").rsplit("/", 1)[-1]
                fpath = FRAMES_DIR / video_id / fname
                if not fname:
                    continue
                if not fpath.exists() and not _fetch_frame_from_web(video_id, fname, fpath):
                    continue   # kare ne lokalde ne panelde — atla
                frames.append({"index": d["index"], "b64": _b64_file(fpath)})
                ts = "; ".join(
                    f"{t.get('marka') or '?'} ({t.get('tur') or '?'}, {t.get('konum') or '?'})"
                    for t in (d.get("tespitler") or [])) or ", ".join(d.get("markalar") or [])
                claims.append(f"KARE {d['index']}: {ts or 'marka bilgisi yok'}")
            if not frames:
                continue
            prompt = _VERIFY_PROMPT.format(claims="\n".join(claims))
            parsed, err = verifier.analyze_frames(frames, prompt)
            verify_budget_spend()
            if err == "QUOTA_DAILY":
                print("[DOĞRULAMA] sağlayıcı günlük kotası doldu — durduruldu")
                break
            if err or not isinstance(parsed, (list, dict)):
                print(f"[DOĞRULAMA] batch hatası (atlandı): {err or 'parse'}")
                continue
            if isinstance(parsed, dict):
                parsed = [parsed]
            verdicts = {v.get("frame"): v for v in parsed if isinstance(v, dict)}
            for d in chunk:
                v = verdicts.get(d["index"])
                if not v:
                    continue
                karar = (v.get("karar") or "").strip().lower()
                notes = json.dumps({
                    "provider": verifier.name, "model": verifier.model,
                    "karar": karar, "neden": (v.get("neden") or "")[:200],
                    "ts": datetime.utcnow().isoformat(),
                }, ensure_ascii=False)
                if karar == "reklam":
                    update_detection_verify(video_id, d["index"], "confirmed", notes)
                    confirmed += 1
                elif karar == "tesadufi":
                    update_detection_verify(video_id, d["index"], "rejected", notes,
                                            guven="Düşük")
                    rejected += 1
                else:
                    update_detection_verify(video_id, d["index"], "uncertain", notes)
                    uncertain += 1

        if rejected:
            recompute_video_aggregates(video_id)
        if confirmed or rejected or uncertain:
            log_event("video", video_id, "ok", "verified",
                      f"2. model ({verifier.name}): {confirmed} onay · "
                      f"{rejected} ret · {uncertain} belirsiz")
            print(f"[DOĞRULAMA] {video_id}: {confirmed} onay, {rejected} ret, "
                  f"{uncertain} belirsiz")
    except Exception as e:
        print(f"[DOĞRULAMA] hata (birincil analiz etkilenmedi): {e}")
