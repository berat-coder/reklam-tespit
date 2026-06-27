"""
REDIS_URL ortam değişkeni varsa Redis+RQ kullanır (Heroku).
Yoksa thread tabanlı kuyruğa düşer (local dev).
"""

import os
import json
import uuid
import threading
from datetime import datetime

REDIS_URL = os.environ.get("REDIS_URL", "")
USE_REDIS = bool(REDIS_URL)

_redis = None
_rq = None
_redis_live = None

if USE_REDIS:
    from redis import Redis
    from rq import Queue as RQQueue
    _redis = Redis.from_url(REDIS_URL)
    _rq = RQQueue(connection=_redis)


# ── Redis canlı state ─────────────────────────────────────────────────────────

class _RedisLiveState:
    """Live video state stored in Redis — paylaşılan worker ↔ web."""

    KEY = "reklam:live"
    TTL = 7200

    def __init__(self, r):
        self._r = r

    def set(self, **kwargs):
        current = self._load() or {}
        current.update(kwargs)
        self._r.setex(self.KEY, self.TTL, json.dumps(current, ensure_ascii=False))

    def add_detection(self, d):
        current = self._load() or {"detections": []}
        current.setdefault("detections", []).append(d)
        self._r.setex(self.KEY, self.TTL, json.dumps(current, ensure_ascii=False))

    def get(self):
        data = self._load()
        if not data:
            return None
        return {**data, "detections": list(data.get("detections", []))}

    def clear(self):
        self._r.delete(self.KEY)

    def _load(self):
        raw = self._r.get(self.KEY)
        return json.loads(raw) if raw else None


def _get_redis_live():
    global _redis_live
    if _redis_live is None:
        _redis_live = _RedisLiveState(_redis)
    return _redis_live


# ── JobManager ────────────────────────────────────────────────────────────────

class JobManager:

    def __init__(self):
        if not USE_REDIS:
            self._queue = []
            self._current = None
            self._lock = threading.Lock()
            self._running = False
            self._cancel_flag = threading.Event()
            self._live_video = None
            self._live_lock = threading.Lock()
            self._status = "idle"
            self._message = "Boş"

    # ── Canlı state ──────────────────────────────────────────────────────────

    def get_live_video(self):
        if USE_REDIS:
            return _get_redis_live().get()
        with self._live_lock:
            if self._live_video is None:
                return None
            return {**self._live_video, "detections": list(self._live_video.get("detections", []))}

    def set_live_video(self, **kwargs):
        if USE_REDIS:
            _get_redis_live().set(**kwargs)
        else:
            with self._live_lock:
                if self._live_video is None:
                    self._live_video = {}
                self._live_video.update(kwargs)

    def add_live_detection(self, detection):
        if USE_REDIS:
            _get_redis_live().add_detection(detection)
        else:
            with self._live_lock:
                if self._live_video is None:
                    self._live_video = {"detections": []}
                self._live_video.setdefault("detections", []).append(detection)

    def clear_live_video(self):
        if USE_REDIS:
            _get_redis_live().clear()
        else:
            with self._live_lock:
                self._live_video = None

    # ── Kuyruk yönetimi ───────────────────────────────────────────────────────

    def add_video(self, video_url, channel_id=None, channel_name=None, priority=False):
        if priority:
            _clear_pause()   # manuel analiz → molayı kaldır
        if USE_REDIS:
            from services.tasks import process_video_rq
            job = _rq.enqueue(
                process_video_rq, video_url, channel_id, channel_name,
                job_timeout=3600, at_front=priority,
            )
            return job.id
        with self._lock:
            job = {
                "type": "video",
                "url": video_url,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "id": str(uuid.uuid4())[:8],
                "queued_at": datetime.utcnow().isoformat(),
            }
            if priority:
                self._queue.insert(0, job)
            else:
                self._queue.append(job)
        self._ensure_worker()
        return job["id"]

    def add_channel_scan(self, channel_url, last_hours=24, content_type="all"):
        _clear_pause()   # manuel kanal taraması → molayı kaldır
        if USE_REDIS:
            from services.tasks import process_channel_scan_rq
            job = _rq.enqueue(
                process_channel_scan_rq, channel_url, last_hours, content_type,
                job_timeout=7200,
            )
            return job.id
        with self._lock:
            job = {
                "type": "channel_scan",
                "url": channel_url,
                "last_hours": last_hours,
                "content_type": content_type,
                "id": str(uuid.uuid4())[:8],
            }
            self._queue.append(job)
        self._ensure_worker()
        return job["id"]

    def cancel_all(self):
        if USE_REDIS:
            _rq.empty()
            try:
                from rq.job import Job
                from rq.registry import StartedJobRegistry
                registry = StartedJobRegistry(queue=_rq)
                for jid in registry.get_job_ids():
                    try:
                        Job.fetch(jid, connection=_redis).cancel()
                    except Exception:
                        pass
            except Exception:
                pass
        else:
            with self._lock:
                self._queue.clear()
            self._cancel_flag.set()
            # Gece zamanlayıcısı da kısa süre (10 dk) yeni iş eklemesin — manuel
            # bir tarama/analiz başlatınca bu mola otomatik kalkar.
            _set_pause(600)

    def queue_status(self):
        if USE_REDIS:
            try:
                from rq.registry import StartedJobRegistry
                started = StartedJobRegistry(queue=_rq).get_job_ids()
                queued = _rq.job_ids
            except Exception:
                started, queued = [], []
            return {
                "queue_length": len(queued),
                "current": started[0] if started else None,
                "running": bool(started),
                "status": "running" if started else ("queued" if queued else "idle"),
                "message": "",
            }
        with self._lock:
            return {
                "queue_length": len(self._queue),
                "current": self._current,
                "running": self._running,
                "status": self._status,
                "message": self._message,
            }

    # ── Thread worker (Redis yoksa) ───────────────────────────────────────────

    def _ensure_worker(self):
        if not self._running:
            self._running = True
            self._cancel_flag.clear()
            threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        while True:
            with self._lock:
                if not self._queue:
                    self._running = False
                    self._current = None
                    self._status = "idle"
                    self._message = "Boş"
                    return
                job = self._queue.pop(0)
                self._current = job

            try:
                cfg = load_config()
                api_key = cfg.get("gemini_api_key", "")
                if not api_key:
                    self._status = "error"
                    self._message = "API key yok"
                    continue

                if job["type"] == "video":
                    from services.tasks import process_video_sync
                    process_video_sync(job, api_key, self)
                elif job["type"] == "channel_scan":
                    from services.tasks import process_channel_scan_sync
                    process_channel_scan_sync(job, api_key, self)
            except Exception as e:
                self._status = "error"
                self._message = f"Hata: {e}"
                import traceback
                traceback.print_exc()

            if self._cancel_flag.is_set():
                self._cancel_flag.clear()
                with self._lock:
                    self._queue.clear()


def load_config():
    from config import load_config as _lc
    return _lc()


# ── Tarama molası (manuel "Tümünü Durdur" sonrası gece zamanlayıcısını sustur) ──

def _set_pause(seconds):
    try:
        import time as _t
        from models.database import kv_set
        kv_set("scan_paused_until", _t.time() + seconds)
    except Exception:
        pass


def _clear_pause():
    try:
        from models.database import kv_set
        kv_set("scan_paused_until", 0)
    except Exception:
        pass


def is_scan_paused():
    try:
        import time as _t
        from models.database import kv_get
        return float(kv_get("scan_paused_until", 0) or 0) > _t.time()
    except Exception:
        return False


JOB_MANAGER = JobManager()
