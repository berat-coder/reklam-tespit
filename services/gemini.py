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
    # Batch yanıtı DİZİ döner; eski desen yalnız {...} yakalıyordu → dizi yanıtlar
    # kurtarılamayıp tüm batch çöpe gidiyordu. Artık [...] de yakalanır.
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*([\[{].*[\]}])\s*```", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(1)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Önce dizi, sonra nesne olarak kurtarmayı dene (trailing virgül temizliğiyle).
    # Dizi kurtarmasında DİKKAT: metindeki ilk '[' çoğu zaman iç içe bir alan
    # ("markalar": [...]) olabilir; onu yanıtın tamamı sanmamak için sonucun
    # nesne listesi olmasını şart koşuyoruz.
    for opener, closer in (("[", "]"), ("{", "}")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end != -1 and end > start:
            cand = cleaned[start:end + 1].replace("\n", " ")
            cand = re.sub(r",(\s*[}\]])", r"\1", cand)
            try:
                parsed = json.loads(cand)
            except json.JSONDecodeError:
                continue
            if opener == "[" and not (
                    isinstance(parsed, list)
                    and any(isinstance(x, dict) for x in parsed)):
                continue        # iç dizi yakalanmış → nesne kurtarmasına geç
            return parsed
    has_ad = bool(re.search(r'"reklam_var"\s*:\s*true', text, re.IGNORECASE))
    markalar = []
    mm = re.search(r'"markalar"\s*:\s*\[(.*?)\]', text, re.DOTALL)
    if mm:
        markalar = [m.strip() for m in re.findall(r'"([^"]+)"', mm.group(1))]
    return {
        # reklam_var'ı marka listesinden TÜRETME. Bozuk yanıtta "markalar" dolu
        # diye kareyi reklam saymak başlıca yanlış-pozitif kaynağıydı: model
        # kulüp armasını/forma sponsorunu yazdığında da kare reklam oluyordu.
        # Yalnız açıkça "reklam_var": true yazıyorsa true.
        "reklam_var": has_ad,
        # Regex kurtarması belirsizdir → düşük güven (güven eşiğiyle sayım dışı).
        "guven": "Düşük",
        "markalar": markalar,
        "tespitler": [],
        "ozet": "regex parse",
    }


def _detection_rules(channel_logos):
    """Tek ve batch analizde paylaşılan ortak tespit kuralları."""
    logos = ', '.join(channel_logos[:10]) if channel_logos else 'yok'
    tur_list = " | ".join(f'"{t}"' for t in TUR_VALUES)
    konum_list = " | ".join(f'"{k}"' for k in KONUM_VALUES)
    return f"""GÖREV: Her görüntüde HARİCİ REKLAM, SPONSOR veya MARKA YERLEŞTİRME var mı?

✅ REKLAM SAYILAN (bunlardan HERHANGİ BİRİ varsa reklam_var=true):
- YouTube pre-roll / mid-roll reklam ekranı (tam ekran reklam, atla butonu, sayaç)
- Görüntünün köşesinde/kenarında YAYINA EKLENMİŞ reklam overlay'i, banner
- Alt bant'ta marka logosu/sloganı/kampanya yazısı
- Sponsor bandı, indirim kodu, "tıkla"/"satın al"/"kod ile indirim" yazıları
- Bahis, oyun, bonus, hoşgeldin paketi reklamları
- ANİDEN BELİREN / hareketli / animasyonlu pop-up reklamlar
- Stüdyoda/masada/ekranda KASITLI YERLEŞTİRİLMİŞ sponsor logosu veya ürün
  (ürün yerleştirme) — konuşmacının elinde tuttuğu markalı ürün dahil.
  Masadaki ürün (ör. kahve bardağı) her karede görünse bile tur her zaman
  "Ürün Yerleştirme" olmalı — "Alt Bant"/"Köşe Banner" ile KARIŞTIRMA.
- KANALIN KENDİ STÜDYO DEKORUNDAKİ sponsor logosu (stüdyo arkasındaki ekran/
  duvar/masa önü panelinde duran marka) → EVET, bu programın sponsorudur.
  tur="Köşe Banner" ya da "Sponsor Bandı" ver.
- Saha kenarı / LED reklam panoları, sahaya boyanmış reklam

🚫 REKLAM SAYMA:
- KANALIN KENDİ ADI ve logosu (bu liste: {logos}) — kanal adının yazılı/logolu
  her görünümü kanal kimliğidir, ASLA reklam veya marka olarak YAZMA
- ⚽ OYUNCUNUN GİYDİĞİ formanın/şortun/çorabın üzerindeki SPONSOR markası
  (forma göğüs/kol sponsoru) — bu maç görüntüsünde sürekli ekranda olur, REKLAM
  SAYMA. (Yazman gerekiyorsa SADECE tespit olarak tur="Forma" ver, "markalar"
  dizisine EKLEME.)
- 🎤 BASIN TOPLANTISI PANOSU / KULÜP MEDYA DUVARI: teknik direktör veya
  futbolcu açıklama yaparken ARKASINDAKİ panoda (backdrop) kulübün kendi
  sponsorları tekrar tekrar basılıdır. Bu KULÜBÜN panosudur, bu YAYININ reklamı
  DEĞİLDİR → REKLAM SAYMA. Aynı şekilde stadyum/tesis tabelaları, kulüp
  basın odası duvarı, devre arası röportaj panosu.
  (Yazman gerekiyorsa SADECE tespit olarak tur="Basın Panosu" ver, "markalar"
  dizisine EKLEME.)
- 🛒 SATIŞ KANALI / PAZARYERİ: bir ürün reklamında "Trendyol'da", "Hepsiburada",
  "Amazon", kargo veya banka logosu geçiyorsa ASIL REKLAMVEREN ürünün markasıdır
  (ör. Esperantos kahve reklamı → reklamveren "Esperantos", Trendyol değil).
  Pazaryerini "markalar" dizisine EKLEME; istersen ayrı tespit olarak
  tur="Satış Kanalı" ver. Pazaryeri ANCAK kendi kurumsal reklamını yapıyorsa
  (kendi kampanyası, kendi sloganı) gerçek reklamveren sayılır.
- FUTBOL KULÜBÜ ARMASI / TAKIM LOGOSU (Fenerbahçe, Galatasaray, Bayern, Real
  Madrid…) — kulüp kimliği, REKLAM DEĞİL
- Lig / turnuva / federasyon logoları (UEFA, FIFA, TFF, Süper Lig, Şampiyonlar Ligi)
- Milli takım / ülke armaları, forma numarası/oyuncu adı
- Program/yayın adı bandı, sunucu/misafir isim tagi, sosyal medya tagi
- Düz renkli (siyah/beyaz) boş kare TEK BAŞINA reklam değildir; yalnız üzerinde
  reklam öğesi (marka, slogan, sayaç) varsa "Geçiş Karesi" yaz.

⚽ KRİTİK AYRIM — "kim yerleştirdi?" diye sor:
- Oyuncunun ÜZERİNDE giydiği sponsor → HAYIR
- KULÜBÜN basın panosu / stadyum tabelası → HAYIR (kulübün sponsoru, yayının değil)
- KANALIN yayınına eklediği overlay/alt bant/tam ekran → EVET
- KANALIN stüdyo dekorundaki sponsor → EVET
- Saha kenarı LED panosu → EVET

🔥 KRİTİK:
- Görüntünün SADECE BİR KÖŞE veya KENARI'nda bile reklam varsa tespit et
- KANAL LOGOSU bir köşede olsa bile SAYMA — AMA aynı karede DİĞER bir köşede
  FARKLI bir marka/sponsor logosu varsa onu MUTLAKA ayrı tespit olarak YAZ
  (ör. sol üstte kanal logosu + sağ üstte sponsor markası → sadece sponsoru yaz)
- Her köşeyi ayrı değerlendir; bir köşedeki kanal logosu diğer köşedeki reklamı gölgelemesin
- Markayı NET okuyabiliyorsan YAZ, emin değilsen boş bırak
- EMİN DEĞİLSEN guven="Düşük" ver. Tahmin yürütme; okuyamadığın bir logoyu
  "olabilir" diye yazma. Yanlış marka yazmak, hiç yazmamaktan kötüdür.
- HER tespit nesnesinde "marka" alanını doldur (o tespit hangi markaya aitse).
  Aynı karede 2 marka varsa 2 AYRI tespit nesnesi yaz, her birinde kendi markası.
- "tur" alanına SADECE ŞU LİSTEDEN TEK BİR kelime yaz — birden fazla yazma,
  eğik çizgi (/) kullanma, parantez/açıklama EKLEME:
  {tur_list}
  ("Forma" = giyilen forma sponsoru, "Basın Panosu" = kulüp backdrop'u,
   "Satış Kanalı" = pazaryeri/kargo — bu üçü reklam sayılmaz, sadece kayıt için)
- "konum" alanına SADECE ŞU LİSTEDEN TEK BİR değer yaz: {konum_list}
- "markalar" dizisine karedeki TÜM reklam markalarını yaz — ANCAK kanal logosu,
  kulüp arması, OYUNCU FORMASI sponsoru, BASIN PANOSU markaları ve SATIŞ KANALI
  (pazaryeri) markalarını bu diziye EKLEME"""


# Tespit türleri ve ekran konumları — hem prompt'ta hem şemada AYNI liste
# kullanılır. Şemadaki enum sayesinde model serbest metin uyduramaz; böylece
# aggregates._canonical_tur substring tahminine bel bağlamaz.
TUR_VALUES = [
    "Pre-Roll", "Mid-Roll", "Video Reklam", "Alt Bant", "Köşe Banner",
    "Sponsor Bandı", "Ürün Yerleştirme", "Geçiş Karesi", "Arka Plan",
    "Forma", "Basın Panosu", "Satış Kanalı",
]
KONUM_VALUES = [
    "sol üst", "sağ üst", "sol alt", "sağ alt",
    "üst orta", "alt orta", "merkez", "tam ekran",
]
GUVEN_VALUES = ["Yüksek", "Orta", "Düşük"]


# Batch JSON çıktısının yapısal şeması — drift/parse hatalarını azaltır
_BATCH_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "frame": {"type": "INTEGER"},
            "reklam_var": {"type": "BOOLEAN"},
            "guven": {"type": "STRING", "enum": GUVEN_VALUES},
            "markalar": {"type": "ARRAY", "items": {"type": "STRING"}},
            "tespitler": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "tur": {"type": "STRING", "enum": TUR_VALUES},
                        "konum": {"type": "STRING", "enum": KONUM_VALUES},
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


def gemini_analyze_batch(api_key, frames, channel_logos, known_brands,
                         main_sponsors=None):
    """
    Birden fazla frame'i TEK Gemini çağrısında analiz eder.
    frames: [{"index": int, "timestamp": str, "b64": str}, ...]
    main_sponsors: kanalın bilinen ana sponsorları → prompt'a bağlam enjekte
    edilir (kalıcı logo ↔ spot reklam ayrımı netleşir).
    Döner: {index: result_dict} — eksik index'ler _skipped fallback alır.
    """
    if not frames:
        return {}

    ctx = ""
    if channel_logos:
        ctx += f"\n🚫 KANALIN KENDİ LOGOLARI (REKLAM SAYMA): {', '.join(channel_logos[:10])}"
    if main_sponsors:
        ctx += (f"\n🏆 BAĞLAM: Bu kanalın RESMİ ANA SPONSORU: "
                f"{', '.join(main_sponsors[:5])}. Bu markanın logosu yayın boyunca "
                f"ekranda SÜREKLİ durur. Gördüğünde konumunu yaz ama türünü doğru "
                f"ver: sabit köşe logosu ise tur='Köşe Banner'; SADECE kısa süreli "
                f"spot reklamı (alt bant, tam ekran, ürün tanıtımı) ise o türü yaz. "
                f"Kalıcı logoyu spot reklamla KARIŞTIRMA.")
    if known_brands:
        # Bu liste yalnız İPUCU. Priming riski var: model listedeki markayı
        # görmediği karede de yazabiliyordu → açık uyarı ekleniyor.
        ctx += (f"\n📌 Video açıklamasında geçen markalar (YALNIZ İPUCU): "
                f"{', '.join(known_brands[:10])} — bunları SADECE gerçekten "
                f"GÖRDÜĞÜN karede yaz, listede geçiyor diye yazma.")

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
        # Bozuk JSON → güvenli parse. DİKKAT: sonucu tüm frame'lere KOPYALAMA.
        # Eskiden tek bir sonuç 12 karenin hepsine yazılıyordu; bir karede
        # görülen marka 12 kareye yayılıp sayımı şişiriyor ve yanlış-pozitif
        # üretiyordu. Kurtarılan yanıt dizi ise frame alanına göre eşleştirilir,
        # tek nesne ise yalnız kendi frame'ine uygulanır; eşleşmeyenler
        # 'skipped' işaretlenir (sayıma girmez).
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            return {i: _empty_result("parse hatası", skipped=True) for i in indices}
        recovered = parse_json_safe(text)
        arr = recovered if isinstance(recovered, list) else [recovered]

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
