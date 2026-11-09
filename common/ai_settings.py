"""Live, transactionally audited administrative switch. Never expose credentials."""
import os
from sqlalchemy import select, text
from common.models import AIControl, AISettingAudit, AIRequest, utcnow

LOCK = 713016320

def boolean_env(name):
    return os.getenv(name, '').lower() in ('true', '1', 'yes')

def control(db, lock=False):
    if lock:
        db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': LOCK})
    return db.scalar(select(AIControl).where(AIControl.id == 1).execution_options(populate_existing=True))

def status(db):
    row = control(db)
    requested = bool(row and row.enabled)
    key = bool(os.getenv('ROUTERAI_API_KEY', '').strip())
    forced = boolean_env('AI_FORCE_DISABLED')
    availability = ('disabled' if not requested else 'force_disabled' if forced else
                    'missing_key' if not key else 'enabled')
    recent=db.scalar(select(AIRequest).order_by(AIRequest.created_at.desc()).limit(1))
    health='not_checked' if recent is None else 'unavailable' if recent.state in ('failed','unknown') else 'pending' if recent.state=='reserved' else 'response_received'
    if availability=='enabled' and health=='unavailable':availability='provider_unavailable'
    return {'requested': requested, 'effective_ai': requested and key and not forced,
            'availability': availability, 'generation': row.generation if row else 0,
            'provider_health': health}

def set_enabled(db, enabled, actor):
    row = control(db, lock=True)
    if row is None:
        row = AIControl(id=1, enabled=False, generation=0)
        db.add(row); db.flush()
    if row.enabled != enabled:
        db.add(AISettingAudit(actor=str(actor), previous=row.enabled, enabled=enabled, generation=row.generation+1))
        row.enabled = enabled; row.generation += 1
        row.enabled_at = utcnow() if enabled else None
        db.flush()
    return status(db)

def permitted(db, generation=None, revision=None, lock=False):
    row = control(db, lock=lock)
    if not row or not row.enabled or not status(db)['effective_ai']:
        return False
    if generation is not None and row.generation != generation:
        return False
    # Turning ON must not replay documents that arrived under an earlier OFF epoch.
    if revision is not None and row.enabled_at is not None:
        from datetime import timezone
        created = revision.created_at
        if created.tzinfo is None: created = created.replace(tzinfo=timezone.utc)
        if created < row.enabled_at: return False
    return True
