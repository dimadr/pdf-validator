"""One-operation SMTP safety fence for legacy content with unproven delivery history.

Runs the existing PDF pipeline unchanged. Its final transaction retains the
computed PDF decision, but requires operator review and cancels only unattempted
new deliveries before any dispatcher can observe them. No mail is sent here.
"""
from sqlalchemy import event,select
from common.models import Attachment,ProcessingRevision,DeliveryIntent,RecipientDelivery
from common.delivery import ServiceConflict,now

def review_factory(base_factory,attachment_id):
    def fence(session):
        # Production SessionLocal disables autoflush; inspect all planned rows
        # inside this uncommitted transaction before allowing the final commit.
        session.flush()
        attachment=session.get(Attachment,attachment_id)
        revision=session.get(ProcessingRevision,attachment.current_revision_id) if attachment and attachment.current_revision_id else None
        if revision is None or revision.state not in ('validated','rejected','review_required','technical_error'):
            return
        result=dict(revision.validation_result or {})
        if 'state' not in result or result.get('operator_review'):return
        rows=list(session.scalars(select(RecipientDelivery).join(DeliveryIntent).where(DeliveryIntent.revision_id==revision.id)))
        if any(row.attempts or row.state not in ('pending','failed','cancelled') for row in rows):
            raise ServiceConflict('STOP: a new SMTP operation was already attempted')
        result['operator_review']={'reason':'legacy_smtp_history_incomplete','pdf_decision':revision.state,
            'pdf_reason':revision.reason,'new_smtp_attempted':False}
        revision.validation_result=result
        if revision.state in ('validated','technical_error'):
            revision.state='review_required';revision.reason='legacy_smtp_history_incomplete'
        revision.finished_at=now()
        for row in rows:
            row.state='cancelled';row.retry_at=None;row.error_code='legacy_smtp_history_incomplete'
            row.error='New SMTP blocked: original legacy delivery outcome is not independently proven'
    def factory():
        session=base_factory();event.listen(session,'before_commit',fence);return session
    return factory
