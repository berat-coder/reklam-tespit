"""
RQ worker başlatıcı.
Heroku'da:  heroku run worker (Procfile'daki worker dyno)
Lokalde:    python worker.py   (REDIS_URL set edilmişse)
"""

import os
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

from redis import Redis
from rq import Worker, Queue

conn = Redis.from_url(REDIS_URL)

if __name__ == "__main__":
    queues = [Queue(connection=conn)]
    worker = Worker(queues, connection=conn)
    print(f"[WORKER] Redis: {REDIS_URL}")
    worker.work(with_scheduler=True)
