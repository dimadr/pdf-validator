# Import all tasks to make them available to Celery
from email_client import fetch_emails_task
from attachment_processor import process_message_attachments
from pdf_validator import validate_pdf_attachment
from email_sender import send_pdf_attachment

from maintenance import cleanup_old_files, health_check

# All tasks are now registered