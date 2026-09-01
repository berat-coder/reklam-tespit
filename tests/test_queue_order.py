"""
Kuyruk sırası + akış URL doğrulaması testleri (AĞ YOK, GEÇİCİ DB).

Neden kritik:
  • next_pending_live tek sıralama (seen_at ASC) kullandigi icin GUNLER once
    basarisiz olmus kayitlar bugunku taze icerigin onune geciyordu: uretimde
    13 taze 'pending' beklerken analiz slotlarini 27-28 Agustos tarihli
    'failed' kayitlar tuketiyordu.
  • Bir client URL DONDURUP o URL 403 verebiliyor. Uretimde olculdu
    (xn6yUkD2hGg): mweb -> HTTP 403, tv_simply -> HTTP 206. Sira mweb'i once
    denedigi icin calismayan URL kabul ediliyor ve arama duruyordu.

Calistirma:  ./.venv/bin/python tests/test_queue_order.py
"""
import sys, os, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Uretim Postgres'ine BAGLANMA: bos string environ'da kaldigi icin
# config.py'nin load_dotenv() cagrisi DATABASE_URL'i geri yukleyemez.
os.environ["DATABASE_URL"] = ""
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["AUTO_SCAN_ENABLED"] = "0"

import app as _a                                                  # noqa: E402,F401
from models.database import get_db, next_pending_live             # noqa: E402
from services import tasks as T                                   # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  → {extra}" if not cond else ""))
    if not cond:
        ok = False


def ekle(vid, status, attempts, seen_at, last_attempt=None):
    with get_db() as c:
        c.execute("""INSERT INTO live_seen (video_id, status, attempts, seen_at,
                     last_attempt, title, url, channel_id)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                  (vid, status, attempts, seen_at, last_attempt, vid, "u", "ch"))


print("\n[1] TAZE 'pending' ESKİ 'failed' RETRY'SİNİN ÖNÜNDE OLMALI")
ekle("eski_failed", "failed", 1, "2026-08-27T10:00:00", "2026-08-27T10:00:00")
ekle("taze_pending", "pending", 0, "2026-09-01T07:00:00")
r = next_pending_live()
check("pending seçildi (eski failed değil)", r and r["video_id"] == "taze_pending",
      r and r["video_id"])

print("\n[2] pending BİTİNCE failed retry sırası gelir")
with get_db() as c:
    c.execute("UPDATE live_seen SET status='done' WHERE video_id='taze_pending'")
r = next_pending_live()
check("şimdi eski failed seçildi", r and r["video_id"] == "eski_failed",
      r and r["video_id"])

print("\n[3] pending GRUBU İÇİNDE FIFO korunur (eski bekleyen aç kalmasın)")
ekle("pending_eski", "pending", 0, "2026-08-28T10:00:00")
ekle("pending_yeni", "pending", 0, "2026-09-01T08:00:00")
r = next_pending_live()
check("iki pending'den ESKİ olan seçildi", r and r["video_id"] == "pending_eski",
      r and r["video_id"])

print("\n[4] DENEME TAVANI: attempts >= max olan failed alınmaz")
with get_db() as c:
    c.execute("DELETE FROM live_seen")
ekle("tavan_asan", "failed", 4, "2026-08-27T10:00:00", "2026-08-27T10:00:00")
check("attempts=4, max=4 → alınmaz", next_pending_live() is None)
with get_db() as c:
    c.execute("UPDATE live_seen SET attempts=3 WHERE video_id='tavan_asan'")
check("attempts=3 → alınır", (next_pending_live() or {}).get("video_id") == "tavan_asan")

print("\n[5] AKIŞ URL DOĞRULAMASI: 403 kabul edilmemeli")
import urllib.error, urllib.request                                # noqa: E402


class _SahteYanit:
    def __init__(self, code): self.status = code
    def getcode(self): return self.status
    def __enter__(self): return self
    def __exit__(self, *a): return False


def sahte_opener(code=None, hata=None):
    class _O:
        def open(self, req, timeout=None):
            if hata:
                raise hata
            return _SahteYanit(code)
    return lambda *a, **kw: _O()


gercek = urllib.request.build_opener
try:
    urllib.request.build_opener = sahte_opener(code=206)
    check("HTTP 206 → kabul", T._stream_url_ok("http://x")[0] is True)
    urllib.request.build_opener = sahte_opener(code=200)
    check("HTTP 200 → kabul", T._stream_url_ok("http://x")[0] is True)
    urllib.request.build_opener = sahte_opener(
        hata=urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None))
    ok403, why = T._stream_url_ok("http://x")
    check("HTTP 403 → RED", ok403 is False, why)
    check("sebep mesajda", "403" in why, why)
    urllib.request.build_opener = sahte_opener(hata=TimeoutError("zaman aşımı"))
    check("zaman aşımı → RED (çökmeden)", T._stream_url_ok("http://x")[0] is False)
finally:
    urllib.request.build_opener = gercek

print("\n[6] ffmpeg BAŞLIK ARGÜMANLARI")
a = T._ffmpeg_header_args({"User-Agent": "UA/1", "Accept": "*/*", "Host": "x",
                           "Range": "bytes=0-", "Cookie": ""})
check("User-Agent ayrı bayrağa", "-user_agent" in a and "UA/1" in a, a)
check("Host/Range elendi", "Host" not in " ".join(a) and "Range" not in " ".join(a), a)
check("boş değer atlandı", "Cookie" not in " ".join(a), a)
check("başlık yoksa argüman yok", T._ffmpeg_header_args(None) == [])

try:
    os.remove(os.environ["DB_PATH"])
except OSError:
    pass
print("\n" + ("TÜM TESTLER GEÇTİ" if ok else "BAŞARISIZ"))
sys.exit(0 if ok else 1)
