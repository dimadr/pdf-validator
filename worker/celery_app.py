from celery import Celery
from celery.schedules import crontab
import os
from dotenv import load_dotenv

load_dotenv()

# Celery configuration
celery_app = Celery(
    "worker",
    broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    backend=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    include=[
        "email_client",
        "attachment_processor", 
        "pdf_validator",
        "email_sender",
        "maintenance",
        "recovery",
        "pipeline",
    ]
)

# Configure Celery
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Europe/Moscow",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=30 * 60,  # 30 minutes
    task_soft_time_limit=25 * 60,  # 25 minutes
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,
    task_default_queue=os.getenv("CELERY_QUEUE", "celery"),
    broker_transport_options={"visibility_timeout": int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "3600")),
                              "global_keyprefix": os.getenv("CELERY_REDIS_KEY_PREFIX", "")},
    result_backend_transport_options={"global_keyprefix": os.getenv("CELERY_REDIS_KEY_PREFIX", "")},
    beat_schedule={
        "reliable-outbox": {"task": "pipeline.dispatch_outbox", "schedule": 5.0},
        "reliable-reconciliation": {"task": "pipeline.reconcile", "schedule": 60.0},
        "brevo-delivery-events": {"task": "pipeline.poll_brevo_events", "schedule": 600.0},
        "fetch-emails-every-2-minutes": {
            "task": "email_client.fetch_emails_task",
            "schedule": 120.0,  # 2 minutes
        },
        "health-check-every-hour": {
            "task": "maintenance.health_check",
            "schedule": 3600.0,  # 1 hour
        },
        "recover-stuck-attachments-every-10-minutes": {
            "task": "recovery.recover_stuck_attachments",
            "schedule": 600.0,  # 10 minutes
        },
    },
)

# Database settings (same as backend)
DATABASE_URL = os.environ["DATABASE_URL"]

# Email settings
IMAP_SERVER = os.getenv("IMAP_SERVER")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD")

SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

# Admin notification email (fallback recipients)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "admin2@example.com")

# Storage
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")
MAX_PDF_SIZE_MB = int(os.getenv("MAX_PDF_SIZE_MB", "50"))

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT = os.getenv("LOG_FORMAT", "json")
