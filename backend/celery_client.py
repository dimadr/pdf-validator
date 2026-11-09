import os
from celery import Celery
from dotenv import load_dotenv

load_dotenv()

celery_broker = Celery(
    "backend_client",
    broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
)
celery_broker.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
)
