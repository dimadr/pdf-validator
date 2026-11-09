from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from typing import List, Optional
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel
from backend.database import get_db
from common.models import IncomingMessage as MessageModel, Attachment as AttachmentModel, Object as ObjectModel, User as UserModel, ProcessingRevision
from backend.routers.auth import get_current_user, require_admin
from backend.utils import decode_email_header
from backend.pipeline_views import pipeline_view, delivery_page, delivery_event_page
from backend.reporting import current_outcomes

router = APIRouter()

# Pydantic models for messages and attachments
class MessageBase(BaseModel):
    pass

class Message(MessageBase):
    id: UUID
    provider_message_id: Optional[str]
    source_id: UUID
    from_email: str
    subject: Optional[str]
    parsed_object: Optional[str]
    parsed_address: Optional[str]
    status: str
    error_message: Optional[str]
    received_at: datetime
    processed_at: Optional[datetime]

    class Config:
        from_attributes = True

class AttachmentBase(BaseModel):
    pass

class Attachment(AttachmentBase):
    id: UUID
    message_id: UUID
    object_id: Optional[UUID]
    filename: str
    sent_filename: Optional[str]
    file_path: Optional[str]
    file_sha256: str
    file_size: Optional[int]
    calculator_number: Optional[str]
    status: str
    reject_reason: Optional[str]
    validation_result: Optional[dict]
    gpt_response: Optional[dict]
    sent_to_email: Optional[str]
    sent_at: Optional[datetime]
    read_receipt_received: Optional[bool] = False
    read_receipt_at: Optional[datetime] = None
    original_message_id: Optional[str] = None
    created_at: datetime
    current_revision_id: Optional[UUID] = None
    current_revision: Optional[dict] = None
    recipient_state_counts: Optional[dict] = None
    client_delivery: Optional[dict] = None
    legacy_history_only: bool = True
    effective_status: Optional[str] = None

    class Config:
        from_attributes = True

class AttachmentWithObject(Attachment):
    object: Optional[dict] = None
    message: Optional[dict] = None

# Messages endpoints
@router.get("/messages", response_model=List[Message])
async def list_messages(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    status: Optional[str] = Query(None),
    from_email: Optional[str] = Query(None),
    source_id: Optional[UUID] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user)
):
    """Получить список входящих писем"""
    query = db.query(MessageModel)
    
    if status:
        query = query.filter(MessageModel.status == status)
    if from_email:
        query = query.filter(MessageModel.from_email.contains(from_email))
    if source_id:
        query = query.filter(MessageModel.source_id == source_id)
    if date_from:
        query = query.filter(MessageModel.received_at >= date_from)
    if date_to:
        query = query.filter(MessageModel.received_at <= date_to)
    
    return query.order_by(MessageModel.received_at.desc()).offset(skip).limit(limit).all()

@router.get("/messages/{message_id}", response_model=Message)
async def get_message(message_id: UUID, db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    """Получить письмо по ID"""
    message = db.query(MessageModel).filter(MessageModel.id == message_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    return message

# Attachments endpoints
@router.get("/attachments", response_model=List[AttachmentWithObject])
async def list_attachments(
    response: Response,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    status: Optional[str] = Query(None),
    reject_reason: Optional[str] = Query(None),
    object_id: Optional[UUID] = Query(None),
    message_id: Optional[UUID] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    search: Optional[str] = Query(None, description="Search by object name, calculator number, or email"),
    sort_by: Optional[str] = Query(None, description="Sort field: created_at, sent_at, status, filename"),
    sort_order: Optional[str] = Query("desc", regex="^(asc|desc)$"),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user)
):
    """Получить список вложений с фильтрами"""
    outcome=current_outcomes()
    query = db.query(AttachmentModel)\
        .outerjoin(ProcessingRevision,ProcessingRevision.id==AttachmentModel.current_revision_id)\
        .outerjoin(ObjectModel,ObjectModel.id==outcome['object_id'])
    query=query.filter(AttachmentModel.status=='archived' if status=='archived' else AttachmentModel.status!='archived')
    
    if status:
        if status=='archived':
            pass
        elif status=='accepted':
            query=query.filter(outcome['smtp_accepted'])
        elif status=='sent':
            query=query.filter(outcome['sent'])
        elif status in ('new','processing'):
            query=query.filter(or_(and_(AttachmentModel.current_revision_id.is_(None),AttachmentModel.status==status),
                and_(AttachmentModel.current_revision_id.isnot(None),ProcessingRevision.state=={'new':'received','processing':'extracting'}[status])))
        else:
            query=query.filter(outcome['state']==status)
    if reject_reason:
        query = query.filter(outcome['reason'] == reject_reason)
    if object_id:
        query = query.filter(outcome['object_id'] == object_id)
    if message_id:
        query = query.filter(AttachmentModel.message_id == message_id)
    if date_from:
        query = query.filter(AttachmentModel.created_at >= date_from)
    if date_to:
        query = query.filter(AttachmentModel.created_at <= date_to)
    
    # Search by object name, calculator number, or sent email
    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            or_(
                ObjectModel.name.ilike(search_pattern),
                ObjectModel.calculator_number.ilike(search_pattern),
                ObjectModel.email.ilike(search_pattern),
                AttachmentModel.sent_to_email.ilike(search_pattern),
                AttachmentModel.filename.ilike(search_pattern)
            )
        )
    
    # Sorting
    if sort_by and sort_by not in {'created_at','sent_at','status','filename'}:
        raise HTTPException(status_code=422,detail='Unsupported sort field')
    sort_column = outcome['state'] if sort_by=='status' else (getattr(AttachmentModel, sort_by) if sort_by else AttachmentModel.created_at)
    if sort_order == "asc":
        query = query.order_by(sort_column.asc())
    else:
        query = query.order_by(sort_column.desc())
    
    response.headers["X-Total-Count"] = str(query.count())
    attachments = query.offset(skip).limit(limit).all()
    
    # Add object info if available
    result = []
    for attachment in attachments:
        attachment_dict = {
            'id': attachment.id,
            'message_id': attachment.message_id,
            'object_id': attachment.object_id,
            'filename': attachment.filename,
            'sent_filename': attachment.sent_filename,
            'file_path': attachment.file_path,
            'file_sha256': attachment.file_sha256,
            'file_size': attachment.file_size,
            'calculator_number': attachment.calculator_number,
            'status': attachment.status,
            'reject_reason': attachment.reject_reason,
            'validation_result': attachment.validation_result,
            'gpt_response': attachment.gpt_response,
            'sent_to_email': attachment.sent_to_email,
            'sent_at': attachment.sent_at,
            'read_receipt_received': attachment.read_receipt_received or False,
            'read_receipt_at': attachment.read_receipt_at,
            'created_at': attachment.created_at
        }
        if attachment.object:
            attachment_dict['object'] = {
                'id': attachment.object.id,
                'name': attachment.object.name,
                'email': attachment.object.email
            }
        if attachment.message:
            attachment_dict['message'] = {
                'id': attachment.message.id,
                'subject': attachment.message.subject,
                'from_email': decode_email_header(attachment.message.from_email),
                'received_at': attachment.message.received_at
            }
        attachment_dict.update(pipeline_view(db,attachment))
        result.append(AttachmentWithObject(**attachment_dict))
    
    return result

@router.get("/attachments/{attachment_id}", response_model=AttachmentWithObject)
async def get_attachment(attachment_id: UUID, db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    """Получить детали вложения"""
    attachment = db.query(AttachmentModel).outerjoin(ObjectModel).filter(AttachmentModel.id == attachment_id).first()
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")
    
    attachment_dict = {
        'id': attachment.id,
        'message_id': attachment.message_id,
        'object_id': attachment.object_id,
        'filename': attachment.filename,
        'sent_filename': attachment.sent_filename,
        'file_path': attachment.file_path,
        'file_sha256': attachment.file_sha256,
        'file_size': attachment.file_size,
        'calculator_number': attachment.calculator_number,
        'status': attachment.status,
        'reject_reason': attachment.reject_reason,
        'validation_result': attachment.validation_result,
        'gpt_response': attachment.gpt_response,
        'sent_to_email': attachment.sent_to_email,
        'sent_at': attachment.sent_at,
        'read_receipt_received': attachment.read_receipt_received or False,
        'read_receipt_at': attachment.read_receipt_at,
        'original_message_id': attachment.original_message_id,
        'created_at': attachment.created_at
    }
    if attachment.object:
        attachment_dict['object'] = {
            'id': attachment.object.id,
            'name': attachment.object.name,
            'email': attachment.object.email
        }
    if attachment.message:
        attachment_dict['message'] = {
            'id': attachment.message.id,
            'subject': attachment.message.subject,
            'from_email': decode_email_header(attachment.message.from_email),
            'received_at': attachment.message.received_at
        }
    
    attachment_dict.update(pipeline_view(db,attachment))
    return AttachmentWithObject(**attachment_dict)

@router.get("/attachments/{attachment_id}/details")
async def get_attachment_details(attachment_id: UUID, db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    """Получить полную информацию о вложении включая результат проверок"""
    attachment = db.query(AttachmentModel).outerjoin(MessageModel).outerjoin(ObjectModel).filter(AttachmentModel.id == attachment_id).first()
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")
    
    details = {
        "id": attachment.id,
        "filename": attachment.filename,
        "file_path": attachment.file_path,
        "file_size": attachment.file_size,
        "status": attachment.status,
        "reject_reason": attachment.reject_reason,
        "validation_result": attachment.validation_result,
        "gpt_response": attachment.gpt_response,
        "sent_to_email": attachment.sent_to_email,
        "sent_at": attachment.sent_at,
        "created_at": attachment.created_at,
        "message": {
            "id": attachment.message.id,
            "subject": attachment.message.subject,
            "from_email": attachment.message.from_email,
            "received_at": attachment.message.received_at
        } if attachment.message else None,
        "object": {
            "id": attachment.object.id,
            "name": attachment.object.name,
            "address": attachment.object.address,
            "email": attachment.object.email
        } if attachment.object else None
    }
    details.update(pipeline_view(db,attachment))
    return details


@router.get("/attachments/{attachment_id}/deliveries")
def get_delivery_history(attachment_id: UUID, skip:int=Query(0,ge=0),
    limit:int=Query(100,ge=1,le=500), db:Session=Depends(get_db),
    current_user:UserModel=Depends(get_current_user)):
    if db.get(AttachmentModel,attachment_id) is None:
        raise HTTPException(status_code=404,detail="Attachment not found")
    return delivery_page(db,attachment_id,skip,limit)


@router.get('/attachments/{attachment_id}/delivery-events')
def get_delivery_events(attachment_id: UUID, delivery_id: UUID, skip: int = Query(0, ge=0),
                        limit: int = Query(25, ge=1, le=100), db: Session = Depends(get_db),
                        current_user: UserModel = Depends(require_admin)):
    return delivery_event_page(db, attachment_id, delivery_id, skip, limit)
