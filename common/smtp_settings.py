"""Durable operator selection; credentials stay outside PostgreSQL."""
import os
from pathlib import Path
from datetime import timedelta
from sqlalchemy import select, func
from common.models import Setting, SMTPProviderAudit, SMTPAttempt, utcnow
from common.delivery import ServiceConflict

PROVIDERS = ('brevo', 'timeweb')
LOCK = 703271189041

def selection(db, lock=False):
    if lock:
        db.execute(select(func.pg_advisory_xact_lock(LOCK)))
    row = db.scalar(select(Setting).where(Setting.key == 'smtp_provider'))
    provider = row.value if row else 'timeweb'
    if provider not in PROVIDERS:
        raise ServiceConflict('Unknown SMTP provider; sending blocked')
    return provider

def credentials(provider):
    if provider == 'brevo':
        key_file = os.getenv('BREVO_SMTP_KEY_FILE')
        key = Path(key_file).read_text().strip() if key_file else os.getenv('BREVO_SMTP_KEY', '').strip()
        if not key:
            raise ValueError('Brevo SMTP key is missing')
        user = os.environ['BREVO_SMTP_USER']
        sender = os.getenv('BREVO_SMTP_FROM') or os.environ['SMTP_FROM']
        if not user.strip() or not sender.strip():
            raise ValueError('Brevo SMTP identity is missing')
        return dict(host='smtp-relay.brevo.com', port=587, user=user,
                    password=key, sender=sender, reply_to=sender, test=False)
    if provider != 'timeweb':
        raise ValueError('Unknown SMTP provider')
    return dict(host=os.environ['SMTP_SERVER'], port=int(os.getenv('SMTP_PORT', '587')),
                user=os.getenv('SMTP_USER'), password=os.getenv('SMTP_PASSWORD', ''),
                sender=os.environ['SMTP_FROM'], reply_to=os.getenv('SMTP_REPLY_TO', os.environ['SMTP_FROM']),
                test=os.getenv('SMTP_TEST_MODE', 'false').lower() == 'true')

def available(provider):
    try:
        cfg = credentials(provider)
        return bool(cfg['host'] and cfg['sender'])
    except (OSError, ValueError, KeyError):
        return False

def quota_limit():
    # Conservative rolling 24h cap also covers an account's calendar-day limit.
    value = int(os.getenv('BREVO_DAILY_LIMIT', '300'))
    if not 1 <= value <= 300:
        raise ValueError('BREVO_DAILY_LIMIT must be between 1 and 300')
    return value

def quota_used(db):
    return db.scalar(select(func.count()).select_from(SMTPAttempt).where(
        SMTPAttempt.provider == 'brevo', SMTPAttempt.started_at > utcnow() - timedelta(days=1),
        SMTPAttempt.state.in_(('in_flight', 'accepted', 'uncertain'))))

def status(db):
    provider = selection(db)
    latest = db.scalar(select(SMTPProviderAudit).order_by(SMTPProviderAudit.created_at.desc()).limit(1))
    return {'provider': provider, 'available': {p: available(p) for p in PROVIDERS},
            'brevo_limit_24h': quota_limit(), 'brevo_used_24h': quota_used(db),
            'last_change': None if latest is None else {
                'actor': latest.actor, 'previous': latest.previous, 'provider': latest.provider,
                'created_at': latest.created_at}, 'automatic_failover': False}

def set_provider(db, provider, actor, expected):
    previous = selection(db, lock=True)
    if previous != expected:
        raise ServiceConflict('SMTP selection changed; refresh before confirming')
    if provider not in PROVIDERS or not available(provider):
        raise ServiceConflict('Selected SMTP provider is not configured')
    if previous != provider:
        row = db.scalar(select(Setting).where(Setting.key == 'smtp_provider'))
        if row is None:
            row = Setting(key='smtp_provider'); db.add(row)
        row.value = provider
        db.add(SMTPProviderAudit(actor=str(actor), previous=previous, provider=provider))
        db.flush()
    return status(db)
