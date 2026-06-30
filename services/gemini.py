import re
import json
import time
import threading
import requests
from config import gemini_url, GEMINI_MODEL, GEMINI_MODEL_LITE, GEMINI_RPM


class RateLimiter:
    """Dakika başı istek bütçesini sadece gerektiğinde uygular (sliding window)."""

    def __init__(self, rpm):
        self.gap = 60.0 / rpm if rpm and rpm > 0 else 0.0
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        if self.gap <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.gap:
                time.sleep(self.gap - delta)
            self._last = time.monotonic()


# Süreç genelinde paylaşılan limiter (free tier varsayılanı; env ile ayarlanır)
RATE_LIMITER = RateLimiter(GEMINI_RPM)


def gemini_call(api_key, payload, max_attempts=4, model=None, limiter=RATE_LIMITER):
    url = gemini_url(model)
    for attempt in range(max_attempts):
        if limiter is not None:
            limiter.wait()
        try:
            r = requests.post(
                f"{url}?key={api_key}", json=payload, timeout=90
            )
            if r.status_code == 200:
                return r.json(), None
            if r.status_code == 429:
                low = (r.text or "").lower()
                # GÜNLÜK kota (RPD) → beklemek fayda etmez (gece yarısı PT resetlenir).
                # Hızlı dön ki çağıran taramayı durdursun (saatlerce 429 bekleme yok).
                if "perday" in low or "per day" in low or "requests per day" in low:
                    return None, "QUOTA_DAILY"
                wait = 20 * (attempt + 1)   # dakika-başı (RPM) → kısa bekle, tekrar dene
                time.sleep(wait)
                continue
            if r.status_code in (500, 502, 503, 504):
                time.sleep(10 * (attempt + 1))
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

✅ REKLAM SAYILAN (bunlardan HERHANGİ BİRİ varsa reklam_var=true):
- YouTube pre-roll / mid-roll reklam ekranı (tam ekran reklam, atla butonu, sayaç)
- Reklam geçiş karesi (siyah, kırmızı, beyaz vs düz renk ekran — reklam arası)
- Görüntünün herhangi bir köşesinde / kenarında reklam overlay'i, banner
- Alt bant'ta marka logosu/sloganı/kampanya yazısı
- Sponsor bandı, indirim kodu, "tıkla"/"satın al"/"kod ile indirim" yazıları
- Bahis, oyun, bonus, hoşgeldin paketi reklamları
- Konuşmacının elinde/yanında kasıtlı tuttuğu markalı ürün
- Arka plan reklam panoları veya logo

🚫 REKLAM SAYMA:
- Kanalın kendi logosu (kanal logosu listesinde olanlar: {', '.join(channel_logos[:6]) if channel_logos else 'yok'})
- Program/yayın adı bandı
- Konuşmacı/misafir isim tagi
- Sosyal medya hesabı tagi

🔥 KRİTİK:
- Görüntünün SADECE BİR KÖŞE veya KENARI'nda bile reklam varsa tespit et
- Markayı NET okuyabiliyorsan YAZ, emin değilsen boş bırak
- Kategori: "Pre-Roll", "Mid-Roll", "Alt Bant", "Köşe Banner", "Sponsor Bandı", "Ürün Yerleştirme", "Geçiş Karesi", "Arka Plan"

YANIT — SADECE JSON:
{{
  "reklam_var": true/false,
  "guven": "Yüksek/Orta/Düşük",
  "markalar": ["NET marka adları, kanal logosu hariç"],
  "tespitler": [
    {{"tur": "Pre-Roll|Mid-Roll|Alt Bant|Köşe Banner|Sponsor Bandı|Ürün Yerleştirme|Geçiş Karesi|Arka Plan",
      "konum": "sağ üst|sol üst|sağ alt|sol alt|alt orta|üst orta|merkez|tam ekran",
      "marka": "marka adı", "detay": "kısa açıklama"}}
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


def _detection_rules(channel_logos):
    """Tek ve batch analizde paylaşılan ortak tespit kuralları."""
    logos = ', '.join(channel_logos[:6]) if channel_logos else 'yok'
    return f"""GÖREV: Her görüntüde HARİCİ REKLAM, SPONSOR veya MARKA YERLEŞTİRME var mı?

✅ REKLAM SAYILAN (bunlardan HERHANGİ BİRİ varsa reklam_var=true):
- YouTube pre-roll / mid-roll reklam ekranı (tam ekran reklam, atla butonu, sayaç)
- Reklam geçiş karesi (siyah, kırmızı, beyaz vs düz renk ekran — reklam arası)
- Görüntünün köşesinde/kenarında YAYINA EKLENMİŞ reklam overlay'i, banner
- Alt bant'ta marka logosu/sloganı/kampanya yazısı
- Sponsor bandı, indirim kodu, "tıkla"/"satın al"/"kod ile indirim" yazıları
- Bahis, oyun, bonus, hoşgeldin paketi reklamları
- ANİDEN BELİREN / hareketli / animasyonlu pop-up reklamlar
- Stüdyoda/masada/ekranda KASITLI YERLEŞTİRİLMİŞ sponsor logosu veya ürün
  (ürün yerleştirme) — konuşmacının elinde tuttuğu markalı ürün dahil
- Saha kenarı / LED reklam panoları, sahaya boyanmış reklam

🚫 REKLAM SAYMA:
- Kanalın kendi logosu (kanal logosu listesi: {logos})
- ⚽ OYUNCUNUN GİYDİĞİ formanın/şortun/çorabın üzerindeki SPONSOR markası
  (forma göğüs/kol sponsoru) — bu maç görüntüsünde sürekli ekranda olur, REKLAM
  SAYMA. (Yazman gerekiyorsa SADECE tespit olarak tur="Forma" ver, "markalar"
  dizisine EKLEME.)
- FUTBOL KULÜBÜ ARMASI / TAKIM LOGOSU (Fenerbahçe, Galatasaray, Bayern, Real
  Madrid…) — kulüp kimliği, REKLAM DEĞİL
- Lig / turnuva / federasyon logoları (UEFA, FIFA, TFF, Süper Lig, Şampiyonlar Ligi)
- Milli takım / ülke armaları, forma numarası/oyuncu adı
- Program/yayın adı bandı, sunucu/misafir isim tagi, sosyal medya tagi

⚽ KRİTİK AYRIM: GİYİLEN forma üzerindeki sponsor = HAYIR (sayma). Ama saha kenarı
LED panosu, yayına eklenen overlay/alt bant, tam ekran reklam, aniden çıkan
hareketli reklam, stüdyoda/masada yerleştirilmiş sponsor = EVET (yaz). Yani
"oyuncunun ÜZERİNDE giydiği" ≠ "sahneye/yayına YERLEŞTİRİLMİŞ".

🔥 KRİTİK:
- Görüntünün SADECE BİR KÖŞE veya KENARI'nda bile reklam varsa tespit et
- KANAL LOGOSU bir köşede olsa bile SAYMA — AMA aynı karede DİĞER bir köşede
  FARKLI bir marka/sponsor logosu varsa onu MUTLAKA ayrı tespit olarak YAZ
  (ör. sol üstte kanal logosu + sağ üstte sponsor markası → sadece sponsoru yaz)
- Her köşeyi ayrı değerlendir; bir köşedeki kanal logosu diğer köşedeki reklamı gölgelemesin
- Markayı NET okuyabiliyorsan YAZ, emin değilsen boş bırak
- HER tespit nesnesinde "marka" alanını doldur (o tespit hangi markaya aitse).
  Aynı karede 2 marka varsa 2 AYRI tespit nesnesi yaz, her birinde kendi markası.
- "tur" alanına SADECE ŞU LİSTEDEN TEK BİR kelime yaz — birden fazla yazma,
  eğik çizgi (/) kullanma, parantez/açıklama EKLEME:
  "Pre-Roll" | "Mid-Roll" | "Video Reklam" | "Alt Bant" | "Köşe Banner" |
  "Sponsor Bandı" | "Ürün Yerleştirme" | "Geçiş Karesi" | "Arka Plan" | "Forma"
  ("Forma" = oyuncunun giydiği forma sponsoru — reklam sayılmaz, sadece kayıt için)
- "markalar" dizisine karedeki TÜM reklam markalarını yaz — ANCAK kanal logosu,
  kulüp arması ve OYUNCU FORMASI sponsorunu bu diziye EKLEME"""


# Batch JSON çıktısının yapısal şeması — drift/parse hatalarını azaltır
_BATCH_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "frame": {"type": "INTEGER"},
            "reklam_var": {"type": "BOOLEAN"},
            "guven": {"type": "STRING"},
            "markalar": {"type": "ARRAY", "items": {"type": "STRING"}},
            "tespitler": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "tur": {"type": "STRING"},
                        "konum": {"type": "STRING"},
                        "marka": {"type": "STRING"},
                        "detay": {"type": "STRING"},
                    },
                },
            },
            "ozet": {"type": "STRING"},
        },
        "required": ["frame", "reklam_var"],
    },
}


def _empty_result(ozet="", skipped=False):
    r = {"reklam_var": False, "guven": "Düşük", "markalar": [],
         "tespitler": [], "ozet": ozet}
    if skipped:
        r["_skipped"] = True
    return r


def gemini_analyze_batch(api_key, frames, channel_logos, known_brands):
    """
    Birden fazla frame'i TEK Gemini çağrısında analiz eder.
    frames: [{"index": int, "timestamp": str, "b64": str}, ...]
    Döner: {index: result_dict} — eksik index'ler _skipped fallback alır.
    """
    if not frames:
        return {}

    ctx = ""
    if channel_logos:
        ctx += f"\n🚫 KANALIN KENDİ LOGOLARI (REKLAM SAYMA): {', '.join(channel_logos[:10])}"
    if known_brands:
        ctx += f"\n📌 Video açıklamasında geçen markalar: {', '.join(known_brands[:10])}"

    parts = [{"text": (
        f"Aşağıda bir YouTube videosundan {len(frames)} ayrı kare var.{ctx}\n\n"
        f"{_detection_rules(channel_logos)}\n\n"
        "⚠️ HER KAREYİ BAĞIMSIZ DEĞERLENDİR — bir karedeki markayı diğerine taşıma.\n"
        "Her kare etiketinden ('=== FRAME N ===') hemen sonra o karenin görüntüsü gelir."
    )}]

    for f in frames:
        parts.append({"text": f"\n=== FRAME {f['index']} | zaman {f['timestamp']} ==="})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": f["b64"]}})

    parts.append({"text": (
        "\n\nYANIT — SADECE JSON DİZİSİ (her kare için bir nesne, kendi 'frame' "
        "index'iyle):\n"
        '[{"frame": N, "reklam_var": true/false, "guven": "Yüksek/Orta/Düşük", '
        '"markalar": ["..."], "tespitler": [{"tur":"...","konum":"...",'
        '"marka":"...","detay":"..."}], "ozet": "tek cümle"}]'
    )})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": max(2000, 350 * len(frames)),
            "responseMimeType": "application/json",
            "responseSchema": _BATCH_SCHEMA,
        },
    }

    data, err = gemini_call(api_key, payload)
    indices = [f["index"] for f in frames]
    if err:
        return {i: _empty_result(err, skipped=True) for i in indices}

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        arr = json.loads(text)
        if not isinstance(arr, list):
            arr = [arr]
    except Exception:
        # Tek-nesne / bozuk JSON için güvenli parse, tüm frame'lere uygula
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            return {i: _empty_result("parse hatası", skipped=True) for i in indices}
        fallback = parse_json_safe(text)
        return {i: fallback for i in indices}

    by_index = {}
    for o in arr:
        if isinstance(o, dict) and "frame" in o:
            try:
                by_index[int(o["frame"])] = o
            except (TypeError, ValueError):
                pass

    out = {}
    for i in indices:
        o = by_index.get(i)
        if o is None:
            out[i] = _empty_result("[batch'te dönmedi]", skipped=True)
        else:
            out[i] = {
                "reklam_var": bool(o.get("reklam_var", False)),
                "guven": o.get("guven", "Düşük"),
                "markalar": o.get("markalar", []) or [],
                "tespitler": o.get("tespitler", []) or [],
                "ozet": o.get("ozet", ""),
            }
    return out


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
    data, err = gemini_call(api_key, payload, model=GEMINI_MODEL_LITE)
    if err:
        return []
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return parse_json_safe(text).get("markalar", []) or []
    except Exception:
        return []
