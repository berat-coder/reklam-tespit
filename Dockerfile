FROM python:3.11-slim

# Sistem bağımlılıkları: ffmpeg (hızlı frame çıkarımı + yt-dlp), opencv için libgl
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Kalıcı veri (data.db, frames/, config.json) bu dizinde tutulur (volume mount edilir)
ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 5001

# Varsayılan komut web; worker docker-compose'da override edilir
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5001", "--workers", "2", "--timeout", "180", "--graceful-timeout", "30"]
