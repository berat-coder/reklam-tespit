"""
Günlük rapor regresyon testleri — süre modeli + tarih filtresi (AĞ YOK).

Neden kritik:
  • _brand_summary brand_counts (KARE başına sayan kolon) topluyordu; köşede
    yayın boyunca duran ana sponsor logosu her karede bir "reklam" sayıldığı
    için tek sponsorluk raporda 40+ reklam görünüyordu.
  • /api/daily-report get_daily_report()'u ARGÜMANSIZ çağırıyordu; days=1,
    days=30 ve day=1999-01-01 aynı veriyi döndürüyordu.

Çalıştırma:  ./.venv/bin/python tests/test_daily_report.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import database as db                                # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  → {extra}" if not cond else ""))
    if not cond:
        ok = False


# ── Sahte veri: aynı marka hem kalıcı sponsorluk hem spot ──────────────────
def _v(vid, day, exposure, counts, ch="ch1"):
    return {"id": vid, "channel_id": ch, "title": vid, "analyzed_at": f"{day}T09:00:00",
            "ad_frame_count": 40, "brand_counts": counts, "brand_exposure": exposure,
            "thumbnail": "", "channel_name": "Kanal"}


VIDEOS = [
    # Köşe logosu: 1 sponsorluk, 3600 sn — ama brand_counts 40 kare diyor
    _v("v1", "2026-08-31", {"Esperantos": {"sec": 3600, "app": 1, "kind": "sponsorluk", "prom": 0.6}},
       {"Esperantos": 40}),
    _v("v2", "2026-08-30", {"Bilyoner": {"sec": 60, "app": 3, "kind": "spot", "prom": 0.9}},
       {"Bilyoner": 12}),
    _v("v3", "2026-08-01", {"Migros": {"sec": 120, "app": 2, "kind": "spot", "prom": 0.5}},
       {"Migros": 30}),
    # süre modeli öncesi kayıt: brand_exposure işaretli boş
    _v("v4", "2026-08-31", {"_none": True}, {"Eski Marka": 99}),
]

db.get_all_videos = lambda completed_only=True: list(VIDEOS)
db.get_channel = lambda cid: {"name": "Kanal", "emv_rate": 0}
db._tr_date = lambda iso: (iso or "")[:10]      # sabit tarih (saat dilimi karıştırmasın)

print("\n[1] ŞİŞME BİTTİ Mİ? (kare değil, çıkış sayısı)")
s = db._brand_summary(VIDEOS)
esp = next((b for b in s["brands"] if b["marka"] == "Esperantos"), None)
check("Esperantos 1 çıkış (40 DEĞİL)", esp and esp["appearances"] == 1, esp)
check("Esperantos 3600 sn görünürlük", esp and esp["seconds"] == 3600, esp)
check("türü sponsorluk", esp and esp["kind"] == "sponsorluk", esp)
check("top_brands.count de çıkış sayısı",
      next(b["count"] for b in s["top_brands"] if b["marka"] == "Esperantos") == 1)
check("_none işareti marka sayılmadı",
      not any(b["marka"].startswith("_") for b in s["brands"]),
      [b["marka"] for b in s["brands"]])
check("süre modeli öncesi kare sayısı sızmadı",
      "Eski Marka" not in {b["marka"] for b in s["brands"]},
      [b["marka"] for b in s["brands"]])

print("\n[2] SPONSORLUK / SPOT AYRIMI")
check("1 kalıcı sponsorluk", [b["marka"] for b in s["sponsorships"]] == ["Esperantos"],
      s["sponsorships"])
check("2 spot", sorted(b["marka"] for b in s["spots"]) == ["Bilyoner", "Migros"], s["spots"])
check("pay toplamı ~100", abs(sum(b["sov_pct"] for b in s["brands"]) - 100) < 0.5,
      sum(b["sov_pct"] for b in s["brands"]))
check("süreye göre sıralı", [b["marka"] for b in s["brands"]][0] == "Esperantos")

print("\n[3] TARİH FİLTRESİ GERÇEKTEN ETKİLİ")
r_day = db.get_daily_report(day="2026-08-30")
check("belirli gün → 1 video", r_day["period_videos"] == 1, r_day["period_videos"])
check("o günün markası", [b["marka"] for b in r_day["today"]["brands"]] == ["Bilyoner"],
      r_day["today"]["brands"])
r_none = db.get_daily_report(day="1999-01-01")
check("boş gün → 0 video", r_none["period_videos"] == 0, r_none["period_videos"])
check("boş gün → 0 marka", r_none["today"]["brands"] == [], r_none["today"]["brands"])
check("aralık etiketi gösteriliyor", r_none["range_label"] == "1999-01-01", r_none)

r1, r30 = db.get_daily_report(days=1), db.get_daily_report(days=30)
check("days=1 ile days=30 AYNI DEĞİL",
      r1["period_videos"] != r30["period_videos"], (r1["period_videos"], r30["period_videos"]))
check("days=30 daha çok video kapsıyor", r30["period_videos"] >= r1["period_videos"],
      (r1["period_videos"], r30["period_videos"]))

print("\n[4] MARKA DETAYI: days + süre modeli")
b = db.get_brand_appearances("esperantos")            # büyük/küçük harf duyarsız
check("marka adı düzeltilerek döndü", b["marka"] == "Esperantos", b["marka"])
check("çıkış 1 (40 kare değil)", b["total"] == 1, b["total"])
check("süre 3600", b["seconds"] == 3600, b["seconds"])
check("tür sponsorluk", b["kind"] == "sponsorluk", b["kind"])
check("zaman çizgisinde süre var", b["timeline"] and b["timeline"][0]["seconds"] == 3600,
      b["timeline"])
b_old = db.get_brand_appearances("Eski Marka")
check("süre modeli öncesi kayıt kare sayısına düşüyor", b_old["total"] == 99, b_old["total"])

print("\n" + ("TÜM TESTLER GEÇTİ" if ok else "BAŞARISIZ"))
sys.exit(0 if ok else 1)
