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


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def _canonical_tur(raw):
    s = (raw or "").lower()
    for kw, canon in _TUR_CANON:
        if kw in s:
            return canon
    r = _norm(raw)
    return r if r else "Reklam"


def _norm_key(s):
    return _norm(s).casefold()


# "Pasif" türler: markanın sadece sürekli köşe logosu/arka plan olarak bulunması.
# active_only işaretli ana sponsorlarda bunlar reklam sayılmaz.
_PASSIVE_TURS = {"Köşe Banner", "Arka Plan", "Reklam"}


def _apply_alias(name, alias_map):
    """Öğrenilen yeniden adlandırma: kaynak adı kanonik ada çevirir."""
    if not alias_map:
        return name
    rule = alias_map.get(_norm_key(name))
    if rule and rule.get("to"):
        return rule["to"]
    return name


def compute_aggregates(detections, channel_logos, main_sponsors=None, active_only=None,
                       brand_aliases=None, ignored_brands=None):
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
    excluded_keys = logo_keys | ignored_keys  # ad sayımından düşenler
    total = len(detections)

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
        # Sadece markaya göre tekilleştir (konum gösterilmiyor).
        for nm in frame_brands:
            o = overlay_acc.setdefault(_norm_key(nm), {"marka": nm, "frames": set()})
            o["frames"].add(idx)

        if not d.get("reklam_var"):
            continue

        # Kanal logosu / yok-sayılan marka olmayan markalar = gerçek reklam markaları (#1)
        ad_brands = [nm for nm in frame_brands if _norm_key(nm) not in excluded_keys]
        ad_tespitler = [t for t in tespitler
                        if _norm_key(al(t.get("marka", ""))) not in excluded_keys]
        rep_turs = [_canonical_tur(t.get("tur", "")) for t in ad_tespitler]
        rep_tur = rep_turs[0] if rep_turs else "Reklam"

        # Her marka için bu karedeki (tür, konum) çiftleri — active_only filtresiyle
        frame_kept = {}   # marka_key -> {"nm":.., "pairs":[(tur,konum)]}
        for nm in ad_brands:
            bk = _norm_key(nm)
            matched = [t for t in ad_tespitler if _norm_key(al(t.get("marka", ""))) == bk]
            if matched:
                pairs = [(_canonical_tur(t.get("tur", "")), _norm(t.get("konum", ""))) for t in matched]
            else:
                konum0 = _norm(ad_tespitler[0].get("konum", "")) if ad_tespitler else ""
                pairs = [(rep_tur, konum0)]
            # active_only: pasif (köşe logosu/arka plan/genel) görünümleri ele
            if bk in active_only_keys:
                pairs = [(t, k) for (t, k) in pairs if t not in _PASSIVE_TURS]
            if pairs:
                frame_kept[bk] = {"nm": nm, "pairs": pairs}

        # Markasız ama aktif türde reklam sinyali (geçiş karesi vb.)
        brandless_active = any(
            not _norm(t.get("marka", "")) and _canonical_tur(t.get("tur", "")) not in _PASSIVE_TURS
            for t in ad_tespitler
        )
        generic_only = not markalar and not tespitler

        # Tüm sinyaller elendiyse (ör. yalnız köşe logosu olan ana sponsor) → sayma
        if not (frame_kept or brandless_active or generic_only):
            continue
        ad_frame_count += 1

        # type_counts: korunan marka çiftleri + markasız aktif türler
        for v in frame_kept.values():
            for tur, _k in v["pairs"]:
                type_counts[tur] = type_counts.get(tur, 0) + 1
        for t in ad_tespitler:
            if not _norm(t.get("marka", "")):
                tur = _canonical_tur(t.get("tur", ""))
                if tur not in _PASSIVE_TURS:
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
        if (ratio >= 0.80 and fc >= 3) or is_logo or is_sponsor:
            persistent.append({
                "marka": o["marka"],
                "frame_count": fc,
                "total_frames": total,
                "ratio": round(ratio, 3),
                "is_channel_logo": is_logo,
                "is_main_sponsor": is_sponsor,
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
        if o.get("ratio", 0) >= 0.80 and o.get("frame_count", 0) >= 3:
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
