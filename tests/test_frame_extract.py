"""
Kare çıkarma regresyon testleri — AĞ YOK (_ffmpeg_seek_frame taklit edilir).

Neden kritik: üretimde akış URL'si ölüyken 121 seek'in hepsi başarısız oluyor,
sonra onarım geçişi AYNI 121 noktayı yavaş modda tekrar deniyordu. Sonuç:
97 dakikalık yayından 1 kare, boşa bir Gemini çağrısı ve iki tur bant genişliği.

Çalıştırma:  ./.venv/bin/python tests/test_frame_extract.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
from services import tasks                                        # noqa: E402
from services.tasks import _extract_frames_ffmpeg, reset_seek_errors, seek_error_summary  # noqa: E402

ok = True
DUR, INT = 5830, 48          # üretimdeki 5E0dtd_6w4c ölçüleri
N = len(range(0, DUR, INT))  # beklenen örnek noktası sayısı


def check(name, cond, extra=""):
    global ok
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  → {extra}" if not cond else ""))
    if not cond:
        ok = False


def run(should_succeed):
    """should_succeed(idx, fast) -> bool. Döner: (alınan kare, toplam çağrı).
    `fast` parametresi ÖNEMLİ: onarım geçişi hassas modla (fast=False) çağırır,
    gerçekte de tam bu yüzden hızlı modda boş kalan kareleri kurtarabiliyor."""
    calls = {"n": 0}

    def fake_seek(url, out_path, t, width, fast=True):
        calls["n"] += 1
        idx = t // INT
        if should_succeed(idx, fast):
            return Path(out_path)          # dosya yazmıyoruz; sadece yol dönüyor
        tasks._log_seek_error(b"HTTP error 403 Forbidden")
        return None

    real = tasks._ffmpeg_seek_frame
    tasks._ffmpeg_seek_frame = fake_seek
    reset_seek_errors()
    try:
        out = _extract_frames_ffmpeg("http://x/stream", Path("/tmp"), INT, 640,
                                     duration=DUR, expected=N)
    finally:
        tasks._ffmpeg_seek_frame = real
    return len(out), calls["n"]


print(f"\n[0] örnek noktası sayısı = {N}")

print("\n[1] ÖLÜ AKIŞ: her seek başarısız → ön yoklamada erken çıkış")
got, calls = run(lambda i, fast: False)
check("hiç kare dönmedi", got == 0, got)
check(f"yalnız ön yoklama denendi (≤6, {N} değil)", calls <= 6, f"{calls} çağrı")
check("242 çağrılık çift tur YOK", calls < N, f"{calls} çağrı")
check("sebep toplandı (403)", "403" in seek_error_summary(), seek_error_summary())

print("\n[2] SAĞLAM AKIŞ: hepsi başarılı → tam kare, onarım yok")
got, calls = run(lambda i, fast: True)
check(f"tüm {N} kare alındı", got == N, got)
check("gereksiz tekrar çağrı yok", calls == N, f"{calls} çağrı")

print("\n[3] TEK TÜK BOŞLUK (%20) → onarım geçişi ÇALIŞIR")
# hızlı modda %20 boş, hassas modda kurtarılıyor (gerçek davranış)
got, calls = run(lambda i, fast: (i % 5 != 1) or not fast)
check("boşluklar onarımda tamamlandı", got == N, f"{got}/{N}")
check("onarım ek çağrı yaptı", calls > N, f"{calls} çağrı")

print("\n[4] ÇOĞUNLUK BOŞ (%80) ama yoklama geçti → onarım ATLANIR")
# Yoklama noktaları üretim koduyla AYNI formülden türetilmeli; sabit kopya
# yazmak, formül değişince testi sessizce yanlış yere bakar hale getiriyordu.
_son = int((N - 1) * 0.9)
probe = sorted({int(i * _son / 3) for i in range(4)})
got, calls = run(lambda i, fast: i in probe or i % 5 == 0)
check("alınan kareler korundu", got > 0, got)
check("onarım atlandı (çağrı ≈ nokta sayısı)", calls <= N + 2, f"{calls} çağrı")

print("\n[5] Sebep özeti sınırlı ve tekilleştirilmiş")
reset_seek_errors()
for i in range(50):
    tasks._log_seek_error(f"HTTP error 403 Forbidden (deneme {i})".encode())
check("aynı hata bir kez yazıldı", seek_error_summary().count("403") == 1,
      seek_error_summary())
for m in (b"Connection timed out", b"Server returned 5XX", b"DNS failure"):
    tasks._log_seek_error(m)
check("en fazla 3 benzersiz sebep", len(seek_error_summary().split(" | ")) <= 3,
      seek_error_summary())

print("\n" + ("TÜM TESTLER GEÇTİ" if ok else "BAŞARISIZ"))
sys.exit(0 if ok else 1)
