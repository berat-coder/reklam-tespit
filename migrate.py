"""
data.json → SQLite migration.

Kullanım:
    python migrate.py                          # varsayılan: data.json
    python migrate.py /path/to/data.json
"""

import json
import sys
from pathlib import Path

DATA_JSON = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "data.json"


def run():
    if not DATA_JSON.exists():
        print(f"[MIGRATE] {DATA_JSON} bulunamadı — çıkılıyor.")
        return

    raw = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    channels = raw.get("channels", {})

    if not channels:
        print("[MIGRATE] data.json boş, yapacak bir şey yok.")
        return

    # DB'yi başlat
    from models.database import init_db, upsert_channel, upsert_video, save_detections
    init_db()

    ch_count = vid_count = det_count = 0

    for ch_id, ch in channels.items():
        upsert_channel(
            ch_id=ch_id,
            name=ch.get("name", ""),
            url=ch.get("url", ""),
            channel_logos=ch.get("channel_logos", []),
            last_scanned=ch.get("last_scanned"),
        )
        ch_count += 1
        print(f"  ✓ Kanal: {ch.get('name', ch_id)}")

        for vid_id, v in (ch.get("videos") or {}).items():
            upsert_video(
                video_id=vid_id,
                channel_id=ch_id,
                title=v.get("title", ""),
                url=v.get("url", ""),
                duration=v.get("duration", 0),
                thumbnail=v.get("thumbnail", ""),
                analyzed_at=v.get("analyzed_at"),
                total_frames=v.get("total_frames", 0),
                api_calls=v.get("api_calls", 0),
                ad_frame_count=v.get("ad_frame_count", 0),
                type_counts=v.get("type_counts", {}),
                brand_counts=v.get("brand_counts", {}),
                desc_brands=v.get("desc_brands", []),
                completed=bool(v.get("completed", False)),
            )
            vid_count += 1

            detections = v.get("detections", [])
            if detections:
                save_detections(vid_id, detections)
                det_count += len(detections)

            print(f"    → Video: {v.get('title', vid_id)[:50]}  "
                  f"({len(detections)} detection)")

    print(f"\n[MIGRATE] Tamamlandı: {ch_count} kanal, {vid_count} video, {det_count} detection")


if __name__ == "__main__":
    run()
