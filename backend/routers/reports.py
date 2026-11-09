"""Reports project current revisions; legacy columns remain immutable history."""
import csv
import io
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, cast, Date, func, or_
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.reporting import current_outcomes
from backend.routers.auth import get_current_user
from backend.utils import decode_email_header
from common.models import Attachment, ProcessingRevision, Object, IncomingMessage, User

router=APIRouter()


class RejectionReport(BaseModel):
    id: UUID
    filename: str
    reject_reason: str
    reject_details: Optional[dict]
    created_at: datetime
    object_name: Optional[str]
    message_subject: Optional[str]
    from_email: Optional[str]


class ReportSummary(BaseModel):
    total_attachments: int
    processed: int
    rejected: int
    sent: int
    smtp_accepted: int
    legacy_sent: int
    rejected_by_reason: dict


def _dates(query,date_from,date_to):
    query=query.filter(Attachment.status!='archived')
    if date_from:query=query.filter(Attachment.created_at>=date_from)
    if date_to:query=query.filter(Attachment.created_at<=date_to)
    return query


def _rejections(db,date_from,date_to,reject_reason):
    outcome=current_outcomes()
    query=db.query(Attachment,ProcessingRevision,Object,IncomingMessage)\
        .outerjoin(ProcessingRevision,ProcessingRevision.id==Attachment.current_revision_id)\
        .outerjoin(Object,Object.id==outcome['object_id'])\
        .outerjoin(IncomingMessage,IncomingMessage.id==Attachment.message_id)\
        .filter(outcome['rejected'])
    query=_dates(query,date_from,date_to)
    if reject_reason:query=query.filter(outcome['reason']==reject_reason)
    return query


def _report(row):
    attachment,revision,obj,message=row
    managed=attachment.current_revision_id is not None
    return RejectionReport(id=attachment.id,filename=attachment.filename,
        reject_reason=(revision.reason or revision.state) if managed else (attachment.reject_reason or 'rejected'),
        reject_details=revision.validation_result if managed else attachment.validation_result,
        created_at=attachment.created_at,object_name=obj.name if obj else None,
        message_subject=message.subject if message else None,
        from_email=decode_email_header(message.from_email) if message else None)


@router.get('/rejections',response_model=List[RejectionReport])
def get_rejection_report(response:Response,skip:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=1000),
    date_from:Optional[datetime]=Query(None),date_to:Optional[datetime]=Query(None),
    reject_reason:Optional[str]=Query(None),db:Session=Depends(get_db),current_user:User=Depends(get_current_user)):
    query=_rejections(db,date_from,date_to,reject_reason)
    response.headers['X-Total-Count']=str(query.count())
    return [_report(row) for row in query.order_by(Attachment.created_at.desc(),Attachment.id).offset(skip).limit(limit)]


@router.get('/rejections.csv')
def get_rejection_report_csv(date_from:Optional[datetime]=Query(None),date_to:Optional[datetime]=Query(None),
    reject_reason:Optional[str]=Query(None),db:Session=Depends(get_db),current_user:User=Depends(get_current_user)):
    output=io.StringIO();writer=csv.writer(output)
    writer.writerow(['ID','Filename','Reject Reason','Created At','Object Name','Message Subject','From Email','Validation Details'])
    for row in _rejections(db,date_from,date_to,reject_reason).order_by(Attachment.created_at.desc(),Attachment.id):
        report=_report(row)
        writer.writerow([str(report.id),report.filename,report.reject_reason,report.created_at.isoformat(),
                         report.object_name or '',report.message_subject or '',report.from_email or '',
                         str(report.reject_details) if report.reject_details else ''])
    return StreamingResponse(io.BytesIO(output.getvalue().encode('utf-8-sig')),media_type='text/csv',
        headers={'Content-Disposition':'attachment; filename=rejections_report.csv'})


@router.get('/summary',response_model=ReportSummary)
def get_report_summary(date_from:Optional[datetime]=Query(None),date_to:Optional[datetime]=Query(None),
    db:Session=Depends(get_db),current_user:User=Depends(get_current_user)):
    c=current_outcomes()
    query=db.query(func.count().label('total'),
        func.count().filter(c['processed']).label('processed'),
        func.count().filter(c['rejected']).label('rejected'),
        func.count().filter(c['sent']).label('sent'),
        func.count().filter(c['smtp_accepted']).label('smtp_accepted'),
        func.count().filter(c['legacy_sent']).label('legacy_sent'))\
        .select_from(Attachment).outerjoin(ProcessingRevision,ProcessingRevision.id==Attachment.current_revision_id)
    row=_dates(query,date_from,date_to).one()
    reasons=db.query(func.coalesce(c['reason'],'rejected'),func.count()).select_from(Attachment)\
        .outerjoin(ProcessingRevision,ProcessingRevision.id==Attachment.current_revision_id).filter(c['rejected'])\
        .group_by(func.coalesce(c['reason'],'rejected'))
    breakdown=dict(_dates(reasons,date_from,date_to).all())
    # sent keeps the legacy aggregate shape; the UI uses v2-only smtp_accepted,
    # never interpreting old sent/admin fallback as proven client submission.
    return ReportSummary(total_attachments=row.total,processed=row.processed,rejected=row.rejected,
        sent=row.sent,smtp_accepted=row.smtp_accepted,legacy_sent=row.legacy_sent,rejected_by_reason=breakdown)


@router.get('/processing-stats')
def get_processing_stats(date_from:Optional[datetime]=Query(None),date_to:Optional[datetime]=Query(None),
    db:Session=Depends(get_db),current_user:User=Depends(get_current_user)):
    c=current_outcomes()
    # Preserve the old daily hard-status rule for unmanaged rows.
    rejected=or_(and_(Attachment.current_revision_id.isnot(None),ProcessingRevision.state=='rejected'),
                 and_(Attachment.current_revision_id.is_(None),Attachment.status=='rejected'))
    query=db.query(cast(Attachment.created_at,Date).label('date'),func.count().label('total'),
        func.count().filter(c['processed']).label('processed'),
        func.count().filter(rejected).label('rejected'),func.count().filter(c['sent']).label('sent'),
        func.count().filter(c['smtp_accepted']).label('smtp_accepted'),
        func.count().filter(c['legacy_sent']).label('legacy_sent')).select_from(Attachment)\
        .outerjoin(ProcessingRevision,ProcessingRevision.id==Attachment.current_revision_id)\
        .group_by(cast(Attachment.created_at,Date))
    values={}
    for row in _dates(query,date_from,date_to):
        values[row.date.isoformat()]={key:getattr(row,key) for key in
            ('total','processed','rejected','sent','smtp_accepted','legacy_sent')}
    return {'daily_stats':values,'total_days':len(values)}
