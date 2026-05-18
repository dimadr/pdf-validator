from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Optional
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel
import csv
import io

from database import get_db
from models import Attachment as AttachmentModel, Object as ObjectModel, IncomingMessage as MessageModel
from utils import decode_email_header
from sqlalchemy import func

router = APIRouter()

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
    rejected_by_reason: dict

@router.get("/rejections", response_model=List[RejectionReport])
async def get_rejection_report(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    reject_reason: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Получить отчет об отклоненных вложениях (JSON)"""
    # Include hard rejections by status and soft rejections where status later changes,
    # but reject_reason remains filled.
    query = db.query(AttachmentModel).outerjoin(ObjectModel).outerjoin(MessageModel).filter(
        or_(
            AttachmentModel.status == 'rejected',
            AttachmentModel.reject_reason.isnot(None)
        )
    )
    
    if date_from:
        query = query.filter(AttachmentModel.created_at >= date_from)
    if date_to:
        query = query.filter(AttachmentModel.created_at <= date_to)
    if reject_reason:
        query = query.filter(AttachmentModel.reject_reason == reject_reason)
    
    attachments = query.order_by(AttachmentModel.created_at.desc()).offset(skip).limit(limit).all()
    
    reports = []
    for attachment in attachments:
        reports.append(RejectionReport(
            id=attachment.id,
            filename=attachment.filename,
            reject_reason=attachment.reject_reason,
            reject_details=attachment.validation_result,
            created_at=attachment.created_at,
            object_name=attachment.object.name if attachment.object else None,
            message_subject=attachment.message.subject if attachment.message else None,
            from_email=decode_email_header(attachment.message.from_email) if attachment.message else None
        ))
    
    return reports

@router.get("/rejections.csv")
async def get_rejection_report_csv(
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    reject_reason: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Экспорт отчета об отклоненных вложениях в CSV"""
    query = db.query(AttachmentModel).outerjoin(ObjectModel).outerjoin(MessageModel).filter(
        or_(
            AttachmentModel.status == 'rejected',
            AttachmentModel.reject_reason.isnot(None)
        )
    )
    
    if date_from:
        query = query.filter(AttachmentModel.created_at >= date_from)
    if date_to:
        query = query.filter(AttachmentModel.created_at <= date_to)
    if reject_reason:
        query = query.filter(AttachmentModel.reject_reason == reject_reason)
    
    attachments = query.order_by(AttachmentModel.created_at.desc()).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Headers
    writer.writerow([
        'ID', 'Filename', 'Reject Reason', 'Created At', 
        'Object Name', 'Message Subject', 'From Email', 'Validation Details'
    ])
    
    # Data
    for attachment in attachments:
        writer.writerow([
            str(attachment.id),
            attachment.filename,
            attachment.reject_reason,
            attachment.created_at.isoformat(),
            attachment.object.name if attachment.object else '',
            attachment.message.subject if attachment.message else '',
            decode_email_header(attachment.message.from_email) if attachment.message else '',
            str(attachment.validation_result) if attachment.validation_result else ''
        ])
    
    output.seek(0)
    
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode('utf-8-sig')),  # Add BOM for Excel
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=rejections_report.csv"}
    )

@router.get("/summary", response_model=ReportSummary)
async def get_report_summary(
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    db: Session = Depends(get_db)
):
    """Получить сводный отчет"""
    query = db.query(AttachmentModel)
    
    if date_from:
        query = query.filter(AttachmentModel.created_at >= date_from)
    if date_to:
        query = query.filter(AttachmentModel.created_at <= date_to)
    
    # Get counts by status in single query
    stats = db.query(
        func.count().label('total'),
        func.count().filter(AttachmentModel.status.in_(['validated', 'sent', 'rejected'])).label('processed'),
        func.count().filter(AttachmentModel.reject_reason.isnot(None)).label('rejected'),
        func.count().filter(AttachmentModel.status == 'sent').label('sent')
    ).select_from(AttachmentModel)
    
    if date_from:
        stats = stats.filter(AttachmentModel.created_at >= date_from)
    if date_to:
        stats = stats.filter(AttachmentModel.created_at <= date_to)
    
    result = stats.first()
    total_attachments = result.total
    processed = result.processed
    rejected = result.rejected
    sent = result.sent
    
    # Get rejection breakdown by reason
    rejected_by_reason = {}
    rejections = query.filter(AttachmentModel.reject_reason.isnot(None)).all()
    for rejection in rejections:
        reason = rejection.reject_reason or 'unknown'
        rejected_by_reason[reason] = rejected_by_reason.get(reason, 0) + 1
    
    return ReportSummary(
        total_attachments=total_attachments,
        processed=processed,
        rejected=rejected,
        sent=sent,
        rejected_by_reason=rejected_by_reason
    )

@router.get("/processing-stats")
async def get_processing_stats(
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    db: Session = Depends(get_db)
):
    """Получить статистику обработки по дням"""
    from sqlalchemy import cast, Date
    
    daily_query = db.query(
        cast(AttachmentModel.created_at, Date).label('date'),
        func.count().label('total'),
        func.count().filter(AttachmentModel.status == 'rejected').label('rejected'),
        func.count().filter(AttachmentModel.status == 'sent').label('sent'),
        func.count().filter(AttachmentModel.status.in_(['validated', 'sent', 'rejected'])).label('processed')
    ).group_by(cast(AttachmentModel.created_at, Date))
    
    if date_from:
        daily_query = daily_query.filter(AttachmentModel.created_at >= date_from)
    if date_to:
        daily_query = daily_query.filter(AttachmentModel.created_at <= date_to)
    
    daily_stats = {}
    for row in daily_query:
        daily_stats[row.date.isoformat()] = {
            'total': row.total,
            'processed': row.processed,
            'rejected': row.rejected,
            'sent': row.sent
        }
    
    return {
        'daily_stats': daily_stats,
        'total_days': len(daily_stats)
    }
