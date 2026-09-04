"""
YouTube hiz siniri (bot-flag / 429) geri cekilme testleri — AG YOK, GECICI DB.

Neden kritik: Railway datacenter IP'si belirli bir istek hacminden sonra
YouTube tarafindan bot olarak isaretleniyor. Sistem bunu KENDI KENDINE
derinlestiriyordu: bir video bot-flag alinca kod kalan 6 client'i da deniyor
(hepsi ayni cevabi aliyor), sonra siradaki videoya gecip yine 7 istek atiyordu.
Uretim logu: 4 dakikada 3 video x 7 = 21 isaretli istek.

Olculdu (2026-09-01): ayni videolar yerel baglantidan PO token OLMADAN bile
cekilebiliyor → videolar erisilebilir, engel IP + hacim kaynakli.

Calistirma:  ./.venv/bin/python tests/test_rate_limit.py
"""
import sys, os, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = ""
_TMPDIR = tempfile.mkdtemp(prefix="rt-rate-")
os.environ["DATA_DIR"] = _TMPDIR
os.environ["AUTO_SCAN_ENABLED"] = "0"

import app as _a                                                   # noqa: E402,F401
from services import tasks as T                                    # noqa: E402
from models.database import kv_get as _kvg, kv_set as _kvs           # noqa: E402


def kv_get_(k):
    return _kvg(k, {}) or {}


def kv_set_(k, v):
    _kvs(k, v)

ok = True


def check(name, cond, extra=""):
    global ok
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  → {extra}" if not cond else ""))
    if not cond:
        ok = False


print("\n[1] BOT-FLAG / 429 TANIMA")
for m, bekle in [
    ("Sign in to confirm you're not a bot. Use --cookies", True),
    ("HTTP Error 429: Too Many Requests", True),
    ("Unable to download webpage: HTTP Error 429", True),
    ("No title found in player responses", False),   # belirti, sebep değil
    ("Video unavailable", False),
    ("Requested format is not available", False),
    ("", False),
    (None, False),
]:
    check(f"{'HIZ' if bekle else 'normal'}: {str(m)[:40]}", T.is_rate_limit_msg(m) is bekle)

print("\n[1b] KENDİ TÜRKÇE MESAJIMIZ DA TANINMALI")
# Client döngüsü İngilizce bot-flag'i yakalayıp mesajı TÜRKÇEYE çeviriyor ve
# aşağı aktarıyor. Dedektör yalnız İngilizce metni tanıdığı için _record_fail
# kaydı 'failed' yazıp deneme bütçesini harcıyordu (üretimde tam böyle oldu).
check("'hız sınırı' tanınıyor",
      T.is_rate_limit_msg("YouTube hız sınırı — 10 dk sonra denenecek"))
check("'bot sayıldı' tanınıyor",
      T.is_rate_limit_msg("IP geçici olarak bot sayıldı"))
check("normal Türkçe hata yanlış eşleşmiyor",
      not T.is_rate_limit_msg("Frame çıkarılamadı — ffmpeg: 403 Forbidden"))

print("\n[2] ÜSTEL GERİ ÇEKİLME")
T.clear_rate_limit()
check("başlangıçta soğuma yok", T.yt_cooldown_remaining() == 0)
w1 = T.note_rate_limit()
check(f"1. flag → {w1} sn", w1 == T.YT_COOLDOWN_BASE, w1)
check("soğuma başladı", T.yt_cooldown_remaining() > 0, T.yt_cooldown_remaining())
# Tırmanma yalnız soğuma BİTTİKTEN sonra gelen yeni flag'te olur.
def _flag_sonrasi():
    """Soğumayı bitmiş say (until=0) ama seriyi ve son flag'i koru."""
    st = kv_get_(T._COOLDOWN_KEY)
    st["until"] = 0
    st["last_flag"] = time.time()      # yakın → sönme devreye girmesin
    kv_set_(T._COOLDOWN_KEY, st)
    return T.note_rate_limit()


w2 = _flag_sonrasi()
check(f"2. flag → iki katı ({w2} sn)", w2 == T.YT_COOLDOWN_BASE * 2, w2)
w3 = _flag_sonrasi()
# Beklenti TAVANA göre yazılmalı: taban 600 sn ve tavan 1800 sn ise
# 3. flag'in "dört katı" (2400) zaten kırpılır.
check(f"3. flag → tavana kırpılmış katlanma ({w3} sn)",
      w3 == min(T.YT_COOLDOWN_MAX, T.YT_COOLDOWN_BASE * 4), w3)
for _ in range(12):
    son = _flag_sonrasi()
check(f"tavan aşılmıyor ({son} sn)", son == T.YT_COOLDOWN_MAX, son)

print("\n[2b] MANDAL KIRILDI: aynı engel bir kez tırmanır")
T.clear_rate_limit()
w1 = T.note_rate_limit()
w_ayni = T.note_rate_limit()          # soğuma HÂLÂ aktifken ikinci flag
check("soğuma aktifken seri artmadı", w_ayni <= w1, (w1, w_ayni))
st = kv_get_(T._COOLDOWN_KEY)
check("seri 1'de kaldı (14'e tırmanmıyor)", st.get("streak") == 1, st)
for _ in range(6):
    T.note_rate_limit()               # üst üste altı belirti daha
st = kv_get_(T._COOLDOWN_KEY)
check("altı belirti sonrası seri hâlâ 1", st.get("streak") == 1, st)

print("\n[2c] ZAMANLA SÖNME: uzun temiz aradan sonra seri sıfırlanır")
# son flag'i çok eskiye al → sönme devreye girmeli
kv_set_(T._COOLDOWN_KEY, {"until": 0, "streak": 5,
                          "last_flag": time.time() - 10 * 3600,
                          "wait": T.YT_COOLDOWN_BASE})
w = T.note_rate_limit()
check(f"seri sıfırlandı → taban bekleme ({w} sn)", w == T.YT_COOLDOWN_BASE, w)
# yakın zamanda flag varsa sönme OLMAZ
kv_set_(T._COOLDOWN_KEY, {"until": 0, "streak": 2,
                          "last_flag": time.time() - 30,
                          "wait": T.YT_COOLDOWN_BASE})
w = T.note_rate_limit()
check(f"yeni flag → tırmanma sürüyor ({w} sn)",
      w == min(T.YT_COOLDOWN_MAX, T.YT_COOLDOWN_BASE * 4), w)

print("\n[2d] TAVAN 30 DAKİKA (saatlerce durmak veri kaçırmak demek)")
check(f"tavan {T.YT_COOLDOWN_MAX} sn = 30 dk", T.YT_COOLDOWN_MAX == 1800,
      T.YT_COOLDOWN_MAX)

print("\n[3] BAŞARI SONRASI SIFIRLAMA")
T.clear_rate_limit()
check("soğuma temizlendi", T.yt_cooldown_remaining() == 0)
check("seri sıfırlandı → sonraki flag yine taban",
      T.note_rate_limit() == T.YT_COOLDOWN_BASE)
T.clear_rate_limit()

print("\n[4] ZAMANLAYICI SOĞUMADA TICK ATLAR")
from services import scheduler as S                                # noqa: E402
T.note_rate_limit()
cagrildi = {"discover": 0}
eski = S._discover
S._discover = lambda *a, **kw: cagrildi.__setitem__("discover", cagrildi["discover"] + 1)
try:
    sonuc = S._tick({"channels": ["x"]}, {}, __import__("datetime").datetime.utcnow(),
                    "2026-09-01")
finally:
    S._discover = eski
check("tick None döndü (atlandı)", sonuc is None, sonuc)
check("keşif HİÇ çağrılmadı", cagrildi["discover"] == 0, cagrildi)
T.clear_rate_limit()

print("\n[4b] ORTAM ENGELİ DENEME BÜTÇESİNİ HARCAMAMALI")
# Hız sınırı videoyla ilgili değil. Sayaç kuyruğa alınırken artıyor; geri
# alınmazsa birkaç saatlik IP engeli kuyruktaki HER videoyu kalıcı mahsur
# bırakıyor (uretimde 71 kayit tam boyle strandledi).
from models.database import mark_live_status, get_db, get_live_attempts   # noqa: E402
with get_db() as _c:
    _c.execute("""INSERT INTO live_seen (video_id,status,attempts,seen_at,title,url,channel_id)
                  VALUES ('rl1','queued',2,'2026-09-01T07:00:00','t','u','ch')""")
check("başlangıç deneme=2", get_live_attempts("rl1") == 2, get_live_attempts("rl1"))
mark_live_status("rl1", "pending", error="Sign in to confirm you're not a bot",
                 dec_attempt=True)
check("hız sınırı → deneme geri alındı (1)", get_live_attempts("rl1") == 1,
      get_live_attempts("rl1"))
with get_db() as _c:
    _r = dict(_c.execute("SELECT status FROM live_seen WHERE video_id='rl1'").fetchone())
check("durum 'pending' (kuyruğa geri döner)", _r["status"] == "pending", _r)
mark_live_status("rl1", "pending", dec_attempt=True)
mark_live_status("rl1", "pending", dec_attempt=True)
check("0'ın altına inmiyor", get_live_attempts("rl1") == 0, get_live_attempts("rl1"))

print("\n[5] SOĞUMA BİTİNCE SERBEST")
from models.database import kv_set                                 # noqa: E402
kv_set(T._COOLDOWN_KEY, {"until": time.time() - 1, "streak": 3})   # süresi geçmiş
check("geçmiş soğuma engellemez", T.yt_cooldown_remaining() == 0,
      T.yt_cooldown_remaining())

import shutil                                                       # noqa: E402
shutil.rmtree(_TMPDIR, ignore_errors=True)
print("\n" + ("TÜM TESTLER GEÇTİ" if ok else "BAŞARISIZ"))
sys.exit(0 if ok else 1)
