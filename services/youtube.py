import os
import re
import tempfile
import requests as _requests
from config import BASE_DIR, DATA_DIR
from yt_dlp import YoutubeDL

_COOKIE_TMPFILE = None


def _cookie_file_path():
    global _COOKIE_TMPFILE
    # Önce kalıcı veri dizini (Docker volume), sonra proje kökü, sonra env var
    for local in (DATA_DIR / "cookies.txt", BASE_DIR / "cookies.txt"):
        if local.exists():
            return str(local)
    content = os.environ.get("YOUTUBE_COOKIES", "")
    if content:
        if _COOKIE_TMPFILE is None:
            tf = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
            tf.write(content)
            tf.close()
            _COOKIE_TMPFILE = tf.name
        return _COOKIE_TMPFILE
    return None


def has_cookies():
    return _cookie_file_path() is not None


def get_ydl_opts(extra=None):
    opts = {"quiet": True, "no_warnings": True}
    cp = _cookie_file_path()
    if cp:
        opts["cookiefile"] = cp
    if extra:
        opts.update(extra)
    return opts


def channel_id_from_url(url):
    url = url.strip().rstrip("/")
    m = re.search(r"@([a-zA-Z0-9_\-\.]+)", url)
    if m:
        return f"@{m.group(1)}"
    m = re.search(r"channel/([a-zA-Z0-9_\-]+)", url)
    if m:
        return m.group(1)
    return url.split("/")[-1]


def _pick_channel_avatar(thumbnails):
    """Return the best avatar URL from a yt-dlp thumbnails list."""
    if not thumbnails:
        return ""
    # prefer entries explicitly tagged as avatar
    for t in thumbnails:
        if "avatar" in (t.get("id") or "").lower() and t.get("url"):
            return t["url"]
    # fallback: smallest square-ish thumbnail (avatar is usually ≤ 176px)
    small = [t for t in thumbnails if t.get("url") and t.get("height", 9999) <= 176]
    if small:
        small.sort(key=lambda x: x.get("height", 0))
        return small[-1]["url"]  # largest of the small ones → best quality
    return ""


def _entry_ts(d):
    """Flat entry / info'dan yayın zaman damgası (epoch sn) — yoksa 0."""
    try:
        return int(d.get("timestamp") or d.get("release_timestamp") or 0)
    except (TypeError, ValueError):
        return 0


def fetch_live_streams(channel_url, last_hours=0):
    """Scheduler keşfi için ucuz yol — sadece /live + /streams sekmeleri."""
    return fetch_channel_videos(channel_url, last_hours=last_hours,
                                content_type="live", tabs=["live", "streams"])


def fetch_channel_videos(channel_url, last_hours=24, content_type="all", tabs=None):
    base_url = channel_url.rstrip("/")
    for suffix in ("/videos", "/streams", "/shorts", "/featured", "/community", "/live"):
        if base_url.endswith(suffix):
            base_url = base_url[: -len(suffix)]
            break

    all_entries = {}
    channel_name = ""
    channel_id_meta = ""
    channel_avatar = ""

    _TAB_URLS = {
        "videos":  (base_url + "/videos",  "videos"),
        "streams": (base_url + "/streams", "streams"),
        "live":    (base_url + "/live",    "live"),    # canlı yayın varsa yakala
        "main":    (base_url,              "main"),
    }
    tab_order = tabs or ["videos", "streams", "live", "main"]
    tries = [_TAB_URLS[t] for t in tab_order if t in _TAB_URLS]

    for try_url, tab in tries:
        try:
            print(f"[KANAL] Çekiliyor: {try_url}")
            with YoutubeDL(get_ydl_opts({
                "extract_flat": "in_playlist",
                "skip_download": True,
                "playlistend": 20,
                "ignoreerrors": True,
                "socket_timeout": 15,
                # Kanal listesi için web client daha güvenilir
                "extractor_args": {"youtube": {"player_client": ["web"]}},
            })) as ydl:
                info = ydl.extract_info(try_url, download=False)

            if not info:
                print(f"[KANAL] {try_url} → boş")
                continue

            if not channel_name:
                channel_name = (
                    info.get("channel")
                    or info.get("uploader")
                    or info.get("title", "").split(" - ")[0]
                )
            if not channel_id_meta:
                channel_id_meta = info.get("channel_id") or info.get("uploader_id", "")
            if not channel_avatar:
                channel_avatar = _pick_channel_avatar(info.get("thumbnails") or [])

            # /live tek video dönebilir (playlist değil)
            if tab == "live" and info.get("id") and info.get("_type") != "playlist":
                eid = info["id"]
                if eid not in all_entries:
                    all_entries[eid] = {
                        "id": eid,
                        "url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={eid}",
                        "title": info.get("title", "🔴 Canlı Yayın"),
                        "duration": 0,
                        "thumbnail": (
                            info.get("thumbnail")
                            or f"https://i.ytimg.com/vi/{eid}/hqdefault.jpg"
                        ),
                        "view_count": info.get("view_count", 0) or 0,
                        "tab": "live",
                        "is_live": True,
                        "timestamp": _entry_ts(info),
                    }
                print(f"[KANAL] {try_url} → canlı yayın bulundu: {eid}")
                continue

            entries = info.get("entries", []) or []
            flat_entries = []
            for e in entries:
                if not e:
                    continue
                if isinstance(e, dict) and e.get("entries"):
                    flat_entries.extend(s for s in e["entries"] if s)
                else:
                    flat_entries.append(e)

            count = 0
            for entry in flat_entries:
                if not entry:
                    continue
                eid = entry.get("id")
                if not eid or eid in all_entries:
                    continue
                # video / url / url_transparent / None → hepsini al (canlı yayın dahil)
                if entry.get("_type") not in (None, "url", "video", "url_transparent"):
                    continue
                # Shorts atla (kısa süreli veya /shorts/ url)
                _dur = entry.get("duration", 0) or 0
                _url = entry.get("url", "") or ""
                from config import SHORTS_MAX_DURATION
                if "/shorts/" in _url or (0 < _dur <= SHORTS_MAX_DURATION):
                    continue
                is_live = bool(entry.get("is_live") or entry.get("was_live"))
                all_entries[eid] = {
                    "id": eid,
                    "url": entry.get("url") or f"https://www.youtube.com/watch?v={eid}",
                    "title": entry.get("title", "") or "Başlıksız",
                    "duration": entry.get("duration", 0) or 0,
                    "thumbnail": (
                        entry.get("thumbnail")
                        or f"https://i.ytimg.com/vi/{eid}/hqdefault.jpg"
                    ),
                    "view_count": entry.get("view_count", 0) or 0,
                    "tab": tab,
                    "is_live": is_live,
                    "timestamp": _entry_ts(entry),
                }
                count += 1
            print(f"[KANAL] {try_url} → {count} yeni video (toplam: {len(all_entries)})")

            if len(all_entries) >= 15 and tab == "videos":
                continue

        except Exception as e:
            err = str(e)
            if "Please sign in" in err or "Sign in" in err:
                print(f"[KANAL] {try_url} → YouTube cookie istedi. "
                      f"YOUTUBE_COOKIES env var'ını Railway'e ekle.")
            else:
                print(f"[KANAL] {try_url} → HATA: {err}")
            continue

    videos_list = list(all_entries.values())

    # İçerik tipi filtresi: canlı yayın (streams/live) vs normal video
    def _is_live_content(v):
        return bool(v.get("is_live")) or v.get("tab") in ("streams", "live")
    if content_type == "live":
        videos_list = [v for v in videos_list if _is_live_content(v)]
    elif content_type == "video":
        videos_list = [v for v in videos_list if not _is_live_content(v)]

    import time as _time
    now = int(_time.time())

    # Zaman penceresi filtresi (last_hours>0). Zaman damgası BİLİNMEYEN entry'ler
    # ATILMAZ — kaçırmamak için tutulur (flat extraction her zaman tarih vermez).
    if last_hours and last_hours > 0:
        cut = now - int(last_hours) * 3600
        videos_list = [v for v in videos_list
                       if (not v.get("timestamp")) or v["timestamp"] >= cut]

    # En yeni → en eski sırala. Tarihi bilinmeyen canlı/yayın içerik "şimdi"
    # sayılıp üste çıkar; diğer tarihsizler en alta düşer.
    def _sort_key(v):
        ts = v.get("timestamp") or 0
        if ts:
            return ts
        if _is_live_content(v):
            return now + 1
        return 0
    videos_list.sort(key=_sort_key, reverse=True)

    print(f"[KANAL] SONUÇ: {channel_name} - {len(videos_list)} video/yayın "
          f"(tip: {content_type}, avatar: {'evet' if channel_avatar else 'yok'})")
    return {
        "channel_name": channel_name or channel_id_from_url(channel_url),
        "channel_id": channel_id_meta,
        "videos": videos_list,
        "channel_avatar": channel_avatar,
    }
