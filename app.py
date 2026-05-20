"""
app.py - YouTube Reklam Tespit Dashboard v3
Kanal bazlı multi-video analiz, akıllı kanal logosu öğrenme
"""

import os
import sys
import re
import json
import base64
import threading
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory

try:
    import cv2
    import numpy as np
    import requests
    from yt_dlp import YoutubeDL
except ImportError as e:
    print("\n[HATA] Eksik paket:", e.name)
    print("pip install flask yt-dlp opencv-python numpy requests\n")
    sys.exit(1)


# ─── Yapılandırma ─────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
CONFIG_FILE  = BASE_DIR / "config.json"
DATA_FILE    = BASE_DIR / "data.json"  # tüm analiz verileri burada
STATIC_DIR   = BASE_DIR / "static"
FRAMES_DIR   = BASE_DIR / "frames"
FRAMES_DIR.mkdir(exist_ok=True)


def load_config():
    cfg = {"gemini_api_key": "", "channels": []}
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    # Railway environment variable'dan oku
    env_key = os.environ.get("GEMINI_API_KEY", "")
    if env_key:
        cfg["gemini_api_key"] = env_key
    return cfg


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get_ydl_opts(extra=None):
    """Ortak yt-dlp seçenekleri - cookie desteği ile"""
    opts = {
        "quiet": True,
        "no_warnings": True,
    }
    # Cookie dosyası varsa ekle
    cookie_file = BASE_DIR / "cookies.txt"
    if cookie_file.exists():
        opts["cookiefile"] = str(cookie_file)
    if extra:
        opts.update(extra)
    return opts
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "channels": {},   # { channel_id: {name, url, channel_logos: [], last_analyzed: ..., videos: {video_id: {...}} } }
    }


def save_data(data):
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


DATA_LOCK = threading.Lock()

def get_data():
    with DATA_LOCK:
        return load_data()

def update_data(updater):
    """updater(data) -> data fonksiyonunu çağırır, kilitleyerek kaydeder."""
    with DATA_LOCK:
        data = load_data()
        data = updater(data) or data
        save_data(data)
        return data


# ─── Gemini ───────────────────────────────────────────────────────────────────
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/"
    f"models/{GEMINI_MODEL}:generateContent"
)


def gemini_call(api_key, payload, max_attempts=3):
    for attempt in range(max_attempts):
        try:
            r = requests.post(
                f"{GEMINI_URL}?key={api_key}", json=payload, timeout=60
            )
            if r.status_code == 200:
                return r.json(), None
            if r.status_code == 429:
                wait = 30
                print(f"[429] {wait}s bekleniyor")
                time.sleep(wait)
                continue
            if r.status_code in (500, 502, 503, 504):
                wait = 10
                print(f"[{r.status_code}] {wait}s bekleniyor")
                time.sleep(wait)
                continue
            return None, f"API hata {r.status_code}: {r.text[:120]}"
        except Exception as e:
            if attempt < max_attempts - 1:
                time.sleep(5)
                continue
            return None, f"Bağlantı: {str(e)[:60]}"
    return None, "Sürekli yoğun"


def parse_json_safe(text):
    """4 katmanlı JSON parse"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    cleaned = text.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(1)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1:
        cand = cleaned[start:end+1].replace("\n", " ")
        cand = re.sub(r",(\s*[}\]])", r"\1", cand)
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
    # son çare
    has_ad = bool(re.search(r'"reklam_var"\s*:\s*true', text, re.IGNORECASE))
    markalar = []
    mm = re.search(r'"markalar"\s*:\s*\[(.*?)\]', text, re.DOTALL)
    if mm:
        markalar = [m.strip() for m in re.findall(r'"([^"]+)"', mm.group(1))]
    return {
        "reklam_var": has_ad or bool(markalar),
        "guven": "Orta" if (has_ad or markalar) else "Düşük",
        "markalar": markalar,
        "tespitler": [],
        "ozet": "regex parse"
    }


def gemini_analyze_frame(api_key, image_b64, channel_logos, known_brands, timestamp):
    """Tek frame'i analiz et"""
    ctx = ""
    if channel_logos:
        ctx += f"\n\n🚫 KANALIN KENDİ LOGOLARI (BUNLARI REKLAM SAYMA): {', '.join(channel_logos[:10])}"
    if known_brands:
        ctx += f"\n📌 Video açıklamasında geçen markalar: {', '.join(known_brands[:10])}"

    prompt = f"""YouTube video frame'i — zaman: {timestamp}{ctx}

GÖREV: Bu görüntüde HARİCİ REKLAM, SPONSOR veya MARKA YERLEŞTİRME var mı?

✅ REKLAM SAYILAN:
- Alt bant'ta marka logosu/sloganı (örn: "Migros Hemen", "Doyuyo Anlatılmaz Yenir")
- Köşelerde harici marka logosu (kanalın kendi logosu DEĞİL)
- Sponsor bandı, kampanya yazısı, indirim kodu
- URL, "kod ile indirimli", "tıkla", "satın al"
- Bahis, oyun, bonus, hoşgeldin paketi
- Konuşmacının kasıtlı tuttuğu marka/ürün
- Arka plan reklam panoları

🚫 REKLAM SAYMA:
- Kanalın kendi logosu (yukarıda listelenenler)
- Program adı bandı (sadece "Türkiye'nin Gurme" gibi)
- Konuşmacı/misafir isim tagları
- Sosyal medya hesabı tagleri

🔥 ÇOK ÖNEMLİ:
- Markayı NET okuyabiliyorsan YAZ. EMİN değilsen boş bırak, UYDURMA!
- "Sanslı", "doyu" gibi yarı okunan markalara dikkat - doğrusunu yaz: "Sanall", "Doyuyo"
- Tespit kategorisi NET olsun: "Alt Bant", "Köşe Logo", "Sponsor Bandı", "Ürün Yerleştirme", "Arka Plan", "Banner"

YANIT — SADECE JSON:
{{
  "reklam_var": true/false,
  "guven": "Yüksek/Orta/Düşük",
  "markalar": ["NET marka adları, kanal logosu hariç"],
  "tespitler": [
    {{"tur": "Alt Bant|Köşe Logo|Sponsor Bandı|Ürün Yerleştirme|Banner|Arka Plan",
      "konum": "sağ üst|sol üst|sağ alt|sol alt|alt orta|üst orta|merkez",
      "marka": "marka", "detay": "kısa"}}
  ],
  "ozet": "tek cümle"
}}"""

    payload = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
            {"text": prompt}
        ]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 2000,
            "responseMimeType": "application/json"
        }
    }
    data, err = gemini_call(api_key, payload)
    if err:
        return {"reklam_var": False, "guven": "Düşük", "markalar": [],
                "tespitler": [], "ozet": err, "_skipped": True}
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return parse_json_safe(text)
    except Exception as e:
        return {"reklam_var": False, "guven": "Düşük", "markalar": [],
                "tespitler": [], "ozet": f"Hata: {str(e)[:60]}", "_skipped": True}


def gemini_extract_brands(api_key, title, description):
    text = f"Başlık: {title}\n\nAçıklama:\n{description[:3000]}"
    prompt = ("YouTube video metnindeki sponsor/reklam/marka isimlerini çıkar.\n\n"
              f"{text}\n\nSADECE JSON: {{\"markalar\": [\"...\"]}}")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 400,
                              "responseMimeType": "application/json"}
    }
    data, err = gemini_call(api_key, payload)
    if err:
        return []
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return parse_json_safe(text).get("markalar", []) or []
    except Exception:
        return []


# ─── Kanal işlemleri ──────────────────────────────────────────────────────────

def channel_id_from_url(url):
    """URL'den kanal ID/handle çıkar"""
    url = url.strip().rstrip("/")
    # @handle
    m = re.search(r"@([a-zA-Z0-9_\-\.]+)", url)
    if m:
        return f"@{m.group(1)}"
    m = re.search(r"channel/([a-zA-Z0-9_\-]+)", url)
    if m:
        return m.group(1)
    return url.split("/")[-1]


def fetch_channel_videos(channel_url, last_hours=24):
    """Kanaldan video listesi çek - /videos, /streams ve ana sayfa dene"""
    base_url = channel_url.rstrip("/")
    for suffix in ("/videos", "/streams", "/shorts", "/featured", "/community"):
        if base_url.endswith(suffix):
            base_url = base_url[:-len(suffix)]
            break

    all_entries = {}  # id -> entry (deduplicate)
    channel_name = ""
    channel_id_meta = ""

    # Denenecek URL'ler — sırayla
    tries = [
        (base_url + "/videos", "videos"),
        (base_url + "/streams", "streams"),
        (base_url, "main"),  # son çare: kanal ana sayfası
    ]

    for try_url, tab in tries:
        try:
            print(f"[KANAL] Çekiliyor: {try_url}")
            with YoutubeDL(get_ydl_opts({
                "extract_flat": "in_playlist",
                "skip_download": True,
                "playlistend": 50,
                "ignoreerrors": True,
                "socket_timeout": 30,
            })) as ydl:
                info = ydl.extract_info(try_url, download=False)
            if not info:
                print(f"[KANAL] {try_url} → boş")
                continue

            if not channel_name:
                channel_name = (info.get("channel") or info.get("uploader") or
                                info.get("title", "").split(" - ")[0])
            if not channel_id_meta:
                channel_id_meta = info.get("channel_id") or info.get("uploader_id", "")

            entries = info.get("entries", []) or []
            # Nested entries (kanal ana sayfası → tabs → entries)
            flat_entries = []
            for e in entries:
                if not e:
                    continue
                # Eğer kendisi de entries içeriyorsa (tab yapısı)
                if isinstance(e, dict) and e.get("entries"):
                    for sub in e.get("entries", []):
                        if sub:
                            flat_entries.append(sub)
                else:
                    flat_entries.append(e)

            count = 0
            for entry in flat_entries:
                if not entry:
                    continue
                eid = entry.get("id")
                if not eid or eid in all_entries:
                    continue
                # Video olmayanları (post, channel, vs) atla
                if entry.get("_type") not in (None, "url", "video"):
                    continue
                all_entries[eid] = {
                    "id": eid,
                    "url": entry.get("url") or f"https://www.youtube.com/watch?v={eid}",
                    "title": entry.get("title", "") or "Başlıksız",
                    "duration": entry.get("duration", 0) or 0,
                    "thumbnail": entry.get("thumbnail") or
                                 f"https://i.ytimg.com/vi/{eid}/hqdefault.jpg",
                    "view_count": entry.get("view_count", 0) or 0,
                    "tab": tab,
                }
                count += 1
            print(f"[KANAL] {try_url} → {count} yeni video (toplam: {len(all_entries)})")

            # Eğer 5+ video bulunduysa yeterli, devam etme
            if len(all_entries) >= 15 and tab == "videos":
                # Videoları aldık, streams'i de bir dene ama gerekmiyorsa
                continue
        except Exception as e:
            print(f"[KANAL] {try_url} → HATA: {e}")
            continue

    videos_list = list(all_entries.values())
    print(f"[KANAL] SONUÇ: {channel_name} - {len(videos_list)} video")

    return {
        "channel_name": channel_name or channel_id_from_url(channel_url),
        "channel_id": channel_id_meta,
        "videos": videos_list,
    }


# ─── Job Sistemi ──────────────────────────────────────────────────────────────

class JobManager:
    """Tüm analiz işlerini kuyrukta yönetir, sırayla çalıştırır (API tasarruf)"""

    def __init__(self):
        self.queue = []
        self.current = None
        self.lock = threading.Lock()
        self.running = False
        self.worker_thread = None
        self.status = "idle"
        self.last_message = ""
        self.cancel_flag = threading.Event()
        # Şu an analiz edilen videonun canlı state'i
        self.live_video = None  # { video_id, title, ... , detections: [...] }
        self.live_lock = threading.Lock()

    def get_live_video(self):
        with self.live_lock:
            if self.live_video is None:
                return None
            # detections listesini kopyala (race condition'sız)
            return {
                **self.live_video,
                "detections": list(self.live_video.get("detections", []))
            }

    def set_live_video(self, **kwargs):
        with self.live_lock:
            if self.live_video is None:
                self.live_video = {}
            self.live_video.update(kwargs)

    def add_live_detection(self, detection):
        with self.live_lock:
            if self.live_video is None:
                self.live_video = {"detections": []}
            self.live_video.setdefault("detections", []).append(detection)

    def clear_live_video(self):
        with self.live_lock:
            self.live_video = None

    def add_video(self, video_url, channel_id=None, channel_name=None, priority=False):
        with self.lock:
            job = {
                "type": "video",
                "url": video_url,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "id": str(uuid.uuid4())[:8],
                "queued_at": datetime.utcnow().isoformat(),
            }
            if priority:
                self.queue.insert(0, job)
            else:
                self.queue.append(job)
        self._ensure_worker()
        return job["id"]

    def add_channel_scan(self, channel_url, last_hours=24):
        """Kanaldaki son N saatteki videoları sıraya at"""
        with self.lock:
            job = {
                "type": "channel_scan",
                "url": channel_url,
                "last_hours": last_hours,
                "id": str(uuid.uuid4())[:8],
            }
            self.queue.append(job)
        self._ensure_worker()
        return job["id"]

    def cancel_all(self):
        with self.lock:
            self.queue.clear()
        self.cancel_flag.set()

    def queue_status(self):
        with self.lock:
            return {
                "queue_length": len(self.queue),
                "current": self.current,
                "running": self.running,
                "status": self.status,
                "message": self.last_message,
            }

    def _ensure_worker(self):
        if not self.running:
            self.running = True
            self.cancel_flag.clear()
            self.worker_thread = threading.Thread(target=self._worker, daemon=True)
            self.worker_thread.start()

    def _worker(self):
        while True:
            with self.lock:
                if not self.queue:
                    self.running = False
                    self.current = None
                    self.status = "idle"
                    self.last_message = "Boş"
                    return
                job = self.queue.pop(0)
                self.current = job

            try:
                cfg = load_config()
                api_key = cfg.get("gemini_api_key", "")
                if not api_key:
                    self.status = "error"
                    self.last_message = "API key yok"
                    continue

                if job["type"] == "video":
                    self._process_video(job, api_key)
                elif job["type"] == "channel_scan":
                    self._process_channel_scan(job, api_key)
            except Exception as e:
                self.status = "error"
                self.last_message = f"Hata: {e}"
                print(f"[JOB-ERROR] {e}")
                import traceback
                traceback.print_exc()

            if self.cancel_flag.is_set():
                self.cancel_flag.clear()
                with self.lock:
                    self.queue.clear()

    def _process_channel_scan(self, job, api_key):
        """Kanalı tarayıp videolarını sıraya ekler"""
        self.status = "scanning_channel"
        self.last_message = f"Kanal taranıyor: {job['url']}"
        try:
            res = fetch_channel_videos(job["url"], last_hours=job.get("last_hours", 24))
        except Exception as e:
            self.last_message = f"Kanal tarama hatası: {e}"
            return

        channel_id = channel_id_from_url(job["url"])
        channel_name = res["channel_name"] or channel_id

        # Kanal kaydet
        def upd(data):
            if channel_id not in data["channels"]:
                data["channels"][channel_id] = {
                    "id": channel_id, "name": channel_name,
                    "url": job["url"],
                    "channel_logos": [],
                    "videos": {}, "last_scanned": datetime.utcnow().isoformat()
                }
            else:
                data["channels"][channel_id]["name"] = channel_name
                data["channels"][channel_id]["last_scanned"] = datetime.utcnow().isoformat()
            return data
        update_data(upd)

        # Videoları kuyruğa ekle (zaten analiz edilmemiş olanları)
        existing = get_data()["channels"].get(channel_id, {}).get("videos", {})
        added = 0
        with self.lock:
            for v in res["videos"]:
                if v["id"] in existing and existing[v["id"]].get("completed"):
                    continue  # zaten yapıldı
                self.queue.append({
                    "type": "video",
                    "url": v["url"],
                    "video_id": v["id"],
                    "video_title": v["title"],
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "id": str(uuid.uuid4())[:8],
                })
                added += 1
        self.last_message = f"{channel_name}: {added} yeni video sıraya alındı"

    def _process_video(self, job, api_key):
        """Bir videoyu analiz et"""
        self.status = "analyzing"
        self.last_message = f"Video açılıyor: {job.get('video_title','')[:40]}"

        # 1. Meta
        try:
            with YoutubeDL(get_ydl_opts({"skip_download": True, "noplaylist": True})) as ydl:
                info = ydl.extract_info(job["url"], download=False)
        except Exception as e:
            self.last_message = f"Meta hatası: {e}"
            return

        video_id = info.get("id") or job.get("video_id")
        title = info.get("title", "")
        duration = info.get("duration", 0) or 0
        description = info.get("description", "") or ""
        thumb_url = info.get("thumbnail", "")
        channel_id = job.get("channel_id") or channel_id_from_url(
            info.get("channel_url", "") or info.get("webpage_url", "")
        )
        channel_name = job.get("channel_name") or info.get("channel", "")

        # Kanal yoksa ekle
        def upd_ch(data):
            if channel_id not in data["channels"]:
                data["channels"][channel_id] = {
                    "id": channel_id, "name": channel_name, "url": job["url"],
                    "channel_logos": [], "videos": {},
                    "last_scanned": datetime.utcnow().isoformat()
                }
            return data
        update_data(upd_ch)

        # Mevcut kanal logoları
        channel_data = get_data()["channels"].get(channel_id, {})
        channel_logos = channel_data.get("channel_logos", [])

        # ── LIVE: video state başlat ──
        self.clear_live_video()
        self.set_live_video(
            video_id=video_id,
            title=title,
            url=job["url"],
            duration=duration,
            thumbnail=thumb_url,
            channel_id=channel_id,
            channel_name=channel_name,
            status="preparing",
            progress=0,
            detections=[],
            api_calls=0,
            total_frames=0,
            current_frame=0,
            total_steps=0,
            message="Açıklama analiz ediliyor..."
        )

        # 2. Açıklamadan markalar
        self.last_message = f"Açıklama analiz: {title[:40]}"
        desc_brands = gemini_extract_brands(api_key, title, description)
        self.set_live_video(desc_brands=desc_brands, channel_logos=channel_logos,
                            message="Stream URL alınıyor...")

        # 3. Stream URL
        self.last_message = f"Stream alınıyor: {title[:40]}"
        try:
            with YoutubeDL(get_ydl_opts({"skip_download": True, "noplaylist": True, "format": "best[ext=mp4][height<=720]/best[height<=720]/best"})) as ydl:
                si = ydl.extract_info(job["url"], download=False)
            stream_url = si.get("url")
            if not stream_url:
                for f in reversed(si.get("formats", [])):
                    if f.get("url") and f.get("vcodec") not in (None, "none"):
                        stream_url = f["url"]; break
        except Exception as e:
            self.last_message = f"Stream hatası: {e}"
            return

        if not stream_url:
            self.last_message = "Stream URL yok"
            return

        # 4. Frame analizi
        cap = cv2.VideoCapture(stream_url)
        if not cap.isOpened():
            self.last_message = "Video açılamadı"
            return

        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        total_f = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        vid_duration = total_f / fps if fps > 0 else duration or 60
        interval = 8
        frame_step = max(1, int(fps * interval))
        total_steps = max(1, int(vid_duration / interval))

        # Frame klasörü
        job_frames_dir = FRAMES_DIR / video_id
        job_frames_dir.mkdir(exist_ok=True)

        current_frame = 0
        analyzed = 0
        api_calls = 0
        detections = []

        # Akıllı tekrar tespit sistemi
        ad_appearances = {}  # {(marka_normalized, tur): [appearance_indices]}
        last_frame = None
        last_result = None

        def frame_diff(f1, f2):
            try:
                s1 = cv2.resize(cv2.cvtColor(f1, cv2.COLOR_BGR2GRAY), (64, 36))
                s2 = cv2.resize(cv2.cvtColor(f2, cv2.COLOR_BGR2GRAY), (64, 36))
                return float(np.mean(cv2.absdiff(s1, s2))) / 255.0
            except Exception:
                return 1.0

        def lower_diff(f1, f2):
            try:
                h = f1.shape[0]
                r1 = cv2.resize(cv2.cvtColor(f1[int(h*0.78):, :], cv2.COLOR_BGR2GRAY), (128, 32))
                r2 = cv2.resize(cv2.cvtColor(f2[int(h*0.78):, :], cv2.COLOR_BGR2GRAY), (128, 32))
                return float(np.mean(cv2.absdiff(r1, r2))) / 255.0
            except Exception:
                return 1.0

        def fmt_ts(s):
            t = int(s); h, r = divmod(t, 3600); m, ss = divmod(r, 60)
            return f"{h:02d}:{m:02d}:{ss:02d}" if h else f"{m:02d}:{ss:02d}"

        def normalize_brand(b):
            if not b: return ""
            return re.sub(r"[^a-z0-9]", "", b.lower())

        while True:
            if self.cancel_flag.is_set():
                cap.release()
                return

            cap.set(cv2.CAP_PROP_POS_FRAMES, current_frame)
            ret, frame = cap.read()
            if not ret:
                break

            ts = current_frame / fps if fps > 0 else analyzed * interval
            ts_str = fmt_ts(ts)

            frame_filename = f"frame_{analyzed:04d}_{int(ts)}s.jpg"
            cv2.imwrite(str(job_frames_dir / frame_filename), frame,
                         [cv2.IMWRITE_JPEG_QUALITY, 78])

            skip_api = False
            if last_frame is not None and last_result is not None:
                if frame_diff(last_frame, frame) < 0.03 and lower_diff(last_frame, frame) < 0.04:
                    skip_api = True

            if skip_api:
                result = {
                    "reklam_var": last_result.get("reklam_var", False),
                    "guven": last_result.get("guven", "Orta"),
                    "markalar": last_result.get("markalar", []),
                    "tespitler": last_result.get("tespitler", []),
                    "ozet": "[Önceki frame ile aynı]"
                }
            else:
                fh, fw = frame.shape[:2]
                if fw > 720:
                    sc = 720 / fw
                    fs = cv2.resize(frame, (720, int(fh * sc)))
                else:
                    fs = frame
                _, buf = cv2.imencode(".jpg", fs, [cv2.IMWRITE_JPEG_QUALITY, 88])
                b64 = base64.b64encode(buf.tobytes()).decode()
                result = gemini_analyze_frame(
                    api_key, b64, channel_logos, desc_brands, ts_str
                )
                api_calls += 1
                last_result = result
                time.sleep(2)

            last_frame = frame

            # ── Akıllı tekrar süzme ──
            # Aynı (marka + tur) 2 defadan fazla görünmüşse ekleme
            filtered_tespitler = []
            for t in result.get("tespitler", []):
                marka = t.get("marka", "") or ""
                tur = t.get("tur", "") or ""
                norm = normalize_brand(marka) + "|" + tur
                appearances = ad_appearances.get(norm, [])
                if len(appearances) < 2:
                    filtered_tespitler.append(t)
                    ad_appearances.setdefault(norm, []).append(analyzed)

            detection = {
                "index": analyzed, "timestamp": ts_str, "seconds": round(ts, 1),
                "frame_url": f"/frames/{video_id}/{frame_filename}",
                "reklam_var": result.get("reklam_var", False) and len(filtered_tespitler) > 0,
                "guven": result.get("guven", "Düşük"),
                "markalar": result.get("markalar", []),
                "tespitler": filtered_tespitler,
                "ozet": result.get("ozet", ""),
                "_all_tespitler_count": len(result.get("tespitler", [])),
                "_filtered_count": len(filtered_tespitler),
                "_api_used": not skip_api,
            }
            detections.append(detection)

            # ── LIVE: detection'ı canlı state'e ekle ──
            self.add_live_detection(detection)

            analyzed += 1
            current_frame += frame_step
            self.last_message = (
                f"{title[:35]} · Frame {analyzed}/{total_steps} · "
                f"API: {api_calls} · {ts_str}"
            )

            # LIVE state güncelle
            self.set_live_video(
                status="analyzing",
                progress=round(min(95, (analyzed / total_steps) * 95), 1),
                api_calls=api_calls,
                total_frames=analyzed,
                current_frame=analyzed,
                total_steps=total_steps,
                message=f"Frame {analyzed}/{total_steps} · {ts_str}"
            )

        cap.release()

        # ── Kanal logosu öğrenme ──
        # Bu videoda 3'ten fazla frame'de görünen ama "köşe logo" tipinde olan markalar
        # → kanal logosu olabilir
        appearance_counts = {}
        for d in detections:
            for t in d.get("tespitler", []) + [tt for tt in (last_result.get("tespitler",[]) if last_result else [])]:
                marka = t.get("marka", "").strip()
                if not marka: continue
                tur = (t.get("tur","") or "").lower()
                konum = (t.get("konum","") or "").lower()
                # Sadece köşe logoları kanal logosu olarak öğren
                if "köşe" in tur or "logo" in tur or "üst" in konum or "alt" in konum:
                    appearance_counts[marka] = appearance_counts.get(marka, 0) + 1

        # Frame'lerin %30'undan fazlasında görünen markalar = kanal logosu
        new_logos = []
        threshold = max(3, analyzed * 0.30)
        for marka, count in appearance_counts.items():
            if count >= threshold and marka not in channel_logos:
                new_logos.append(marka)

        # Veriyi kaydet
        def upd_video(data):
            ch = data["channels"].setdefault(channel_id, {
                "id": channel_id, "name": channel_name, "url": job["url"],
                "channel_logos": [], "videos": {},
                "last_scanned": datetime.utcnow().isoformat()
            })
            if new_logos:
                ch["channel_logos"] = list(dict.fromkeys(ch.get("channel_logos", []) + new_logos))

            # Özet hesapla
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

            ch["videos"][video_id] = {
                "id": video_id, "title": title, "url": job["url"],
                "duration": duration, "thumbnail": thumb_url,
                "analyzed_at": datetime.utcnow().isoformat(),
                "total_frames": analyzed,
                "api_calls": api_calls,
                "ad_frame_count": len(ad_detections),
                "type_counts": type_counts,
                "brand_counts": brand_counts,
                "detections": detections,
                "completed": True,
                "desc_brands": desc_brands,
            }
            return data

        update_data(upd_video)
        self.last_message = f"✓ Tamamlandı: {title[:35]} · {len([d for d in detections if d['reklam_var']])} reklam"
        self.set_live_video(
            status="completed", progress=100,
            message=f"Tamamlandı — {len([d for d in detections if d['reklam_var']])} reklam tespit"
        )


JOB_MANAGER = JobManager()


# ─── Flask App ────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:p>")
def static_files(p):
    return send_from_directory(STATIC_DIR, p)


@app.route("/frames/<video_id>/<filename>")
def frame_files(video_id, filename):
    return send_from_directory(FRAMES_DIR / video_id, filename)


@app.route("/api/config", methods=["GET", "POST"])
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


@app.route("/api/channels", methods=["GET"])
def list_channels():
    """Kanallar listesi + özet istatistikler"""
    cfg = load_config()
    data = get_data()
    out = []
    for url in cfg.get("channels", []):
        ch_id = channel_id_from_url(url)
        ch = data["channels"].get(ch_id, {})
        videos = ch.get("videos", {})
        completed = [v for v in videos.values() if v.get("completed")]
        total_ads = sum(v.get("ad_frame_count", 0) for v in completed)

        # Top markalar
        brand_totals = {}
        for v in completed:
            for marka, count in (v.get("brand_counts", {}) or {}).items():
                brand_totals[marka] = brand_totals.get(marka, 0) + count
        top_brands = sorted(brand_totals.items(), key=lambda x: -x[1])[:5]

        out.append({
            "id": ch_id,
            "url": url,
            "name": ch.get("name", ch_id),
            "video_count": len(completed),
            "total_ads": total_ads,
            "channel_logos": ch.get("channel_logos", []),
            "top_brands": [{"name": b, "count": c} for b, c in top_brands],
            "last_scanned": ch.get("last_scanned"),
        })
    return jsonify({"channels": out})


@app.route("/api/channel/<path:ch_id>/browse")
@app.route("/api/channel-browse")
def channel_browse(ch_id=None):
    """Kanaldaki tüm videoları listele (YouTube'dan canlı çek), analiz durumlarıyla."""
    # Query param desteği (URL kaçışı sorununu önler)
    if ch_id is None:
        ch_id = request.args.get("id", "")
    if not ch_id:
        return jsonify({"error": "ch_id gerekli"}), 400

    # URL'yi config'ten bul
    cfg = load_config()
    channel_url = None
    for url in cfg.get("channels", []):
        if channel_id_from_url(url) == ch_id:
            channel_url = url
            break

    if not channel_url:
        # data'dan bul
        data = get_data()
        ch = data["channels"].get(ch_id)
        if ch:
            channel_url = ch.get("url")

    if not channel_url:
        return jsonify({"error": "Kanal bulunamadı"}), 404

    try:
        res = fetch_channel_videos(channel_url)
    except Exception as e:
        return jsonify({"error": f"Kanal taranamadı: {e}"}), 500

    # Analiz durumlarını ekle
    data = get_data()
    analyzed_ids = set(data["channels"].get(ch_id, {}).get("videos", {}).keys())

    # Şu an sırada/işleniyor mu kontrol
    queue_state = JOB_MANAGER.queue_status()
    queued_urls = set()
    current_url = None
    with JOB_MANAGER.lock:
        for j in JOB_MANAGER.queue:
            if j.get("type") == "video":
                queued_urls.add(j.get("url", ""))
        if JOB_MANAGER.current and JOB_MANAGER.current.get("type") == "video":
            current_url = JOB_MANAGER.current.get("url", "")

    videos = []
    for v in res["videos"]:
        status = "not_analyzed"
        if v["id"] in analyzed_ids:
            status = "analyzed"
        elif v["url"] == current_url:
            status = "processing"
        elif v["url"] in queued_urls:
            status = "queued"
        videos.append({**v, "status": status})

    return jsonify({
        "channel_name": res["channel_name"],
        "channel_id": ch_id,
        "channel_url": channel_url,
        "videos": videos,
    })


@app.route("/api/channel/<path:ch_id>")
@app.route("/api/channel-info")
def channel_detail(ch_id=None):
    """Bir kanalın detay sayfası"""
    if ch_id is None:
        ch_id = request.args.get("id", "")
    if not ch_id:
        return jsonify({"error": "ch_id gerekli"}), 400

    data = get_data()
    ch = data["channels"].get(ch_id)
    if not ch:
        # Config'te varsa boş veriyle döndür
        cfg = load_config()
        for url in cfg.get("channels", []):
            if channel_id_from_url(url) == ch_id:
                return jsonify({
                    "channel": {
                        "id": ch_id, "name": ch_id, "url": url,
                        "channel_logos": [], "last_scanned": None,
                    },
                    "videos": [], "brand_totals": [], "type_totals": [],
                })
        return jsonify({"error": "Kanal bulunamadı"}), 404

    videos = list(ch.get("videos", {}).values())
    videos.sort(key=lambda v: v.get("analyzed_at", ""), reverse=True)

    # Aggregate stats
    brand_totals = {}
    type_totals = {}
    for v in videos:
        for marka, count in (v.get("brand_counts", {}) or {}).items():
            brand_totals[marka] = brand_totals.get(marka, 0) + count
        for tur, count in (v.get("type_counts", {}) or {}).items():
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
            "id": v.get("id"),
            "title": v.get("title"),
            "thumbnail": v.get("thumbnail"),
            "duration": v.get("duration"),
            "analyzed_at": v.get("analyzed_at"),
            "ad_frame_count": v.get("ad_frame_count", 0),
            "total_frames": v.get("total_frames", 0),
            "type_counts": v.get("type_counts", {}),
            "top_brands": sorted((v.get("brand_counts", {}) or {}).items(),
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


@app.route("/api/video/<video_id>")
def video_detail(video_id):
    data = get_data()
    for ch_id, ch in data["channels"].items():
        if video_id in ch.get("videos", {}):
            v = ch["videos"][video_id]
            return jsonify({
                "video": v,
                "channel": {"id": ch_id, "name": ch.get("name", "")},
            })
    return jsonify({"error": "Video bulunamadı"}), 404


@app.route("/api/scan/channel", methods=["POST"])
def scan_channel():
    """Tek kanalı tara"""
    data = request.get_json()
    url = data.get("url", "").strip()
    hours = int(data.get("hours", 24))
    if not url:
        return jsonify({"error": "URL gerekli"}), 400
    job_id = JOB_MANAGER.add_channel_scan(url, last_hours=hours)
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/scan/all", methods=["POST"])
def scan_all():
    """Konfigürasyondaki tüm kanalları tara"""
    data = request.get_json() or {}
    hours = int(data.get("hours", 24))
    cfg = load_config()
    channels = cfg.get("channels", [])
    if not channels:
        return jsonify({"error": "Kanal listesi boş"}), 400
    job_ids = []
    for url in channels:
        job_ids.append(JOB_MANAGER.add_channel_scan(url, last_hours=hours))
    return jsonify({"ok": True, "job_ids": job_ids, "count": len(channels)})


@app.route("/api/analyze/video", methods=["POST"])
def analyze_single_video():
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL gerekli"}), 400
    job_id = JOB_MANAGER.add_video(url, priority=True)
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/live-video")
def live_video():
    """Şu an analiz edilen videonun canlı state'i"""
    live = JOB_MANAGER.get_live_video()
    if live is None:
        return jsonify({"active": False})
    return jsonify({"active": True, **live})


@app.route("/api/queue")
def queue_status():
    return jsonify(JOB_MANAGER.queue_status())


@app.route("/api/cancel-queue", methods=["POST"])
def cancel_queue():
    JOB_MANAGER.cancel_all()
    return jsonify({"ok": True})


def open_browser():
    import webbrowser
    time.sleep(1.0)
    webbrowser.open("http://127.0.0.1:5001")


if __name__ == "__main__":
    print("\n" + "=" * 58)
    print("  YouTube Reklam Tespit v3")
    print("  Tarayıcı: http://127.0.0.1:5001")
    print("=" * 58 + "\n")
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), debug=False, threaded=True)
