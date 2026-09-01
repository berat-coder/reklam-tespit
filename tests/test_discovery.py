"""
Otomatik keşif regresyon testleri — AĞ YOK (youtube fonksiyonlari taklit edilir).

Neden kritik: otomatik sistem SIRADAN VIDEOLARI hicbir zaman taramiyordu.
_discover yalniz fetch_live_streams cagiriyor, auto_scan.content_type ayarini
hic okumuyordu. Olcum (2026-09-01): takip edilen 11 kanalda son 24 saatte
9 video yayinlanmis, 8'i sistemde hic yoktu — hepsi canli olmayan yukleme.

Ayrica gerıye-bakis penceresi her tick'te 1 saate cokuyordu (gap_h aritmetigi).

Calistirma:  ./.venv/bin/python tests/test_discovery.py
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import scheduler as S                              # noqa: E402
from config import _merge_auto_scan, AUTO_SCAN_MIN               # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  → {extra}" if not cond else ""))
    if not cond:
        ok = False


# ── Taklitler ──────────────────────────────────────────────────────────────
CANLI = {"channel_name": "Kanal", "videos": [
    {"id": "live1", "title": "CANLI maç", "url": "u1", "is_live": True}]}
VIDEOLAR = {"channel_name": "Kanal", "videos": [
    {"id": "vid1", "title": "Yorum videosu", "url": "u2"},
    {"id": "vid2", "title": "Analiz videosu", "url": "u3"}]}


def kur(ct):
    """Taklitleri kur, _discover'i calistir, (cagrilar, isaretlenenler) dondur."""
    cagri = {"live": 0, "video": 0, "lookback": None, "tabs": None}
    isaret, bekleyen = [], []
    import services.youtube as Y

    def sahte_live(url, last_hours=24, known_ids=None, **kw):
        cagri["live"] += 1; cagri["lookback"] = last_hours
        return CANLI

    def sahte_video(url, last_hours=24, content_type="all", tabs=None, **kw):
        cagri["video"] += 1; cagri["lookback"] = last_hours; cagri["tabs"] = tabs
        return VIDEOLAR

    eski = (Y.fetch_live_streams, Y.fetch_channel_videos,
            S.is_live_seen, S.is_video_completed, S.mark_live_seen,
            S.set_live_wait, S.log_event)
    Y.fetch_live_streams = sahte_live
    Y.fetch_channel_videos = sahte_video
    S.is_live_seen = lambda v: False
    S.is_video_completed = lambda v: False
    S.mark_live_seen = lambda vid, **kw: isaret.append(vid)
    S.set_live_wait = lambda vid: bekleyen.append(vid)
    S.log_event = lambda *a, **kw: None
    try:
        n = S._discover({"channels": ["https://youtube.com/@k"]}, 24, {}, content_type=ct)
    finally:
        (Y.fetch_live_streams, Y.fetch_channel_videos, S.is_live_seen,
         S.is_video_completed, S.mark_live_seen, S.set_live_wait, S.log_event) = eski
    return n, cagri, isaret, bekleyen


print("\n[1] content_type='all' → HEM canlı HEM sıradan video")
n, c, isaret, bekleyen = kur("all")
check("canlı kaynağı çağrıldı", c["live"] == 1, c)
check("video kaynağı çağrıldı", c["video"] == 1, c)
check("3 içerik işaretlendi", sorted(isaret) == ["live1", "vid1", "vid2"], isaret)
check("yalnız canlı olan beklemeye alındı", bekleyen == ["live1"], bekleyen)
check("yeni sayısı 3", n == 3, n)
check("video sekmesi tek istek (tabs=['videos'])", c["tabs"] == ["videos"], c["tabs"])

print("\n[2] content_type='live' → sıradan videolara BAKMAZ")
n, c, isaret, _ = kur("live")
check("video kaynağı çağrılmadı", c["video"] == 0, c)
check("yalnız canlı işaretlendi", isaret == ["live1"], isaret)

print("\n[3] content_type='video' → canlıya BAKMAZ")
n, c, isaret, bekleyen = kur("video")
check("canlı kaynağı çağrılmadı", c["live"] == 0, c)
check("yalnız videolar işaretlendi", sorted(isaret) == ["vid1", "vid2"], isaret)
check("hiçbiri beklemeye alınmadı", bekleyen == [], bekleyen)

print("\n[4] GERİYE-BAKIŞ PENCERESİ 1 SAATE ÇÖKMESİN")
check("taban tanımlı", S.MIN_LOOKBACK_H >= 6, S.MIN_LOOKBACK_H)
# tick araligi 69 sn (uretimde olculen) → eski aritmetikle gap_h = 1
for gap_sn, ad in ((69, "69 sn (üretimdeki gerçek tick)"), (300, "5 dk"), (3600, "1 saat")):
    eski_gap = int(gap_sn / 3600) + 1
    eski_lb = min(24, max(1, eski_gap))
    yeni_lb = min(24, max(S.MIN_LOOKBACK_H, int(gap_sn // 3600) + 1))
    check(f"{ad}: eski={eski_lb}s → yeni={yeni_lb}s", yeni_lb >= S.MIN_LOOKBACK_H,
          f"yeni={yeni_lb}")
# uzun kesintide gercek bosluk kullanilsin
uzun = min(24, max(S.MIN_LOOKBACK_H, int(10 * 3600 // 3600) + 1))
check("10 saatlik kesinti → 11 saat geriye bak", uzun == 11, uzun)

print("\n[5] AYAR ALT SINIRLARI OKUMA yolunda da uygulanmalı")
a = _merge_auto_scan({"interval_min": 1, "day_interval_min": 1, "content_type": "live"})
check("interval_min 1 → 5'e çekildi", a["interval_min"] == AUTO_SCAN_MIN["interval_min"], a)
check("day_interval_min 1 → 5'e çekildi", a["day_interval_min"] == 5, a)
check("eski 'live' varsayılanı 'all'a taşındı", a["content_type"] == "all", a)
b = _merge_auto_scan({"content_type": "video"})
check("bilinçli 'video' seçimi korunur", b["content_type"] == "video", b)
c2 = _merge_auto_scan({"interval_min": 30})
check("makul değer bozulmaz", c2["interval_min"] == 30, c2)

print("\n" + ("TÜM TESTLER GEÇTİ" if ok else "BAŞARISIZ"))
sys.exit(0 if ok else 1)
