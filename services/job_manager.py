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
        _stamp_finished(kwargs)
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


def _stamp_finished(kwargs):
    """Analiz bitti/hata verdi anını kaydet — /api/live-video hoşgörü penceresi bunu okur."""
    if kwargs.get("status") in ("completed", "error") and "finished_at" not in kwargs:
        kwargs["finished_at"] = datetime.utcnow().isoformat()


def _job_label(kind, title=None, channel_name=None, url=""):
    """Kuyruk/sidebar için insan-okur etiket (Redis ve thread modunda ortak)."""
    if kind == "channel_scan":
        return "📡 Kanal taraması: " + ((url or "").split("@")[-1] or "?")
    if kind == "verify":
        return "🔍 " + (title or "2. model doğrulaması")
    t = (title or "").strip()
    if not t:
        u = url or ""
        t = u.split("v=")[-1].split("&")[0] if "v=" in u else u.rsplit("/", 1)[-1]
    ch = (channel_name or "").strip()
    return "🎬 " + (f"{ch} · {t}" if ch else t)


def _merge_live_state(running_item, live, kind, channel_name, url):
    """Canlı analiz state'ini (adım/ilerleme/başlık) running_item'a işle."""
    message = ""
    if live and live.get("status") not in (None, "completed", "error"):
        running_item.update({
            "video_id": live.get("video_id") or "",
            "step": live.get("status") or "",
            "progress": live.get("progress") or 0,
        })
        if live.get("title"):
            running_item["title"] = live["title"]
            running_item["label"] = _job_label(
                kind, live["title"], channel_name or live.get("channel_name"), url)
        message = live.get("message") or ""
    return running_item, message


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
            _stamp_finished(kwargs)
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

    def add_video(self, video_url, channel_id=None, channel_name=None, priority=False, title=""):
        if priority:
            _clear_pause()   # manuel analiz → molayı kaldır
        if USE_REDIS:
            from services.tasks import process_video_rq
            # Worker imzası değişmez; başlık/kanal bilgisi meta ile taşınır (sidebar için)
            job = _rq.enqueue(
                process_video_rq, video_url, channel_id, channel_name,
                job_timeout=3600, at_front=priority,
                description=(title or video_url)[:120],
                meta={"kind": "video", "title": title or "",
                      "channel_name": channel_name or "", "url": video_url},
            )
            return job.id
        with self._lock:
            job = {
                "type": "video",
                "url": video_url,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "title": title or "",
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
                description=("Kanal taraması: " + channel_url)[:120],
                meta={"kind": "channel_scan", "url": channel_url},
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

    def add_verify(self, video_id):
        """2. model doğrulama işi (analiz sonrası, düşük öncelik)."""
        if USE_REDIS:
            from services.tasks import process_verify_rq
            job = _rq.enqueue(
                process_verify_rq, video_id,
                job_timeout=1800,
                description=f"Doğrulama: {video_id}",
                meta={"kind": "verify", "url": "", "title": f"Doğrulama: {video_id}"},
            )
            return job.id
        with self._lock:
            job = {"type": "verify", "video_id": video_id,
                   "id": str(uuid.uuid4())[:8]}
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
            return self._queue_status_redis()
        return self._queue_status_thread()

    def _queue_status_redis(self):
        from rq.job import Job
        from rq.registry import StartedJobRegistry
        try:
            started_ids = StartedJobRegistry(queue=_rq).get_job_ids()
            queued_ids = _rq.job_ids
        except Exception:
            started_ids, queued_ids = [], []

        # Ölü worker'ın job'u registry'de job_timeout (1-2 saat) boyunca kalabiliyor;
        # heartbeat'i ~2 dk'dan eski job'ları çalışıyor sayma.
        running_job = None
        for jid in started_ids:
            try:
                j = Job.fetch(jid, connection=_redis)
            except Exception:
                continue
            hb = getattr(j, "last_heartbeat", None)
            if hb is not None:
                try:
                    if (datetime.utcnow() - hb).total_seconds() > 120:
                        continue
                except TypeError:
                    pass
            running_job = j
            break

        running_item, message = None, ""
        if running_job is not None:
            meta = running_job.meta or {}
            kind = meta.get("kind") or (
                "channel_scan" if "channel_scan" in (running_job.func_name or "") else "video")
            url = meta.get("url") or (running_job.args[0] if running_job.args else "")
            title, channel_name = meta.get("title") or "", meta.get("channel_name") or ""
            running_item = {
                "label": _job_label(kind, title, channel_name, url),
                "title": title, "channel_name": channel_name,
                "url": url, "type": kind,
            }
            # Tek worker → reklam:live anahtarı çalışan işin anlık durumudur
            live = _get_redis_live().get() or {}
            running_item, message = _merge_live_state(running_item, live, kind, channel_name, url)

        queued_items = []
        if queued_ids:
            try:
                jobs = Job.fetch_many(queued_ids[:6], connection=_redis)
            except Exception:
                jobs = []
            for j in jobs:
                if j is None:
                    continue
                meta = j.meta or {}
                kind = meta.get("kind") or (
                    "channel_scan" if "channel_scan" in (j.func_name or "") else "video")
                url = meta.get("url") or (j.args[0] if j.args else "")
                queued_items.append({
                    "label": _job_label(kind, meta.get("title"), meta.get("channel_name"), url),
                    "type": kind,
                    "position": len(queued_items) + 1,
                })

        running = running_job is not None
        return {
            "queue_length": len(queued_ids),
            "current": started_ids[0] if started_ids else None,
            "running": running,
            "status": "running" if running else ("queued" if queued_ids else "idle"),
            "message": message,
            "running_item": running_item,
            "queued_items": queued_items,
        }

    def _queue_status_thread(self):
        with self._lock:
            current = dict(self._current) if self._current else None
            queue_snapshot = [dict(j) for j in self._queue[:6]]
            queue_length = len(self._queue)
            running = self._running and current is not None
            status = self._status
            message = self._message

        running_item = None
        if running and current:
            kind = current.get("type", "video")
            url = current.get("url", "")
            title, channel_name = current.get("title") or "", current.get("channel_name") or ""
            running_item = {
                "label": _job_label(kind, title, channel_name, url),
                "title": title, "channel_name": channel_name,
                "url": url, "type": kind,
            }
            live = self.get_live_video() or {}
            running_item, live_msg = _merge_live_state(running_item, live, kind, channel_name, url)
            if live_msg:
                message = live_msg

        queued_items = [{
            "label": _job_label(j.get("type", "video"), j.get("title"),
                                j.get("channel_name"), j.get("url", "")),
            "type": j.get("type", "video"),
            "position": i + 1,
        } for i, j in enumerate(queue_snapshot)]

        return {
            "queue_length": queue_length,
            "current": current.get("id") if current else None,
            "running": running,
            "status": status,
            "message": message,
            "running_item": running_item,
            "queued_items": queued_items,
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
                # API key YALNIZ video analizi için gerekir. Kanal taraması
                # Gemini kullanmaz ama bu kontrole takılıp iş SESSİZCE
                # kuyruktan düşüyordu (pop edilmiş, sonra continue).
                if job.get("type") == "video" and not api_key:
                    self._status = "error"
                    self._message = "API key yok"
                    continue

                if job["type"] == "video":
                    from services.tasks import process_video_sync
                    process_video_sync(job, api_key, self)
                elif job["type"] == "channel_scan":
                    from services.tasks import process_channel_scan_sync
                    process_channel_scan_sync(job, api_key, self)
                elif job["type"] == "verify":
                    from services.tasks import process_verify_sync
                    process_verify_sync(job, api_key, self)
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
