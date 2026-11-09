"""Current client operation, excluding previous resends and administrator notifications."""
from sqlalchemy import select,func,exists
from common.models import DeliveryIntent,RecipientDelivery

def client_delivery(db,revision_id):
    present=exists(select(RecipientDelivery.id).where(RecipientDelivery.intent_id==DeliveryIntent.id,RecipientDelivery.role=='client'))
    intent=db.scalar(select(DeliveryIntent).where(DeliveryIntent.revision_id==revision_id,present)
        .order_by(DeliveryIntent.created_at.desc(),DeliveryIntent.id.desc()).limit(1))
    counts=dict(db.execute(select(RecipientDelivery.state,func.count()).where(
        RecipientDelivery.intent_id==intent.id,RecipientDelivery.role=='client').group_by(RecipientDelivery.state)).all()) if intent else {}
    total=sum(counts.values())
    providers=sorted(set(db.scalars(select(RecipientDelivery.smtp_provider).where(RecipientDelivery.intent_id==intent.id,
        RecipientDelivery.role=='client',RecipientDelivery.smtp_provider.isnot(None),RecipientDelivery.attempts>0)))) if intent else []
    return {'intent_id':str(intent.id) if intent else None,'counts':counts,'providers':providers,'all_accepted':total>0 and counts.get('accepted',0)==total}
