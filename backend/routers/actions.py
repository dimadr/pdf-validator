"""Manual commands use the same revision/outbox transaction as automatic work."""
import hashlib
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Body
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.database import get_db
from common.models import Attachment, DeliveryIntent, User
from common.config import PipelineUnavailable, require_schema
from common.delivery import ServiceConflict
from common.service import request_reprocess, request_resend
from backend.routers.auth import require_admin, get_current_user
from backend.pipeline_views import pipeline_view

router = APIRouter()


class ActionRequest(BaseModel):
    expected_revision_id: Optional[UUID] = None


class TaskResponse(BaseModel):
    message: str
    attachment_id: UUID
    task_id: Optional[str] = None
    revision_id: UUID
    intent_id: Optional[UUID] = None
    state: str


def _result(item, attachment_id):
    intent = isinstance(item, DeliveryIntent)
    return TaskResponse(message="Command committed; durable dispatch pending",
        attachment_id=attachment_id, revision_id=item.revision_id if intent else item.id,
        intent_id=item.id if intent else None, state="queued")


def _execute(db, operation, attachment_id, request_key, expected_revision_id):
    require_schema(db)
    if not request_key or len(request_key)>200:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required (maximum 200 characters)")
    if db.get(Attachment, attachment_id) is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    call = request_reprocess if operation=="reprocess" else request_resend
    return call(db, attachment_id, request_key, expected_revision_id)


def _single(db, operation, attachment_id, request_key, body):
    try:
        item=_execute(db, operation, attachment_id, request_key, body.expected_revision_id if body else None)
        db.commit()
        return _result(item, attachment_id)
    except PipelineUnavailable as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ServiceConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Command transaction failed; retry the same Idempotency-Key") from exc


@router.post("/{attachment_id}/reprocess", response_model=TaskResponse, status_code=202)
def reprocess_attachment(attachment_id: UUID, body: Optional[ActionRequest]=Body(None),
    request_key: Optional[str]=Header(None, alias="Idempotency-Key"),
    db: Session=Depends(get_db), current_user: User=Depends(require_admin)):
    return _single(db,"reprocess",attachment_id,request_key,body)


@router.post("/{attachment_id}/resend", response_model=TaskResponse, status_code=202)
def resend_attachment(attachment_id: UUID, body: Optional[ActionRequest]=Body(None),
    request_key: Optional[str]=Header(None, alias="Idempotency-Key"),
    db: Session=Depends(get_db), current_user: User=Depends(require_admin)):
    return _single(db,"resend",attachment_id,request_key,body)


def _batch(db, operation, ids, key):
    results=[]
    try:
        require_schema(db)
        if not key or len(key)>200:
            raise HTTPException(status_code=422,detail="Idempotency-Key is required (maximum 200 characters)")
        # Stable lock order, one operation per distinct ID.
        for aid in sorted(set(ids),key=str):
            item_key=hashlib.sha256((key+":"+operation+":"+str(aid)).encode()).hexdigest()
            try:
                with db.begin_nested():
                    item=_execute(db,operation,aid,item_key,None)
                    db.flush()
                    results.append({"attachment_id":str(aid),"status":202,**_result(item,aid).model_dump(mode="json")})
            except (ServiceConflict,HTTPException) as exc:
                results.append({"attachment_id":str(aid),"status":exc.status_code if isinstance(exc,HTTPException) else 409,
                                "error":exc.detail if isinstance(exc,HTTPException) else str(exc)})
        db.commit()
        processed=sum(r["status"]==202 for r in results)
        return {"processed":processed,"errors":len(results)-processed,"total":len(ids),
                "unique":len(results),"duplicate_ids":len(ids)-len(results),"results":results}
    except PipelineUnavailable as exc:
        db.rollback()
        raise HTTPException(status_code=503,detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503,detail="Batch not committed; retry the same Idempotency-Key") from exc


@router.post("/batch-reprocess",status_code=202)
def batch_reprocess(attachment_ids:list[UUID],request_key:Optional[str]=Header(None,alias="Idempotency-Key"),
    db:Session=Depends(get_db),current_user:User=Depends(require_admin)):
    return _batch(db,"reprocess",attachment_ids,request_key)


@router.post("/batch-resend",status_code=202)
def batch_resend(attachment_ids:list[UUID],request_key:Optional[str]=Header(None,alias="Idempotency-Key"),
    db:Session=Depends(get_db),current_user:User=Depends(require_admin)):
    return _batch(db,"resend",attachment_ids,request_key)


@router.get("/{attachment_id}/status")
def get_attachment_status(attachment_id:UUID,db:Session=Depends(get_db),
    current_user:User=Depends(get_current_user)):
    attachment=db.get(Attachment,attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404,detail="Attachment not found")
    return {"id":attachment.id,"status":attachment.status,"reject_reason":attachment.reject_reason,
            "sent_to_email":attachment.sent_to_email,"sent_at":attachment.sent_at,
            "updated_at":attachment.updated_at,**pipeline_view(db,attachment)}
