"""
Gemini yanıt ayrıştırma regresyon testleri — AĞ YOK (gemini_call taklit edilir).

Çalıştırma:  ./.venv/bin/python tests/test_gemini_parse.py

Neden kritik: bu iki hata yanlış-pozitiflerin başlıca kaynağıydı —
  • bozuk JSON'da TEK sonuç 12 karenin hepsine kopyalanıyordu
  • regex kurtarması "markalar" doluysa reklam_var'ı ZORLA true yapıyordu
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import gemini                      # noqa: E402
from services.gemini import parse_json_safe      # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  → {extra}" if not cond else ""))
    if not cond:
        ok = False


print("\n[1] reklam_var UYDURULMAMALI")
r = parse_json_safe('kırık ... "markalar": ["Fenerbahçe", "Trendyol"] ...')
check("markalar dolu ama reklam_var yok → False", r["reklam_var"] is False, r)
check("markalar kanıt olarak korunuyor", r["markalar"] == ["Fenerbahçe", "Trendyol"], r)
check("regex kurtarması → guven Düşük", r["guven"] == "Düşük", r)
r2 = parse_json_safe('bozuk "reklam_var": true ... "markalar": ["Bilyoner"] ...')
check("açıkça reklam_var:true → True", r2["reklam_var"] is True, r2)

print("\n[2] DİZİ (batch) kurtarması")
arr = parse_json_safe('```json\n[{"frame":3,"reklam_var":true},{"frame":4,"reklam_var":false}]\n```')
check("```json içindeki dizi parse edildi", isinstance(arr, list) and len(arr) == 2, arr)
arr2 = parse_json_safe('bla [{"frame":7,"reklam_var":true},] son')
check("trailing virgüllü dizi kurtarıldı", isinstance(arr2, list) and arr2[0]["frame"] == 7, arr2)
# İç dizi ("markalar": [...]) yanıtın tamamı sanılmamalı
inner = parse_json_safe('{"reklam_var": false, "markalar": ["A","B"]}xx')
check("iç dizi yanıt sanılmadı (nesne döndü)", isinstance(inner, dict), inner)

print("\n[3] Bozuk yanıt 12 kareye KOPYALANMAMALI")
frames = [{"index": i, "b64": "x", "timestamp": f"00:{i:02d}"} for i in range(12)]


def fake_broken(api_key, payload, **kw):
    return {"candidates": [{"content": {"parts": [
        {"text": '{"reklam_var": true, "markalar": ["Nesine"]}'}]}}]}, None


gemini.gemini_call = fake_broken
out = gemini.gemini_analyze_batch("k", frames, [], [])
counted = [i for i, v in out.items() if v.get("reklam_var")]
check("hiçbir kare reklam sayılmadı", counted == [], f"reklam sayılan: {counted}")
check("hepsi skipped işaretli", all(v.get("_skipped") for v in out.values()),
      [v.get("_skipped") for v in out.values()])

print("\n[4] Geçerli dizi yanıtı doğru kareye eşlenmeli")


def fake_ok(api_key, payload, **kw):
    body = json.dumps([
        {"frame": 2, "reklam_var": True, "guven": "Yüksek", "markalar": ["Bilyoner"],
         "tespitler": [{"tur": "Alt Bant", "konum": "alt orta", "marka": "Bilyoner"}]},
        {"frame": 5, "reklam_var": False, "guven": "Orta"},
    ])
    return {"candidates": [{"content": {"parts": [{"text": body}]}}]}, None


gemini.gemini_call = fake_ok
out = gemini.gemini_analyze_batch("k", frames, [], [])
check("frame 2 reklam", out[2]["reklam_var"] is True, out[2])
check("frame 2 markası doğru", out[2]["markalar"] == ["Bilyoner"], out[2])
check("frame 5 temiz", out[5]["reklam_var"] is False, out[5])
check("dönmeyen kare skipped", out[0].get("_skipped") is True, out[0])
check("dönmeyen kare reklam değil", out[7]["reklam_var"] is False, out[7])

print("\n[5] Prompt'ta KANAL BAZLI sponsor bilgisi OLMAMALI")
rules = gemini._detection_rules(["NEO Spor"])
check("'ANA SPONSOR' geçmiyor", "ANA SPONSOR" not in rules.upper(),
      "prompt kanal sponsorunu söylüyor → onay yanlılığı")
check("kanal-bağımsız kalıcı logo kuralı var", "KALICI LOGO" in rules)
check("'her yayının sponsoru farklı' notu var", "her yayının sponsoru farklı" in rules)
check("şemada tur/konum/marka zorunlu",
      gemini._BATCH_SCHEMA["items"]["properties"]["tespitler"]["items"]["required"]
      == ["tur", "konum", "marka"])

print("\n" + ("TÜM TESTLER GEÇTİ" if ok else "BAŞARISIZ"))
sys.exit(0 if ok else 1)
