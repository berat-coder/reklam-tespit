FROM python:3.11-slim

# Sistem bağımlılıkları: ffmpeg (kare çıkarımı + yt-dlp), opencv için libgl,
# deno indirmek için curl+unzip.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libgl1 libglib2.0-0 curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── JS RUNTIME (deno) — TESPİTİN ÇALIŞMASI İÇİN ZORUNLU ──
# yt-dlp, YouTube'un imza ve "n" sınamasını çözmek için HARİCİ bir JS runtime
# ister (yt-dlp-ejs eklentisiyle birlikte). İmajda yoksa:
#   "Signature solving failed / n challenge solving failed"
#   → web ailesindeki TÜM client'lar 0 format döndürür; ayakta kalan tek yol
#     android_vr olur ve o da yalnız tek bir 360p progressive format verir,
#     DASH'i "GVS PO Token which was not provided" ile atlanır → kare çıkmaz.
# 18-28 Ağustos 2026 arasındaki "0 başarılı analiz" arızasının KÖK NEDENİ buydu;
# IP/proxy/yt-dlp sürümü değil (üçü de ölçümle elendi).
# Doğrulama: `yt-dlp -v` çıktısında şu satır dolu olmalı →
#   [debug] [youtube] [jsc] JS Challenge Providers: ... deno ...
ARG DENO_VERSION=2.7.14
RUN curl -fsSL -o /tmp/deno.zip \
        "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip && chmod +x /usr/local/bin/deno \
    && deno --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Kalıcı veri (data.db, frames/, config.json) bu dizinde tutulur (volume mount edilir)
ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 5001

# Varsayılan komut web (worker docker-compose'da override edilir).
# Shell form → $PORT genişler: Railway PORT'u set eder; compose'da PORT yoksa 5001.
# Tek worker + thread = Redis'siz thread-kuyruğu tutarlı çalışır.
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-5001} --workers 1 --threads 8 --timeout 300 --graceful-timeout 30"]
