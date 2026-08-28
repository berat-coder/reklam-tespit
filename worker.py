"""
RQ worker başlatıcı — OFİS İŞÇİSİ.

YouTube, Railway'in datacenter IP'sinden video formatı vermiyor (tüm player
client'lar sıfır format döner). Bu yüzden yt-dlp + ffmpeg işi normal bir
internet bağlantısındaki makinede (ofis sunucusu) çalışır. Panel, veritabanı ve
kanıt kareleri Railway'de kalır; işçi kapalıyken de panel çalışır, yalnız yeni
işler kuyrukta bekler.

Ofis makinesinde gerekli ortam değişkenleri (.env dosyasına yazılabilir):
    REDIS_URL         Railway Redis PUBLIC adresi    (iş kuyruğu)
    DATABASE_URL      Railway Postgres PUBLIC adresi (sonuçlar)
    GEMINI_API_KEY    Gemini anahtarı
    FRAME_UPLOAD_URL  https://pitch.onstream.live    (kanıt kareleri)
    WORKER_TOKEN      web ile paylaşılan gizli anahtar
    YT_USE_COOKIES=0  (cookie gerekmiyor; bayat cookie zarar veriyor)

Çalıştırma:  ./.venv/bin/python worker.py
"""

import os
import platform
import socket
import threading

import requests
from dotenv import load_dotenv

load_dotenv()

# REDIS_URL YOKSA localhost'a düşmek TEHLİKELİ: Railway'de böyle bir Redis yok,
# süreç boot'ta "Error 111 connecting to localhost:6379" ile çöküyor, platform
# yeniden başlatıyor ve sonsuz ÇÖKME DÖNGÜSÜ oluşuyor (6 saatte 11+ restart
# gözlendi). Loglar traceback'le doluyor, gerçek sorun görünmez oluyor.
# Artık: değişken yoksa net mesaj + temiz çıkış (restart döngüsü yok).
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
UPLOAD_URL = os.environ.get("FRAME_UPLOAD_URL", "").strip().rstrip("/")
TOKEN = os.environ.get("WORKER_TOKEN", "").strip()
HEARTBEAT_SECONDS = 30

from redis import Redis
from rq import Worker, SimpleWorker, Queue

if not REDIS_URL:
    print("[WORKER] HATA: REDIS_URL tanımlı değil — işçi kuyruğa bağlanamaz.\n"
          "         Railway'de worker servisine REDIS_URL ekleyin\n"
          "         (Redis servisinin REDIS_URL'ü), yerelde .env'e yazın.")
    raise SystemExit(1)

conn = Redis.from_url(REDIS_URL)


def _heartbeat_loop():
    """Panelin 'işçi çevrimiçi' rozetini besler."""
    host = socket.gethostname()[:60]
    while True:
        try:
            requests.post(
                f"{UPLOAD_URL}/api/worker/heartbeat",
                headers={"X-Worker-Token": TOKEN},
                json={"host": host, "version": "v4"},
                timeout=15,
            )
        except Exception:
            pass          # ağ kesintisi işçiyi durdurmasın
        threading.Event().wait(HEARTBEAT_SECONDS)


if __name__ == "__main__":
    if UPLOAD_URL and TOKEN:
        threading.Thread(target=_heartbeat_loop, daemon=True).start()
        print(f"[WORKER] kalp atışı → {UPLOAD_URL}")
    else:
        print("[WORKER] UYARI: FRAME_UPLOAD_URL/WORKER_TOKEN yok — "
              "kanıt kareleri panele yüklenmeyecek")

    queues = [Queue(connection=conn)]
    # macOS'ta RQ'nun varsayılan (fork'lu) işçisi ÇÖKÜYOR: iş, os.fork ile
    # ayrı bir "work-horse" sürecinde çalışıyor ve fork sonrası OpenCV /
    # Objective-C runtime kullanımı signal 11 (SIGSEGV) veriyor
    # ("Work-horse terminated unexpectedly; waitpid returned 11").
    # SimpleWorker fork ETMEZ, işi aynı süreçte çalıştırır → macOS'ta çalışır.
    # Linux'ta (Railway/ofis sunucusu) varsayılan işçi korunur; WORKER_SIMPLE
    # ile elle zorlanabilir.
    simple = (os.environ.get("WORKER_SIMPLE", "").strip().lower()
              in ("1", "true", "yes", "on")) or platform.system() == "Darwin"
    if simple:
        os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")
        print("[WORKER] SimpleWorker (fork yok — macOS uyumu)")
        worker = SimpleWorker(queues, connection=conn)
    else:
        worker = Worker(queues, connection=conn)
    print(f"[WORKER] Redis: {REDIS_URL.split('@')[-1]}")
    print("[WORKER] iş bekleniyor...")
    worker.work(with_scheduler=True)
