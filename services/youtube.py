import re
from config import BASE_DIR
from yt_dlp import YoutubeDL


def get_ydl_opts(extra=None):
    opts = {"quiet": True, "no_warnings": True}
    cookie_file = BASE_DIR / "cookies.txt"
    if cookie_file.exists():
        opts["cookiefile"] = str(cookie_file)
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


def fetch_channel_videos(channel_url, last_hours=24):
    base_url = channel_url.rstrip("/")
    for suffix in ("/videos", "/streams", "/shorts", "/featured", "/community"):
        if base_url.endswith(suffix):
            base_url = base_url[: -len(suffix)]
            break

    all_entries = {}
    channel_name = ""
    channel_id_meta = ""

    tries = [
        (base_url + "/videos", "videos"),
        (base_url + "/streams", "streams"),
        (base_url, "main"),
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
                continue

            if not channel_name:
                channel_name = (
                    info.get("channel")
                    or info.get("uploader")
                    or info.get("title", "").split(" - ")[0]
                )
            if not channel_id_meta:
                channel_id_meta = info.get("channel_id") or info.get("uploader_id", "")

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
                if entry.get("_type") not in (None, "url", "video"):
                    continue
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
                }
                count += 1
            print(f"[KANAL] {try_url} → {count} yeni video (toplam: {len(all_entries)})")

            if len(all_entries) >= 15 and tab == "videos":
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
