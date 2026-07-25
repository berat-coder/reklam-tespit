"""
Tespitlerden video agregatlarını hesaplayan SAF fonksiyonlar (DB/I-O yok).
Hem ilk analiz (services/tasks.py) hem de manuel düzeltme sonrası yeniden
hesaplama (models/database.recompute_video_aggregates) AYNI mantığı kullanır ki
sayımlar asla sapmasın.
"""

import re

_GUVEN_RANK = {"yüksek": 3, "orta": 2, "düşük": 1}

# Model bazen tür alanına cümle/birleşik değer yazıyor; temiz kategoriye eşle.
# Sıra önemli — ilk eşleşen kazanır.
_TUR_CANON = [
    # Yerleşim: oyuncunun GİYDİĞİ forma sponsoru (varsayılan reklam sayılmaz).
    # NOT: "kit" tek başına çok geniş eşleşiyordu ("kitap", "kitle" → Forma);
    # yalnız tam sözcük olarak eşleşsin diye buradan çıkarıldı, aşağıda
    # _canonical_tur içinde sözcük sınırıyla ele alınıyor.
    ("forma", "Forma"), ("jersey", "Forma"), ("mayo", "Forma"),
    ("oyuncu üz", "Forma"),
    # Kulübün basın toplantısı panosu / medya duvarı / stadyum tabelası:
    # kulübün kendi sponsorları, YAYININ reklamı değil → sayılmaz.
    ("basın", "Basın Panosu"), ("backdrop", "Basın Panosu"),
    ("medya duvar", "Basın Panosu"), ("pano", "Basın Panosu"),
    # Pazaryeri/kargo/banka: reklamın sahibi değil, satış kanalı → sayılmaz.
    ("satış kanal", "Satış Kanalı"), ("pazaryeri", "Satış Kanalı"),
    ("pazar yeri", "Satış Kanalı"), ("marketplace", "Satış Kanalı"),
    ("pre-roll", "Pre-Roll"), ("pre roll", "Pre-Roll"),
    ("mid-roll", "Mid-Roll"), ("mid roll", "Mid-Roll"),
    ("video reklam", "Video Reklam"),
    ("alt bant", "Alt Bant"),
    ("sponsor", "Sponsor Bandı"),
    ("geçiş", "Geçiş Karesi"),
    ("köşe", "Köşe Banner"), ("banner", "Köşe Banner"), ("logo", "Köşe Banner"),
    ("ürün", "Ürün Yerleştirme"),
    ("arka plan", "Arka Plan"),
]

# Tam sözcük eşleşmesi gereken türler (substring çok geniş kaçıyor)
_TUR_WORD_CANON = [(re.compile(r"\bkit\b"), "Forma")]


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def _canonical_tur(raw):
    s = (raw or "").lower()
    for kw, canon in _TUR_CANON:
        if kw in s:
            return canon
    for rx, canon in _TUR_WORD_CANON:
        if rx.search(s):
            return canon
    r = _norm(raw)
    return r if r else "Reklam"


def _norm_key(s):
    return _norm(s).casefold()


_TR_FOLD = str.maketrans("çğıöşü", "cgiosu")


def _strict_key(s):
    """Katı karşılaştırma anahtarı: Türkçe katla + tüm işaret/boşlukları at.
    'Eski Açık' ↔ 'eskiacik', '343 Digital' ↔ '343digital' eşleşir."""
    return re.sub(r"[^a-z0-9]", "", _norm(s).casefold().translate(_TR_FOLD))


# Videoda BİR KEZ sayılan yerleşimler: masadaki ürün (ör. kahve bardağı) her
# karede görünür — 50-60 kez saymak veriyi şişirir. Marka başına 1 sayılır,
# diğer kareler kanıt olarak saklanır ama sayıma girmez.
_ONCE_PER_VIDEO_TURS = {"Ürün Yerleştirme"}


# Global spor-kulüp/lig ayıklama listesi (config'ten, 30s cache). Kulüp armaları
# (Fenerbahçe, Bayern, UEFA…) reklam sayılmasın diye. UI'dan düzenlenebilir.
_GLOBAL_IGNORE_CACHE = {"ts": 0.0, "keys": frozenset()}


def _global_ignore_keys():
    import time
    now = time.time()
    if now - _GLOBAL_IGNORE_CACHE["ts"] > 30:
        try:
            from config import load_config
            lst = load_config().get("global_ignored_brands") or []
        except Exception:
            lst = []
        _GLOBAL_IGNORE_CACHE["keys"] = frozenset(_norm_key(x) for x in lst if x)
        _GLOBAL_IGNORE_CACHE["ts"] = now
    return _GLOBAL_IGNORE_CACHE["keys"]


# Reklam SAYILMAYACAK yerleşimler (kanonik tür), config'ten 30s cache.
# Varsayılan {"Forma"} — oyuncunun giydiği forma sponsoru sayılmaz.
_EXCLUDED_PL_CACHE = {"ts": 0.0, "set": frozenset({"Forma"})}


def _excluded_placements():
    import time
    now = time.time()
    if now - _EXCLUDED_PL_CACHE["ts"] > 30:
        try:
            from config import load_config, DEFAULT_EXCLUDED_PLACEMENTS
            lst = load_config().get("excluded_placements")
            if lst is None:
                lst = DEFAULT_EXCLUDED_PLACEMENTS
        except Exception:
            lst = ["Forma"]
        _EXCLUDED_PL_CACHE["set"] = frozenset(_canonical_tur(x) for x in lst if x)
        _EXCLUDED_PL_CACHE["ts"] = now
    return _EXCLUDED_PL_CACHE["set"]


# "Pasif" türler: markanın sadece sürekli köşe logosu/arka plan olarak bulunması.
# active_only işaretli ana sponsorlarda bunlar reklam sayılmaz.
_PASSIVE_TURS = {"Köşe Banner", "Arka Plan", "Reklam"}


# Sayıma girmek için gereken en düşük güven (config'ten, 30s cache).
# Model "Düşük" güvenle yazdığı tahminler yanlış-pozitiflerin başlıca kaynağıydı
# ve hiçbir yerde elenmiyordu. Eşiğin altındaki kareler SAYILMAZ ama kanıt
# olarak görünmeye devam eder (kullanıcı elle onaylayabilir).
_MIN_CONF_CACHE = {"ts": 0.0, "rank": 2}


def _min_confidence_rank():
    import time
    now = time.time()
    if now - _MIN_CONF_CACHE["ts"] > 30:
        try:
            from config import load_config, DEFAULT_MIN_CONFIDENCE
            val = load_config().get("min_confidence") or DEFAULT_MIN_CONFIDENCE
        except Exception:
            val = "Orta"
        _MIN_CONF_CACHE["rank"] = _GUVEN_RANK.get(_norm_key(val), 2)
        _MIN_CONF_CACHE["ts"] = now
    return _MIN_CONF_CACHE["rank"]


def _apply_alias(name, alias_map):
    """Öğrenilen yeniden adlandırma: kaynak adı kanonik ada çevirir."""
    if not alias_map:
        return name
    rule = alias_map.get(_norm_key(name))
    if rule and rule.get("to"):
        return rule["to"]
    return name


def compute_aggregates(detections, channel_logos, main_sponsors=None, active_only=None,
                       brand_aliases=None, ignored_brands=None, channel_name="",
                       auto_main_sponsors=None):
    """
    detections: get_detections() biçiminde dict listesi.
    channel_logos: kanalın kendi logoları (reklam sayılmayacak).
    main_sponsors: ana sponsor olarak etiketlenen markalar (sayılmaya devam).
    active_only: ana sponsorlardan, sadece gerçek reklamları (alt bant/banner)
                 sayılacak olanlar — köşe logosu/arka plan görünümleri elenir.
    Döner: {ad_frame_count, type_counts, brand_counts,
            persistent_overlays, brand_report}
    Deterministik ve idempotent — aynı girdi hep aynı çıktı.
    """
    alias_map = brand_aliases or {}
    def al(n):
        return _apply_alias(n, alias_map)
    logo_keys = {_norm_key(l) for l in (channel_logos or []) if l}
    sponsor_keys = {_norm_key(s) for s in (main_sponsors or []) if s}
    active_only_keys = {_norm_key(s) for s in (active_only or []) if s}
    ignored_keys = {_norm_key(i) for i in (ignored_brands or []) if i}
    ignored_keys |= _global_ignore_keys()      # kulüp arması/lig/milli takım → reklam değil
    excluded_keys = logo_keys | ignored_keys  # ad sayımından düşenler
    excluded_pl = _excluded_placements()       # reklam sayılmayan yerleşimler (ör. Forma)
    # Kanalın KENDİ ADI reklam değildir ('Eski Açık', '343 Digital'...) — katı eşleşme
    channel_keys = set()
    if channel_name:
        ck = _strict_key(channel_name)
        if len(ck) >= 3:
            channel_keys.add(ck)
    auto_keys = {_norm_key(s) for s in (auto_main_sponsors or []) if s}
    once_counted = set()   # (marka_key, anahtar) — videoda bir kez sayılanlar
    total = len(detections)
    min_conf = _min_confidence_rank()   # eşiğin altındaki kareler sayılmaz

    type_counts = {}
    brand_counts = {}
    ad_frame_count = 0
    brand_acc = {}      # marka_key -> rapor birikteci
    overlay_acc = {}    # (marka_key, konum_key) -> {marka, konum, frames:set}

    for d in detections:
        idx = d.get("index")
        secs = d.get("seconds", 0.0) or 0.0
        ts = d.get("timestamp", "")
        guven = d.get("guven", "Düşük")
        tespitler = d.get("tespitler", []) or []
        markalar = d.get("markalar", []) or []

        # Markalar = markalar alanı (model bunu güvenilir doldurur) ∪ tespit markaları.
        # Model çoğu zaman tespitler[].marka'yı boş bırakıp markalar[]'ı doldurur,
        # bu yüzden ESAS kaynak markalar[] olmalı.
        frame_brands = []   # görüntü adıyla, casefold ile tekilleştirilmiş
        seen_b = set()
        for name in list(markalar) + [t.get("marka", "") for t in tespitler]:
            nm = _norm(al(name))   # öğrenilen alias uygulanır (varyantlar birleşir)
            if not nm:
                continue
            k = _norm_key(nm)
            if k in seen_b:
                continue
            seen_b.add(k)
            frame_brands.append(nm)

        # ── Kalıcı bindirme: TÜM kareler (reklam olmayanlar dahil), kanal logosu dahil ──
        for nm in frame_brands:
            o = overlay_acc.setdefault(_norm_key(nm), {"marka": nm, "frames": set()})
            o["frames"].add(idx)
        # Uzamsal tutarlılık: markanın ekran ÇEYREĞİ ('sağ üst' vb. — Gemini konum
        # alanı) sayılır → "hep aynı köşede" kalan logo otomatik ana-sponsor sinyali
        for t in tespitler:
            nm2 = _norm(al(t.get("marka", "")))
            kn = _norm(t.get("konum", "")).casefold()
            if nm2 and kn:
                o = overlay_acc.setdefault(_norm_key(nm2), {"marka": nm2, "frames": set()})
                kd = o.setdefault("konumlar", {})
                kd[kn] = kd.get(kn, 0) + 1

        if not d.get("reklam_var"):
            continue

        # ── Güven eşiği ──
        # Modelin "Düşük" güvenle yazdığı tahminler (okunamayan logo, karıştırılan
        # marka) sayıma girmez; kare yine de kanıt akışında görünür ve marka
        # kanıtlarına eklenir. Eşik Ayarlar'dan değiştirilebilir.
        low_conf = _GUVEN_RANK.get(_norm_key(guven), 1) < min_conf

        # Kanal logosu / yok-sayılan / kanal adı olmayan markalar = gerçek reklamlar
        def _is_excluded(name):
            return (_norm_key(name) in excluded_keys
                    or _strict_key(name) in channel_keys)
        ad_brands = [nm for nm in frame_brands if not _is_excluded(nm)]
        ad_tespitler = [t for t in tespitler
                        if not _is_excluded(al(t.get("marka", "")))]
        rep_turs = [_canonical_tur(t.get("tur", "")) for t in ad_tespitler]
        rep_tur = rep_turs[0] if rep_turs else "Reklam"

        # Her marka için bu karedeki (tür, konum) çiftleri — active_only filtresiyle
        frame_kept = {}    # marka_key -> {"nm":.., "pairs":[(tur,konum)]} (SAYILAN)
        repeat_only = {}   # tekrar eden ürün-yerleştirme: sayılmaz, kanıt olarak eklenir
        for nm in ad_brands:
            bk = _norm_key(nm)
            matched = [t for t in ad_tespitler if _norm_key(al(t.get("marka", ""))) == bk]
            if matched:
                pairs = [(_canonical_tur(t.get("tur", "")), _norm(t.get("konum", ""))) for t in matched]
            else:
                konum0 = _norm(ad_tespitler[0].get("konum", "")) if ad_tespitler else ""
                pairs = [(rep_tur, konum0)]
            # Yerleşim eleme: forma vb. dışlanan yerleşimler reklam sayılmaz
            pairs = [(t, k) for (t, k) in pairs if t not in excluded_pl]
            # ── Sayım baskılama (videoda BİR KEZ sayılanlar) ──
            #  • ANA SPONSORUN pasif görünümü (köşe logosu/arka plan): 55 karede
            #    görünse de = 1 sponsorluk. Spot reklamları (alt bant, tam ekran)
            #    normal sayılır → dinamik reklam metrikleri kirlenmez.
            #  • Ürün Yerleştirme: masadaki ürün her karede görünür → videoda 1.
            # İlk görünüm sayılır; sonrakiler 'repeats'e düşer (kanıt olarak kalır).
            counted, repeats = [], []
            if low_conf:
                # Düşük güven: hiçbiri sayılmaz, hepsi kanıta düşer
                repeats = list(pairs)
                pairs = []
            for (t, k) in pairs:
                if bk in active_only_keys and t in _PASSIVE_TURS:
                    okey = (bk, "__sponsor_passive__")
                elif t in _ONCE_PER_VIDEO_TURS:
                    okey = (bk, t)
                else:
                    okey = None
                if okey is not None:
                    if okey in once_counted:
                        repeats.append((t, k))
                        continue
                    once_counted.add(okey)
                counted.append((t, k))
            if counted:
                frame_kept[bk] = {"nm": nm, "pairs": counted}
            elif repeats:
                repeat_only[bk] = {"nm": nm, "pairs": repeats}

        # Tekrar eden yerleştirmelerin karesini KANITA ekle (sayım artmaz,
        # görünürlük aralığı — first/last — gerçek kalsın)
        for bk, info in repeat_only.items():
            b = brand_acc.get(bk)
            if b is None:
                continue
            fr = b["frames"].get(idx)
            if fr is None:
                fr = b["frames"][idx] = {
                    "index": idx, "timestamp": ts, "seconds": secs,
                    "frame_url": d.get("frame_url", ""), "guven": guven, "turler": [],
                }
            for tur, _k in info["pairs"]:
                if tur not in fr["turler"]:
                    fr["turler"].append(tur)
            if secs > b["last_seconds"]:
                b["last_seconds"], b["last_ts"] = secs, ts

        # Markasız ama aktif türde reklam sinyali (geçiş karesi vb.)
        brandless_active = (not low_conf) and any(
            not _norm(t.get("marka", ""))
            and _canonical_tur(t.get("tur", "")) not in _PASSIVE_TURS
            and _canonical_tur(t.get("tur", "")) not in excluded_pl
            for t in ad_tespitler
        )

        # NOT: eskiden 'generic_only' (model reklam_var=true deyip hiç marka/tespit
        # vermemesi) kareyi KOŞULSUZ saydırıyordu. Gerekçesiz "reklam var" iddiası
        # doğrulanamıyor ve şişkinliğin sessiz kaynağıydı → artık sayılmıyor.
        # Tüm sinyaller elendiyse (ör. yalnız köşe logosu olan ana sponsor) → sayma
        if not (frame_kept or brandless_active):
            continue
        ad_frame_count += 1

        # type_counts: korunan marka çiftleri + markasız aktif türler
        for v in frame_kept.values():
            for tur, _k in v["pairs"]:
                type_counts[tur] = type_counts.get(tur, 0) + 1
        for t in ad_tespitler:
            if not _norm(t.get("marka", "")):
                tur = _canonical_tur(t.get("tur", ""))
                if tur not in _PASSIVE_TURS and tur not in excluded_pl:
                    type_counts[tur] = type_counts.get(tur, 0) + 1

        # Marka raporu birikteci
        for bk, info in frame_kept.items():
            nm = info["nm"]
            b = brand_acc.get(bk)
            if b is None:
                b = brand_acc[bk] = {
                    "marka": nm, "appearances": 0, "frames": {},
                    "turler": {}, "konumlar": {},
                    "first_seconds": secs, "first_ts": ts,
                    "last_seconds": secs, "last_ts": ts,
                    "max_guven": guven,
                }
            for tur, konum in info["pairs"]:
                b["appearances"] += 1
                b["turler"][tur] = b["turler"].get(tur, 0) + 1
                if konum:
                    b["konumlar"][konum] = b["konumlar"].get(konum, 0) + 1
                brand_counts[b["marka"]] = brand_counts.get(b["marka"], 0) + 1
            fr = b["frames"].get(idx)
            if fr is None:
                fr = b["frames"][idx] = {
                    "index": idx, "timestamp": ts, "seconds": secs,
                    "frame_url": d.get("frame_url", ""), "guven": guven, "turler": [],
                }
            for tur, _k in info["pairs"]:
                if tur not in fr["turler"]:
                    fr["turler"].append(tur)
            if secs < b["first_seconds"]:
                b["first_seconds"], b["first_ts"] = secs, ts
            if secs > b["last_seconds"]:
                b["last_seconds"], b["last_ts"] = secs, ts
            if _GUVEN_RANK.get(guven.casefold(), 0) > _GUVEN_RANK.get(b["max_guven"].casefold(), 0):
                b["max_guven"] = guven

    # ── Kalıcı bindirme listesi ──
    # Eşik üstü olanlar + işaretli (kanal logosu / ana sponsor) olanlar her zaman
    # listelenir ki kullanıcı işareti geri alabilsin.
    persistent = []
    for o in overlay_acc.values():
        fc = len(o["frames"])
        ratio = fc / total if total else 0
        bk = _norm_key(o["marka"])
        is_logo = bk in logo_keys
        is_sponsor = bk in sponsor_keys
        # Uzamsal tutarlılık: baskın çeyrek + o çeyrekte kalma oranı
        kn_counts = o.get("konumlar", {})
        kn_total = sum(kn_counts.values())
        dom_kn, dom_n = ("", 0)
        if kn_counts:
            dom_kn, dom_n = max(kn_counts.items(), key=lambda x: x[1])
        consistency = (dom_n / kn_total) if kn_total else 0.0
        # OTOMATİK ana-sponsor adayı: (a) karelerin ≥%80'inde, VEYA
        # (b) karelerin ≥%40'ında + hep AYNI çeyrekte (≥%60 tutarlılık) — köşe
        # logosu deseni. Kanal logosu / zaten-sponsor hariç.
        auto_candidate = (not is_logo and not is_sponsor and fc >= 3 and (
            ratio >= 0.80 or
            (ratio >= 0.40 and kn_total >= 3 and consistency >= 0.60)))
        if auto_candidate or (ratio >= 0.80 and fc >= 3) or is_logo or is_sponsor:
            persistent.append({
                "marka": o["marka"],
                "frame_count": fc,
                "total_frames": total,
                "ratio": round(ratio, 3),
                "dominant_konum": dom_kn,
                "konum_consistency": round(consistency, 2),
                "auto_candidate": auto_candidate,
                "is_channel_logo": is_logo,
                "is_main_sponsor": is_sponsor,
                "is_auto_main_sponsor": bk in auto_keys,
                "is_active_only": bk in active_only_keys,
            })
    persistent.sort(key=lambda x: x["ratio"], reverse=True)

    # ── Marka raporu ──
    brand_report = []
    for b in brand_acc.values():
        tur_counts = b["turler"]
        frames = sorted(b["frames"].values(), key=lambda f: f["seconds"])
        brand_report.append({
            "marka": b["marka"],
            "appearances": b["appearances"],
            "frame_count": len(b["frames"]),
            "tur_counts": tur_counts,  # {tur: adet} — kırılım için
            "turler": sorted(tur_counts, key=lambda k: tur_counts[k], reverse=True),
            "konumlar": sorted(b["konumlar"], key=lambda k: b["konumlar"][k], reverse=True),
            "frames": frames,  # tıklayınca açılacak frame listesi
            "first_ts": b["first_ts"], "first_seconds": b["first_seconds"],
            "last_ts": b["last_ts"], "last_seconds": b["last_seconds"],
            "max_guven": b["max_guven"],
            "is_main_sponsor": _norm_key(b["marka"]) in sponsor_keys,
            "is_auto_main_sponsor": _norm_key(b["marka"]) in auto_keys,
            "is_active_only": _norm_key(b["marka"]) in active_only_keys,
        })
    # Ana sponsorlar en üstte, sonra görünüm sayısına göre
    brand_report.sort(key=lambda x: (not x["is_main_sponsor"], -x["appearances"]))

    return {
        "ad_frame_count": ad_frame_count,
        "type_counts": type_counts,
        "brand_counts": brand_counts,
        "persistent_overlays": persistent,
        "brand_report": brand_report,
    }


def auto_sponsor_candidates(agg, threshold, existing_sponsors=None):
    """compute_aggregates çıktısından otomatik ANA SPONSOR (+active_only) yapılacak
    markaları döndürür. İki sinyal:
      (a) tek videoda eşik üstü görünüm (şişik veri),
      (b) KALICI bindirme — ekranda sürekli (ratio≥0.80) = köşe logosu / title
          sponsor (ör. A101). Bunlar köşe logosu olarak süzülmeli, sadece gerçek
          reklamları (alt bant vb.) sayılmalı.
    Kanal logoları hariç. Döner: ['A101', ...]"""
    existing = {_norm_key(s) for s in (existing_sponsors or [])}
    out, seen = [], set()
    for m, c in (agg.get("brand_counts") or {}).items():
        k = _norm_key(m)
        if c >= threshold and k not in existing and k not in seen:
            out.append(m); seen.add(k)
    for o in (agg.get("persistent_overlays") or []):
        k = _norm_key(o.get("marka", ""))
        if o.get("is_channel_logo") or k in existing or k in seen:
            continue
        # Yeni sinyal: uzamsal-zamansal aday (≥%40 kare + aynı çeyrek) veya ≥%80
        if o.get("auto_candidate") or (o.get("ratio", 0) >= 0.80
                                       and o.get("frame_count", 0) >= 3):
            out.append(o["marka"]); seen.add(k)
    return out


def suggest_channel_logos(detections, channel_logos, analyzed):
    """Köşe/üst/alt konumda yüksek tekrarla görünen markaları kanal logosu adayı
    olarak önerir (eşik: max(3, frame*0.30)). Persist kararını çağıran verir."""
    logo_keys = {_norm_key(l) for l in (channel_logos or []) if l}
    counts = {}
    for d in detections:
        for t in d.get("tespitler", []) or []:
            marka = _norm(t.get("marka", ""))
            if not marka:
                continue
            tur = (t.get("tur", "") or "").lower()
            konum = (t.get("konum", "") or "").lower()
            if "köşe" in tur or "logo" in tur or "üst" in konum or "alt" in konum:
                counts[marka] = counts.get(marka, 0) + 1
    threshold = max(3, (analyzed or 0) * 0.30)
    return [m for m, c in counts.items()
            if c >= threshold and _norm_key(m) not in logo_keys]
