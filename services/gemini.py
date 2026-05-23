import re
import json
import time
import requests
from config import GEMINI_URL


def gemini_call(api_key, payload, max_attempts=3):
    for attempt in range(max_attempts):
        try:
            r = requests.post(
                f"{GEMINI_URL}?key={api_key}", json=payload, timeout=60
            )
            if r.status_code == 200:
                return r.json(), None
            if r.status_code == 429:
                time.sleep(30)
                continue
            if r.status_code in (500, 502, 503, 504):
                time.sleep(10)
                continue
            return None, f"API hata {r.status_code}: {r.text[:120]}"
        except Exception as e:
            if attempt < max_attempts - 1:
                time.sleep(5)
                continue
            return None, f"Bağlantı: {str(e)[:60]}"
    return None, "Sürekli yoğun"


def parse_json_safe(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    cleaned = text.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(1)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1:
        cand = cleaned[start:end + 1].replace("\n", " ")
        cand = re.sub(r",(\s*[}\]])", r"\1", cand)
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
    has_ad = bool(re.search(r'"reklam_var"\s*:\s*true', text, re.IGNORECASE))
    markalar = []
    mm = re.search(r'"markalar"\s*:\s*\[(.*?)\]', text, re.DOTALL)
    if mm:
        markalar = [m.strip() for m in re.findall(r'"([^"]+)"', mm.group(1))]
    return {
        "reklam_var": has_ad or bool(markalar),
        "guven": "Orta" if (has_ad or markalar) else "Düşük",
        "markalar": markalar,
        "tespitler": [],
        "ozet": "regex parse",
    }


def gemini_analyze_frame(api_key, image_b64, channel_logos, known_brands, timestamp):
    ctx = ""
    if channel_logos:
        ctx += f"\n\n🚫 KANALIN KENDİ LOGOLARI (BUNLARI REKLAM SAYMA): {', '.join(channel_logos[:10])}"
    if known_brands:
        ctx += f"\n📌 Video açıklamasında geçen markalar: {', '.join(known_brands[:10])}"

    prompt = f"""YouTube video frame'i — zaman: {timestamp}{ctx}

GÖREV: Bu görüntüde HARİCİ REKLAM, SPONSOR veya MARKA YERLEŞTİRME var mı?

✅ REKLAM SAYILAN:
- Alt bant'ta marka logosu/sloganı
- Köşelerde harici marka logosu (kanalın kendi logosu DEĞİL)
- Sponsor bandı, kampanya yazısı, indirim kodu
- URL, "kod ile indirimli", "tıkla", "satın al"
- Bahis, oyun, bonus, hoşgeldin paketi
- Konuşmacının kasıtlı tuttuğu marka/ürün
- Arka plan reklam panoları

🚫 REKLAM SAYMA:
- Kanalın kendi logosu (yukarıda listelenenler)
- Program adı bandı
- Konuşmacı/misafir isim tagları
- Sosyal medya hesabı tagleri

🔥 ÇOK ÖNEMLİ:
- Markayı NET okuyabiliyorsan YAZ. EMİN değilsen boş bırak, UYDURMA!
- Tespit kategorisi NET olsun: "Alt Bant", "Köşe Logo", "Sponsor Bandı", "Ürün Yerleştirme", "Arka Plan", "Banner"

YANIT — SADECE JSON:
{{
  "reklam_var": true/false,
  "guven": "Yüksek/Orta/Düşük",
  "markalar": ["NET marka adları, kanal logosu hariç"],
  "tespitler": [
    {{"tur": "Alt Bant|Köşe Logo|Sponsor Bandı|Ürün Yerleştirme|Banner|Arka Plan",
      "konum": "sağ üst|sol üst|sağ alt|sol alt|alt orta|üst orta|merkez",
      "marka": "marka", "detay": "kısa"}}
  ],
  "ozet": "tek cümle"
}}"""

    payload = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
            {"text": prompt},
        ]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 2000,
            "responseMimeType": "application/json",
        },
    }
    data, err = gemini_call(api_key, payload)
    if err:
        return {"reklam_var": False, "guven": "Düşük", "markalar": [],
                "tespitler": [], "ozet": err, "_skipped": True}
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return parse_json_safe(text)
    except Exception as e:
        return {"reklam_var": False, "guven": "Düşük", "markalar": [],
                "tespitler": [], "ozet": f"Hata: {str(e)[:60]}", "_skipped": True}


def gemini_extract_brands(api_key, title, description):
    text = f"Başlık: {title}\n\nAçıklama:\n{description[:3000]}"
    prompt = (
        "YouTube video metnindeki sponsor/reklam/marka isimlerini çıkar.\n\n"
        f"{text}\n\nSADECE JSON: {{\"markalar\": [\"...\"]}}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 400,
            "responseMimeType": "application/json",
        },
    }
    data, err = gemini_call(api_key, payload)
    if err:
        return []
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return parse_json_safe(text).get("markalar", []) or []
    except Exception:
        return []
