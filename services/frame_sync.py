"""
Kare senkronizasyonu — ofis işçisi → Railway web.

Ofisteki işçi kareleri kendi diskine yazar; panel ise Railway'deki kalıcı
diskten servis eder. Bu modül, işçi modunda çalışırken üretilen kareleri
web'e yükler ki panelde (yöneticinin gördüğü yerde) kanıt kareleri eksiksiz
görünsün.

Devreye girmesi için iki ortam değişkeni gerekir (yalnız işçide tanımlanır):
    FRAME_UPLOAD_URL = https://pitch.onstream.live
    WORKER_TOKEN     = web ile paylaşılan gizli anahtar
Tanımlı değilse tüm çağrılar sessizce no-op olur → Railway/lokal tek makine
kurulumunda davranış hiç değişmez.

Yükleme arka plan thread'lerinde yapılır; analiz akışını yavaşlatmaz.
"""

import os
import queue
import threading

import requests

_UPLOAD_URL = os.environ.get("FRAME_UPLOAD_URL", "").strip().rstrip("/")
_TOKEN = os.environ.get("WORKER_TOKEN", "").strip()
_THREADS = 3
_TIMEOUT = 30

_q = queue.Queue()
_started = False
_start_lock = threading.Lock()
_fail_logged = False


def enabled():
    return bool(_UPLOAD_URL and _TOKEN)


def _post(video_id, path):
    with open(path, "rb") as f:
        r = requests.post(
            f"{_UPLOAD_URL}/api/worker/frame",
            headers={"X-Worker-Token": _TOKEN},
            data={"video_id": video_id},
            files={"file": (path.name, f, "image/jpeg")},
            timeout=_TIMEOUT,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")


def _loop():
    global _fail_logged
    while True:
        item = _q.get()
        if item is None:
            _q.task_done()
            break
        video_id, path = item
        try:
            try:
                _post(video_id, path)
            except Exception:
                _post(video_id, path)          # tek deneme daha (ağ dalgalanması)
        except Exception as e:
            if not _fail_logged:               # her kare için log basma
                _fail_logged = True
                print(f"[KARE-YÜKLEME] hata: {e}")
        finally:
            _q.task_done()


def _ensure_started():
    global _started
    if _started:
        return
    with _start_lock:
        if _started:
            return
        for _ in range(_THREADS):
            threading.Thread(target=_loop, daemon=True).start()
        _started = True


def queue_frame(video_id, path):
    """Kareyi yükleme kuyruğuna al (işçi modu kapalıysa no-op)."""
    if not enabled() or not video_id or not path:
        return
    try:
        if not os.path.exists(path):
            return
    except Exception:
        return
    _ensure_started()
    _q.put((video_id, path))


def wait(timeout=120):
    """Kuyruktakilerin bitmesini bekle (video sonunda çağrılır)."""
    if not enabled() or not _started:
        return
    done = threading.Event()

    def _joiner():
        _q.join()
        done.set()

    threading.Thread(target=_joiner, daemon=True).start()
    done.wait(timeout)
