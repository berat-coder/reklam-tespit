"""
Sayım motoru regresyon testleri — AĞ YOK, Gemini çağrısı YOK.

Çalıştırma:  ./.venv/bin/python tests/test_aggregates.py

Kapsam:
  1. Yerleşim elemesi (Forma / Basın Panosu / Satış Kanalı / Ürün Markası)
  2. Güven eşiği (Düşük güven sayıma girmez, kanıtta kalır)
  3. Gerekçesiz "reklam var" iddiası sayılmaz
  4. Kanal adı elemesi, ürün yerleştirme videoda 1, spot sayımı
  5. Ana sponsorun pasif görünümü videoda 1 (kanıt kareleri korunur)
  6. Süre + olay modeli: kalıcı logo = 1 sponsorluk, ayrı çıkışlar = N olay
  7. Pay (SoV), belirginlik katsayısı, süreye göre sıralama
  8. Baskılanan sayım görünürlük süresini bozmaz
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.aggregates import compute_aggregates   # noqa: E402

IV = 8.0          # kareler arası aralık (sn)
ok = True


def check(name, cond, extra=""):
    global ok
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  → {extra}" if not cond else ""))
    if not cond:
        ok = False


def frame(i, markalar, tespitler, guven="Yüksek", reklam=True):
    return {"index": i, "seconds": i * IV, "timestamp": f"{int(i*IV)//60}:{int(i*IV)%60:02d}",
            "frame_url": f"/frames/v/frame_{i:04d}.jpg", "reklam_var": reklam,
            "guven": guven, "markalar": markalar, "tespitler": tespitler}


def corner(i, marka="Nesine"):
    return frame(i, [marka], [{"tur": "Köşe Banner", "konum": "sağ üst", "marka": marka}])


def band(i, marka):
    return frame(i, [marka], [{"tur": "Alt Bant", "konum": "alt orta", "marka": marka}])


print("\n[1] Sayılmayan yerleşimler: Forma / Basın Panosu / Satış Kanalı / Ürün Markası")
dets = [
    band(0, "Bilyoner"),
    frame(1, ["Beko"], [{"tur": "Forma", "konum": "merkez", "marka": "Beko"}]),
    frame(2, ["Sekerbank"], [{"tur": "Basın Panosu", "konum": "merkez", "marka": "Sekerbank"}]),
    frame(3, ["Esperantos", "Trendyol"], [
        {"tur": "Alt Bant", "konum": "alt orta", "marka": "Esperantos"},
        {"tur": "Satış Kanalı", "konum": "alt orta", "marka": "Trendyol"}]),
    frame(4, ["BOYNER", "SQUATWOLF"], [
        {"tur": "Video Reklam", "konum": "tam ekran", "marka": "BOYNER"},
        {"tur": "Ürün Markası", "konum": "merkez", "marka": "SQUATWOLF"}]),
]
a = compute_aggregates(dets, [], channel_name="")
bc = a["brand_counts"]
check("Bilyoner sayıldı", bc.get("Bilyoner") == 1, bc)
check("Esperantos (asıl reklamveren) sayıldı", bc.get("Esperantos") == 1, bc)
check("BOYNER (reklamveren) sayıldı", bc.get("BOYNER") == 1, bc)
check("Beko (forma) sayılmadı", "Beko" not in bc, bc)
check("Sekerbank (basın panosu) sayılmadı", "Sekerbank" not in bc, bc)
check("Trendyol (satış kanalı) sayılmadı", "Trendyol" not in bc, bc)
check("SQUATWOLF (ürün markası) sayılmadı", "SQUATWOLF" not in bc, bc)

print("\n[2] Güven eşiği: Düşük sayıma girmez")
dets2 = [
    band(0, "Nesine"),
    frame(1, ["Papara"], [{"tur": "Alt Bant", "konum": "alt orta", "marka": "Papara"}],
          guven="Düşük"),
    frame(2, ["Nesine"], [{"tur": "Alt Bant", "konum": "alt orta", "marka": "Nesine"}],
          guven="Orta"),
]
a2 = compute_aggregates(dets2, [], channel_name="")
check("Yüksek+Orta sayıldı (Nesine=2)", a2["brand_counts"].get("Nesine") == 2, a2["brand_counts"])
check("Düşük güven sayılmadı", "Papara" not in a2["brand_counts"], a2["brand_counts"])

print("\n[3] Gerekçesiz 'reklam var' iddiası sayılmaz")
a3 = compute_aggregates([frame(0, [], [])], [], channel_name="")
check("marka/tespit yoksa kare sayılmadı", a3["ad_frame_count"] == 0, a3["ad_frame_count"])

print("\n[4] Kanal adı elemesi + ürün yerleştirme videoda 1 + spot")
dets4 = [frame(i, ["Starbucks"],
               [{"tur": "Ürün Yerleştirme", "konum": "merkez", "marka": "Starbucks"}])
         for i in range(6)]
dets4 += [band(10, "NEO Spor"), band(11, "Bilyoner"), band(12, "Bilyoner")]
a4 = compute_aggregates(dets4, [], channel_name="NEO Spor")
b4 = a4["brand_counts"]
check("Starbucks videoda 1 kez", b4.get("Starbucks") == 1, b4)
check("kanal adı elendi", "NEO Spor" not in b4, b4)
check("Bilyoner 2 spot", b4.get("Bilyoner") == 2, b4)

print("\n[5] Ana sponsorun pasif görünümü videoda 1, kanıt korunur")
dets5 = [corner(i) for i in range(20)] + [band(21, "Nesine")]
a5 = compute_aggregates(dets5, [], main_sponsors=["Nesine"], active_only=["Nesine"],
                        channel_name="")
check("20 köşe + 1 bant → 2 sayım", a5["brand_counts"].get("Nesine") == 2, a5["brand_counts"])
br5 = {b["marka"]: b for b in a5["brand_report"]}
check("kanıt kareleri korundu (21)", br5["Nesine"]["frame_count"] == 21,
      br5["Nesine"]["frame_count"])

print("\n[6] Süre + olay: kalıcı logo = 1 sponsorluk")
a6 = compute_aggregates([corner(i) for i in range(55)], [], channel_name="")
b6 = {b["marka"]: b for b in a6["brand_report"]}["Nesine"]
check("kind = sponsorluk", b6["kind"] == "sponsorluk", b6["kind"])
check("çıkış = 1 (55 değil)", b6["appearance_count"] == 1, b6["appearance_count"])
check("süre ≈ 55×8 = 440sn", abs(b6["exposure_seconds"] - 440) < 1, b6["exposure_seconds"])
check("etiket 7:20", b6["exposure_label"] == "7:20", b6["exposure_label"])
check("özet 1 sponsorluk", a6["exposure_summary"]["sponsorship_count"] == 1,
      a6["exposure_summary"])

print("\n[7] Ayrı zamanlarda çıkan alt bant = 2 olay (sponsorluk DEĞİL)")
a7 = compute_aggregates([band(i, "Bilyoner") for i in (0, 1, 2, 30, 31)], [], channel_name="")
b7 = {b["marka"]: b for b in a7["brand_report"]}["Bilyoner"]
check("kind = spot", b7["kind"] == "spot", b7["kind"])
check("çıkış = 2", b7["appearance_count"] == 2, b7["appearance_count"])
check("süre = 5×8 = 40sn", abs(b7["exposure_seconds"] - 40) < 1, b7["exposure_seconds"])

print("\n[8] Pay (SoV), belirginlik, sıralama, baskılanan sayım süreyi bozmaz")
dets8 = [corner(i, "Nesine") for i in range(40)] + [band(i, "Papara") for i in (10, 11)]
a8 = compute_aggregates(dets8, [], channel_name="")
r8 = {b["marka"]: b for b in a8["brand_report"]}
check("Nesine payı > %90", r8["Nesine"]["sov_pct"] > 90, r8["Nesine"]["sov_pct"])
check("paylar ~%100", 99 <= round(r8["Nesine"]["sov_pct"] + r8["Papara"]["sov_pct"]) <= 101,
      (r8["Nesine"]["sov_pct"], r8["Papara"]["sov_pct"]))
check("köşe banner belirginlik 0.35", r8["Nesine"]["prominence"] == 0.35,
      r8["Nesine"]["prominence"])
check("alt bant belirginlik 0.6", r8["Papara"]["prominence"] == 0.6, r8["Papara"]["prominence"])
check("en uzun süre en üstte", a8["brand_report"][0]["marka"] == "Nesine",
      [b["marka"] for b in a8["brand_report"]])

a9 = compute_aggregates([corner(i, "A101") for i in range(40)], [],
                        main_sponsors=["A101"], active_only=["A101"], channel_name="")
r9 = {b["marka"]: b for b in a9["brand_report"]}["A101"]
check("sayım baskılandı (1)", r9["appearances"] == 1, r9["appearances"])
check("ama görünürlük tam (320sn)", abs(r9["exposure_seconds"] - 320) < 1,
      r9["exposure_seconds"])
check("kanıt kareleri tam (40)", r9["frame_count"] == 40, r9["frame_count"])

print("\n" + ("TÜM TESTLER GEÇTİ" if ok else "BAŞARISIZ"))
sys.exit(0 if ok else 1)
