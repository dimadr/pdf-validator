"""Recovery task for stuck attachments (lost due to worker crash)"""

import os
from celery_app import celery_app
from utils import logger
from database import get_db
from models import Attachment, Object
from datetime import datetime, timezone, timedelta

@celery_app.task(bind=True, name="recovery.recover_stuck_attachments")
def recover_stuck_attachments(self):
    """Find validated/rejected/approved attachments without sent_to_email and re-queue them"""
    from common.config import enabled
    if enabled():
        from common.delivery import reconcile
        from database import SessionLocal
        return reconcile(SessionLocal)

    logger.info("[RECOVERY] Starting recovery check for stuck attachments")

    min_age = timedelta(minutes=10)
    cutoff = datetime.now(timezone.utc) - min_age

    recovered = {"validated": 0, "rejected": 0, "approved": 0, "skipped_inactive": 0}

    with get_db() as db:
        try:
            stuck = db.query(Attachment).filter(
                Attachment.status.in_(["validated", "rejected", "approved"]),
                (
                    (Attachment.updated_at < cutoff) |
                    ((Attachment.updated_at.is_(None)) & (Attachment.created_at < cutoff))
                ),
                (Attachment.sent_to_email.is_(None) | (Attachment.sent_to_email == "")),
            ).all()

            if not stuck:
                logger.info("[RECOVERY] No stuck attachments found")
                return {"status": "ok", "recovered": 0}

            logger.info(f"[RECOVERY] Found {len(stuck)} stuck attachments")

            for att in stuck:
                if att.current_revision_id is not None:
                    continue
                # Skip if linked object is inactive — reject and delete PDF
                inactive = None
                if att.calculator_number:
                    inactive = db.query(Object).filter(
                        Object.calculator_number == att.calculator_number,
                        Object.is_active == False
                    ).first()
                if not inactive and att.object_id:
                    inactive = db.query(Object).filter(
                        Object.id == att.object_id,
                        Object.is_active == False
                    ).first()
                if inactive:
                    logger.info(f"[RECOVERY] Rejecting {att.id}: object {inactive.name} is inactive")
                    att.status = 'rejected'
                    att.reject_reason = 'object_inactive'
                    if att.file_path and os.path.exists(att.file_path):
                        os.remove(att.file_path)
                        logger.info(f"[RECOVERY] Deleted PDF: {att.file_path}")
                    recovered["skipped_inactive"] += 1
                    continue

                if att.status == "approved":
                    from pdf_validator import finalize_validation
                    finalize_validation.delay(str(att.id))
                    recovered["approved"] += 1
                    logger.info(f"[RECOVERY] Queued finalize_validation for {att.id} (approved)")
                elif att.status in ("validated", "rejected"):
                    from email_sender import send_pdf_attachment
                    send_pdf_attachment.delay(str(att.id))
                    recovered[att.status] += 1
                    logger.info(f"[RECOVERY] Queued send_pdf_attachment for {att.id} ({att.status})")

            db.commit()

        except Exception as e:
            logger.error(f"[RECOVERY] Error during recovery: {e}")
            return {"status": "error", "message": str(e)}

    logger.info(f"[RECOVERY] Done: {recovered}")
    return {"status": "ok", "recovered": recovered}
