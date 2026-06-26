"""
Gece otomatik canlı-yayın taraması zamanlayıcısı.

Web process'inde tek bir daemon thread olarak çalışır (gunicorn --workers 1 →
tek instance, çift tetikleme yok). 60 sn'de bir uyanır, config'i YENİDEN okur
(ayar değişikliği restart'sız etki eder) ve pencere içindeyse `interval_min`
dakikada bir TEK canlı yayını tam analize gönderir.

Mantık (kullanıcı isteği):
  • İlk keşif gece başında 24s geriye bakar; sonraki keşifler son keşiften beri.
  • Her tick'te yalnız BİR canlı yayın analiz edilir → 03:00–09:30 / 15dk ≈ 26/gece.
  • Canlı yayın dedup: `live_seen` tablosu (devam eden yayın tekrar kuyruğa alınmaz).
"""

import time
import threading
from datetime import datetime, timedelta

from config import load_config
from models.database import (
    is_live_seen, mark_live_seen, mark_live_status, next_pending_live,
    count_pending_live, prune_live_seen, is_video_completed,
    kv_get, kv_set, log_event,
)

_STATE_KEY = "auto_scan_state"
_started = False
_lock = threading.Lock()


# ── Zaman yardımcıları ─────────────────────────────────────────────────────────

def _eff_now(tz_offset):
    """Etkin yerel saat = UTC + ofset (TR için +3). Sunucu saat diliminden bağımsız."""
    return datetime.utcnow() + timedelta(hours=int(tz_offset or 0))


def _parse_hhmm(s, default):
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return default


def _in_window(cur_min, start_min, end_min):
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min   # gece yarısını geçen pencere


def _night_key(eff_now, start_min, end_min):
    """Geceyi etiketleyen tarih. Pencere gece yarısını geçiyorsa ve sabah
    kısmındaysak gece bir önceki güne aittir."""
    cur = eff_now.hour * 60 + eff_now.minute
    d = eff_now.date()
    if start_min > end_min and cur < end_min:
        d = d - timedelta(days=1)
    return d.isoformat()


def _epoch_to_eff_iso(epoch, tz):
    return (datetime.utcfromtimestamp(epoch) + timedelta(hours=int(tz or 0))).isoformat()


# ── Keşif + analiz ─────────────────────────────────────────────────────────────

def _discover(cfg, lookback, state):
    """Tüm kanallarda canlı yayınları keşfet, yeni olanları live_seen'e işle."""
    from services.youtube import fetch_live_streams, channel_id_from_url
    new_count = 0
    for url in cfg.get("channels", []):
        try:
            res = fetch_live_streams(url, last_hours=lookback)
        except Exception as e:
            print(f"[OTO-TARAMA] keşif hata ({url}): {e}")
            continue
        cid = channel_id_from_url(url)
        for v in res.get("videos", []):
            vid = v.get("id")
            if not vid or is_live_seen(vid) or is_video_completed(vid):
                continue
            mark_live_seen(vid, channel_id=cid, title=v.get("title", ""),
                           url=v.get("url", ""), analyzed=False)
            new_count += 1
    if new_count:
        state["tonight_found"] = state.get("tonight_found", 0) + new_count
        print(f"[OTO-TARAMA] {new_count} yeni canlı yayın keşfedildi")
    return new_count


def _analyze_one(asc, state):
    """Bekleyen havuzdan EN SON keşfedilen bir yayını tam analize gönder."""
    cap = int(asc.get("nightly_cap", 30))
    if state.get("tonight_analyzed", 0) >= cap:
        print(f"[OTO-TARAMA] gecelik üst sınıra ({cap}) ulaşıldı — analiz atlandı")
        return None
    while True:
        row = next_pending_live()
        if not row:
            return None
        vid = row["video_id"]
        if is_video_completed(vid):
            mark_live_status(vid, "done")        # zaten bitmiş/analiz edilmiş → atla
            continue
        from services.job_manager import JOB_MANAGER
        url = row.get("url") or f"https://www.youtube.com/watch?v={vid}"
        JOB_MANAGER.add_video(url, channel_id=row.get("channel_id") or None,
                              channel_name="")
        # 'queued' + deneme sayacı; başarı/hata'yı _analyze_video_core günceller
        mark_live_status(vid, "queued", inc_attempt=True)
        state["tonight_analyzed"] = state.get("tonight_analyzed", 0) + 1
        attempt = (row.get("attempts") or 0) + 1
        log_event("auto_tick", row.get("title") or vid, "info", "enqueued",
                  f"analize gönderildi (deneme {attempt})")
        print(f"[OTO-TARAMA] analize gönderildi: {row.get('title') or vid} "
              f"({state['tonight_analyzed']}/{cap}, deneme {attempt})")
        return vid


def _tick(cfg, asc, eff_now, night_key):
    state = kv_get(_STATE_KEY, {}) or {}
    if state.get("night_key") != night_key:
        # Yeni gece → sayaçları sıfırla, eski dedup kayıtlarını buda
        prune_live_seen(36)
        state = {"night_key": night_key, "tonight_analyzed": 0,
                 "tonight_found": 0, "first_done": False, "last_discovery_ts": 0}

    # İlk keşif 24s; sonrakiler son keşiften beri geçen süre (+1s tampon)
    lookback = int(asc.get("lookback_hours", 24))
    if state.get("first_done") and state.get("last_discovery_ts"):
        gap_h = int((time.time() - state["last_discovery_ts"]) / 3600) + 1
        lookback = min(lookback, max(1, gap_h))

    _discover(cfg, lookback, state)
    state["last_discovery_ts"] = int(time.time())
    state["first_done"] = True

    analyzed = _analyze_one(asc, state)

    # Frame depolama backstop: cap aşıldıysa en eski klasörleri buda
    try:
        from services.storage import prune_frames
        from config import FRAME_STORAGE_CAP_MB
        prune_frames(FRAME_STORAGE_CAP_MB)
    except Exception as e:
        print(f"[OTO-TARAMA] frame budama atlandı: {e}")

    state["last_tick_ts"] = int(time.time())
    state["last_run_iso"] = eff_now.isoformat()
    kv_set(_STATE_KEY, state)
    return analyzed


# ── Döngü + başlatıcı ──────────────────────────────────────────────────────────

def _loop():
    while True:
        try:
            cfg = load_config()
            asc = cfg.get("auto_scan", {}) or {}
            if asc.get("enabled", True):
                tz = asc.get("tz_offset", 3)
                eff = _eff_now(tz)
                cur_min = eff.hour * 60 + eff.minute
                start_min = _parse_hhmm(asc.get("start", "03:00"), 180)
                end_min = _parse_hhmm(asc.get("end", "09:30"), 570)
                if _in_window(cur_min, start_min, end_min):
                    nk = _night_key(eff, start_min, end_min)
                    state = kv_get(_STATE_KEY, {}) or {}
                    interval = max(1, int(asc.get("interval_min", 15)))
                    due = (state.get("night_key") != nk) or \
                          (time.time() - state.get("last_tick_ts", 0) >= interval * 60)
                    if due:
                        with _lock:
                            _tick(cfg, asc, eff, nk)
        except Exception as e:
            print(f"[OTO-TARAMA] döngü hatası: {e}")
        time.sleep(60)


def run_tick_now():
    """Pencere dışında elle bir tick tetikle (test / 'Şimdi tara' butonu)."""
    cfg = load_config()
    asc = cfg.get("auto_scan", {}) or {}
    tz = asc.get("tz_offset", 3)
    eff = _eff_now(tz)
    start_min = _parse_hhmm(asc.get("start", "03:00"), 180)
    end_min = _parse_hhmm(asc.get("end", "09:30"), 570)
    nk = _night_key(eff, start_min, end_min)
    with _lock:
        return _tick(cfg, asc, eff, nk)


def get_status():
    """Panel için durum: pencere, son/sıradaki çalışma, bu-gece sayaçları."""
    cfg = load_config()
    asc = cfg.get("auto_scan", {}) or {}
    state = kv_get(_STATE_KEY, {}) or {}
    tz = asc.get("tz_offset", 3)
    eff = _eff_now(tz)
    cur_min = eff.hour * 60 + eff.minute
    start_min = _parse_hhmm(asc.get("start", "03:00"), 180)
    end_min = _parse_hhmm(asc.get("end", "09:30"), 570)
    interval = max(1, int(asc.get("interval_min", 15)))
    in_win = _in_window(cur_min, start_min, end_min)
    now_ts = time.time()

    if not asc.get("enabled", True):
        next_run_iso = None
    elif in_win:
        last = state.get("last_tick_ts", 0)
        nxt = (last + interval * 60) if last else now_ts
        next_run_iso = _epoch_to_eff_iso(max(nxt, now_ts), tz)
    else:
        sh, sm = divmod(start_min, 60)
        cand = eff.replace(hour=sh, minute=sm, second=0, microsecond=0)
        if cand <= eff:
            cand += timedelta(days=1)
        next_run_iso = cand.isoformat()

    return {
        "enabled": bool(asc.get("enabled", True)),
        "in_window": in_win,
        "window": {"start": asc.get("start", "03:00"), "end": asc.get("end", "09:30")},
        "interval_min": interval,
        "lookback_hours": int(asc.get("lookback_hours", 24)),
        "nightly_cap": int(asc.get("nightly_cap", 30)),
        "tz_offset": int(tz or 0),
        "now_iso": eff.isoformat(),
        "last_run_iso": state.get("last_run_iso"),
        "next_run_iso": next_run_iso,
        "tonight_analyzed": state.get("tonight_analyzed", 0),
        "tonight_found": state.get("tonight_found", 0),
        "pending": count_pending_live(),
        "night_key": state.get("night_key"),
    }


def start_scheduler():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    t = threading.Thread(target=_loop, daemon=True, name="auto-scan")
    t.start()
    print("[OTO-TARAMA] Zamanlayıcı başlatıldı (60 sn döngü)")
