"""
7/24 otomatik canlı-yayın taraması zamanlayıcısı.

Web process'inde tek bir daemon thread olarak çalışır (gunicorn --workers 1 →
tek instance, çift tetikleme yok). 60 sn'de bir uyanır, config'i YENİDEN okur
(ayar değişikliği restart'sız etki eder) ve SÜREKLİ tick atar:
  • start–end arası "yoğun pencere": her `interval_min` dk (varsayılan 15)
  • pencere dışı "normal mod": her `day_interval_min` dk (varsayılan 30)

Her tick:
  1. requeue_stale_queued — worker ölümünde takılı 'queued' satırları kurtar
  2. _recheck_live_waits — bekleyen canlı yayınlar bitti mi? bittiyse kuyruğa
  3. _discover — kanallarda yeni canlı yayın keşfi (sürenler live_wait'e)
  4. _analyze_one — kuyruk BOŞSA bekleyen havuzdan FIFO bir yayını analize gönder
     (günlük üst sınır `daily_cap`; sayaç TR takvim gününe göre sıfırlanır)
"""

import time
import threading
from datetime import datetime, timedelta

from config import load_config
from models.database import (
    is_live_seen, mark_live_seen, mark_live_status, next_pending_live,
    count_pending_live, prune_live_seen, is_video_completed,
    requeue_stale_queued, set_live_wait, list_live_waits, mark_live_check,
    count_waiting_live,
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


def _epoch_to_eff_iso(epoch, tz):
    return (datetime.utcfromtimestamp(epoch) + timedelta(hours=int(tz or 0))).isoformat()


# ── Keşif + analiz ─────────────────────────────────────────────────────────────

def _discover(cfg, lookback, state):
    """Tüm kanallarda SON `lookback` saatteki canlı yayınları keşfet, yeni
    olanları live_seen'e işle. Her kanal için sonuç loglanır (Durum paneli)."""
    from services.youtube import fetch_live_streams, channel_id_from_url
    from models.database import live_seen_ids
    channels = cfg.get("channels", [])
    known = live_seen_ids()   # bilinenleri tek seferde al → tekrar tarih sorgusu yok
    new_count = 0
    for url in channels:
        try:
            res = fetch_live_streams(url, last_hours=lookback, known_ids=known)
        except Exception as e:
            print(f"[OTO-TARAMA] keşif hata ({url}): {e}")
            code, _ = _classify(str(e))
            log_event("channel_scan", url, "error", code, str(e)[:200])
            continue
        cid = channel_id_from_url(url)
        cname = res.get("channel_name") or cid
        ch_new = 0
        for v in res.get("videos", []):
            vid = v.get("id")
            if not vid or is_live_seen(vid) or is_video_completed(vid):
                continue
            mark_live_seen(vid, channel_id=cid, title=v.get("title", ""),
                           url=v.get("url", ""), analyzed=False)
            if v.get("is_live"):
                # Yayın SÜRÜYOR → kuyruğa alma; bitmesini bekle (canlı-kenar
                # örneklemesi yalnız ilk ~10 dk'yı görür ve yanlış veri üretir).
                set_live_wait(vid)
            ch_new += 1
        new_count += ch_new
        found = len(res.get("videos", []))
        log_event("channel_scan", cname, "ok", "discover",
                  f"{found} canlı yayın (son {lookback}s) · {ch_new} yeni")
    if new_count:
        state["today_found"] = state.get("today_found", 0) + new_count
        print(f"[OTO-TARAMA] toplam {new_count} yeni canlı yayın keşfedildi")
    return new_count


def _recheck_live_waits(asc):
    """Bekleyen canlı yayınlar bitti mi? Bittiyse 'pending'e al (tam VOD analizi
    sıradaki tick'te kuyruğa girer). Satır başına `live_recheck_min` dk throttle."""
    from services.youtube import _resolve_video_meta
    recheck_min = int(asc.get("live_recheck_min", 45))
    ttl_h = int(asc.get("live_wait_ttl_hours", 12))
    for row in list_live_waits(recheck_min=recheck_min, limit=5):
        vid = row["video_id"]
        title = row.get("title") or vid
        ws = row.get("wait_since") or row.get("seen_at") or ""
        try:
            waited_h = (datetime.utcnow() - datetime.fromisoformat(ws)).total_seconds() / 3600
        except (TypeError, ValueError):
            waited_h = 0
        if waited_h > ttl_h:
            mark_live_status(vid, "permanent",
                             error=f"canlı yayın {ttl_h} saatte bitmedi (7/24 yayın olabilir)")
            log_event("auto_tick", title, "warn", "live_wait_ttl",
                      f"{ttl_h} saattir canlı — beklemekten vazgeçildi")
            continue
        url = row.get("url") or f"https://www.youtube.com/watch?v={vid}"
        try:
            _, live_status = _resolve_video_meta(url)
        except Exception:
            live_status = "error"
        mark_live_check(vid)
        if live_status in ("was_live", "post_live", "not_live"):
            mark_live_status(vid, "pending")
            log_event("auto_tick", title, "ok", "live_ended",
                      "yayın bitti — tam analiz sıraya alındı")
            print(f"[OTO-TARAMA] yayın bitti, analize hazır: {title}")
        elif live_status == "error" and (row.get("check_count") or 0) + 1 >= 6:
            mark_live_status(vid, "failed", error="canlı durum 6 kez çözülemedi",
                             inc_attempt=True)
        # is_live / is_upcoming → beklemeye devam (yalnız last_check güncellendi)


def _classify(err):
    e = (err or "").lower()
    if "sign in" in e:
        return "cookie_expired", "cookie"
    return "scan_error", "transient"


def _analyze_one(asc, state):
    """Bekleyen havuzdan FIFO bir yayını tam analize gönder — yalnız kuyruk boşsa
    (worker meşgulken yığılma olmasın; boşalınca her tick'te beslenir)."""
    cap = int(asc.get("daily_cap", 70))
    if state.get("today_analyzed", 0) >= cap:
        print(f"[OTO-TARAMA] günlük üst sınıra ({cap}) ulaşıldı — analiz atlandı")
        return None
    from services.job_manager import JOB_MANAGER
    try:
        qs = JOB_MANAGER.queue_status()
        if qs.get("queue_length", 0) > 0 or qs.get("running"):
            return None
    except Exception:
        pass
    while True:
        row = next_pending_live()
        if not row:
            return None
        vid = row["video_id"]
        if is_video_completed(vid):
            mark_live_status(vid, "done")        # zaten bitmiş/analiz edilmiş → atla
            continue
        url = row.get("url") or f"https://www.youtube.com/watch?v={vid}"
        JOB_MANAGER.add_video(url, channel_id=row.get("channel_id") or None,
                              channel_name="", title=row.get("title") or "")
        # 'queued' + deneme sayacı; başarı/hata'yı _analyze_video_core günceller
        mark_live_status(vid, "queued", inc_attempt=True)
        state["today_analyzed"] = state.get("today_analyzed", 0) + 1
        attempt = (row.get("attempts") or 0) + 1
        log_event("auto_tick", row.get("title") or vid, "info", "enqueued",
                  f"analize gönderildi (deneme {attempt})")
        print(f"[OTO-TARAMA] analize gönderildi: {row.get('title') or vid} "
              f"({state['today_analyzed']}/{cap}, deneme {attempt})")
        return vid


def _tick(cfg, asc, eff_now, day_key):
    state = kv_get(_STATE_KEY, {}) or {}
    # Eski state 'night_key' kullanıyordu — bir kez day_key'e taşı
    if state.get("day_key") is None and state.get("night_key"):
        state["day_key"] = state.pop("night_key")
        state["today_analyzed"] = state.pop("tonight_analyzed", 0)
        state["today_found"] = state.pop("tonight_found", 0)
    if state.get("day_key") != day_key:
        # Yeni gün → sayaçları sıfırla, eski kayıtları durum-duyarlı buda
        prune_live_seen(36)
        state = {"day_key": day_key, "today_analyzed": 0,
                 "today_found": 0, "first_done": False, "last_discovery_ts": 0}

    # Worker ölümünde takılı kalan 'queued' satırları kurtar
    try:
        n = requeue_stale_queued(90)
        if n:
            print(f"[OTO-TARAMA] {n} takılı 'queued' kayıt yeniden kuyruğa alınabilir yapıldı")
    except Exception as e:
        print(f"[OTO-TARAMA] requeue hatası: {e}")

    # Bekleyen canlı yayınlar bitti mi? (Gemini maliyeti yok — cap'ten bağımsız)
    try:
        _recheck_live_waits(asc)
    except Exception as e:
        print(f"[OTO-TARAMA] canlı-bekle kontrolü hatası: {e}")

    # İlk keşif 24s; sonrakiler son keşiften beri geçen süre (+1s tampon)
    lookback = int(asc.get("lookback_hours", 24))
    if state.get("first_done") and state.get("last_discovery_ts"):
        gap_h = int((time.time() - state["last_discovery_ts"]) / 3600) + 1
        lookback = min(lookback, max(1, gap_h))

    _discover(cfg, lookback, state)
    state["last_discovery_ts"] = int(time.time())
    state["first_done"] = True

    analyzed = _analyze_one(asc, state)

    # Frame bakımı: eski kareleri (retention) sil + cap aşımını buda → yer aç
    try:
        from services.storage import frame_maintenance
        frame_maintenance()
    except Exception as e:
        print(f"[OTO-TARAMA] frame bakımı atlandı: {e}")

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
            from services.job_manager import is_scan_paused
            if asc.get("enabled", True) and not is_scan_paused():
                tz = asc.get("tz_offset", 3)
                eff = _eff_now(tz)
                cur_min = eff.hour * 60 + eff.minute
                start_min = _parse_hhmm(asc.get("start", "03:00"), 180)
                end_min = _parse_hhmm(asc.get("end", "09:30"), 570)
                # 7/24: pencere yalnız TEMPOYU belirler (yoğun / normal),
                # pencere dışında da tarama devam eder.
                in_win = _in_window(cur_min, start_min, end_min)
                interval = max(1, int(asc.get("interval_min", 15)) if in_win
                               else int(asc.get("day_interval_min", 30)))
                dk = eff.date().isoformat()
                state = kv_get(_STATE_KEY, {}) or {}
                cur_key = state.get("day_key") or state.get("night_key")
                due = (cur_key != dk) or \
                      (time.time() - state.get("last_tick_ts", 0) >= interval * 60)
                if due:
                    with _lock:
                        _tick(cfg, asc, eff, dk)
        except Exception as e:
            print(f"[OTO-TARAMA] döngü hatası: {e}")
        time.sleep(60)


def run_tick_now():
    """Elle bir tick tetikle (test / 'Şimdi tara' butonu)."""
    cfg = load_config()
    asc = cfg.get("auto_scan", {}) or {}
    tz = asc.get("tz_offset", 3)
    eff = _eff_now(tz)
    with _lock:
        return _tick(cfg, asc, eff, eff.date().isoformat())


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
    day_interval = max(1, int(asc.get("day_interval_min", 30)))
    eff_interval = interval if in_win else day_interval
    now_ts = time.time()

    # 7/24: sıradaki çalışma her zaman "son tick + etkin tempo"
    if not asc.get("enabled", True):
        next_run_iso = None
    else:
        last = state.get("last_tick_ts", 0)
        nxt = (last + eff_interval * 60) if last else now_ts
        next_run_iso = _epoch_to_eff_iso(max(nxt, now_ts), tz)

    today_analyzed = state.get("today_analyzed", state.get("tonight_analyzed", 0))
    today_found = state.get("today_found", state.get("tonight_found", 0))
    daily_cap = int(asc.get("daily_cap", 70))

    return {
        "enabled": bool(asc.get("enabled", True)),
        "in_window": in_win,
        "mode": "intensive" if in_win else "normal",
        "window": {"start": asc.get("start", "03:00"), "end": asc.get("end", "09:30")},
        "interval_min": interval,
        "day_interval_min": day_interval,
        "lookback_hours": int(asc.get("lookback_hours", 24)),
        "daily_cap": daily_cap,
        "nightly_cap": daily_cap,   # eski frontend cache'i için alias
        "tz_offset": int(tz or 0),
        "now_iso": eff.isoformat(),
        "last_run_iso": state.get("last_run_iso"),
        "next_run_iso": next_run_iso,
        "today_analyzed": today_analyzed,
        "today_found": today_found,
        "tonight_analyzed": today_analyzed,   # alias
        "tonight_found": today_found,         # alias
        "pending": count_pending_live(),
        "waiting_live": count_waiting_live(),
        "day_key": state.get("day_key") or state.get("night_key"),
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
