"""
Frame (kare) depolama yönetimi — kalıcı volume üzerinde.

İki strateji:
  • keep_only_evidence_frames: analiz bitince sadece reklam (kanıt) karelerini
    tut, temiz kareleri sil → çok az yer kaplar.
  • prune_frames: toplam frame boyutu cap'i aşarsa en eski video klasörlerini
    sil → volume asla dolmaz (backstop).
"""

import os
import shutil
from config import FRAMES_DIR


def _dir_size(path):
    total = 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except Exception:
        pass
    return total


def keep_only_evidence_frames(video_id, keep_filenames):
    """video_id klasöründe yalnız `keep_filenames` (kanıt kareleri) kalsın;
    gerisini sil. Döner: silinen dosya sayısı."""
    d = FRAMES_DIR / video_id
    if not d.exists():
        return 0
    keep = set(keep_filenames or [])
    removed = 0
    try:
        for p in d.glob("frame_*.jpg"):
            if p.name not in keep:
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        # Klasör tamamen boşaldıysa (hiç kanıt yok) kaldır
        if not any(d.iterdir()):
            d.rmdir()
    except Exception as e:
        print(f"[FRAME] kanıt temizleme hatası ({video_id}): {e}")
    return removed


def prune_frames_by_age(days):
    """Analizden `days` günden eski videoların KARELERİNİ (görsellerini) sil →
    yer açılır. Rapor/veri (DB) ETKİLENMEZ. Klasör mtime'ı analiz zamanıdır.
    Döner: silinen video klasörü sayısı."""
    days = int(days)
    if days <= 0:
        return 0
    import time
    cutoff = time.time() - days * 86400
    removed = 0
    try:
        if not FRAMES_DIR.exists():
            return 0
        for p in FRAMES_DIR.iterdir():
            try:
                if p.is_dir() and p.stat().st_mtime < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
    except Exception:
        return removed
    if removed:
        print(f"[FRAME] {removed} eski video klasörü silindi (>{days} gün) — rapor korundu")
    return removed


def frame_maintenance():
    """Tek giriş noktası: (1) yaşa göre eski kareleri sil (retention açıksa),
    (2) boyut cap'i aşılırsa en eski klasörleri buda. config'ten okur."""
    try:
        from config import load_config, FRAME_STORAGE_CAP_MB
        cfg = load_config()
        fr = cfg.get("frame_retention") or {}
        if fr.get("enabled", True):
            prune_frames_by_age(int(fr.get("days", 2)))
        prune_frames(FRAME_STORAGE_CAP_MB)
    except Exception as e:
        print(f"[FRAME] bakım hatası: {e}")


def prune_frames(cap_mb):
    """Toplam frame boyutu cap_mb'yi aşarsa en eski (mtime) video klasörlerini
    silerek sınırın altına in. Döner: silinen video klasörü sayısı."""
    cap_bytes = max(1, int(cap_mb)) * 1024 * 1024
    try:
        if not FRAMES_DIR.exists():
            return 0
        dirs = [p for p in FRAMES_DIR.iterdir() if p.is_dir()]
    except Exception:
        return 0
    total = sum(_dir_size(p) for p in dirs)
    if total <= cap_bytes:
        return 0
    # En eskiden yeniye sırala (mtime)
    dirs.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)
    removed = 0
    for p in dirs:
        if total <= cap_bytes:
            break
        sz = _dir_size(p)
        shutil.rmtree(p, ignore_errors=True)
        total -= sz
        removed += 1
    if removed:
        print(f"[FRAME] cap aşıldı → {removed} eski video klasörü silindi "
              f"(kalan ~{total // (1024*1024)}MB / {cap_mb}MB)")
    return removed
