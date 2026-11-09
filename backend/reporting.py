"""SQL projections for current v2 outcomes without rewriting legacy history."""
from sqlalchemy import and_, case, exists, not_, or_, select
from common.models import Attachment, ProcessingRevision, DeliveryIntent, RecipientDelivery


def current_outcomes():
    managed=Attachment.current_revision_id.isnot(None)
    legacy=Attachment.current_revision_id.is_(None)
    state=case((managed,ProcessingRevision.state),else_=Attachment.status)
    reason=case((managed,ProcessingRevision.reason),else_=Attachment.reject_reason)
    object_id=case((managed,ProcessingRevision.object_id),else_=Attachment.object_id)
    rejected=or_(and_(managed,ProcessingRevision.state=='rejected'),
                 and_(legacy,or_(Attachment.status=='rejected',Attachment.reject_reason.isnot(None))))
    processed=or_(and_(managed,ProcessingRevision.state.in_(
        ['validated','rejected','review_required','technical_error','discarded_inactive'])),
        and_(legacy,Attachment.status.in_(['validated','sent','rejected'])))
    client_exists=exists(select(1).where(RecipientDelivery.intent_id==DeliveryIntent.id,
                                       RecipientDelivery.role=='client')).correlate(DeliveryIntent)
    latest_intent=select(DeliveryIntent.id).where(
        DeliveryIntent.revision_id==Attachment.current_revision_id,client_exists)\
        .order_by(DeliveryIntent.created_at.desc(),DeliveryIntent.id.desc()).limit(1)\
        .correlate(Attachment).scalar_subquery()
    client_present=exists(select(1).where(RecipientDelivery.intent_id==latest_intent,
        RecipientDelivery.role=='client')).correlate(Attachment)
    incomplete=exists(select(1).where(RecipientDelivery.intent_id==latest_intent,
        RecipientDelivery.role=='client',RecipientDelivery.state!='accepted')).correlate(Attachment)
    smtp_accepted=and_(managed,client_present,not_(incomplete))
    legacy_sent=and_(legacy,Attachment.status=='sent')
    return dict(state=state,reason=reason,object_id=object_id,rejected=rejected,processed=processed,
                smtp_accepted=smtp_accepted,legacy_sent=legacy_sent,
                sent=or_(legacy_sent,smtp_accepted))
