from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from uuid import UUID
from typing import List, Optional
import re

from backend.database import get_db
from common.models import Object as ObjectModel, User as UserModel, Attachment, ProcessingRevision, DeliveryIntent, RecipientDelivery
from backend.schemas import Object, ObjectCreate, ObjectUpdate
from backend.routers.auth import get_current_user, require_admin

router = APIRouter()

def normalize_name(name: str) -> str:
    """Нормализация имени объекта для поиска/сравнения"""
    return re.sub(r'[^\w\s]', '', name.lower().strip())

@router.post("/", response_model=Object)
async def create_object(obj: ObjectCreate, db: Session = Depends(get_db), current_user: UserModel = Depends(require_admin)):
    """Создать новый объект (только админ)"""
    name_norm = normalize_name(obj.name)
    
    db_obj = ObjectModel(
        name=obj.name,
        name_norm=name_norm,
        eldis_id=obj.eldis_id,
        object_date=obj.object_date,
        calculator_number=obj.calculator_number,
        address=obj.address,
        email=obj.email
    )
    db.add(db_obj)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409,detail="Object conflicts with existing records") from exc
    db.refresh(db_obj)
    return db_obj

@router.get("/", response_model=List[Object])
async def list_objects(
    response: Response,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    search: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    period: Optional[str] = Query(None, description="Filter by period (e.g. '23-24')"),
    sort_by: Optional[str] = Query(None, description="Sort field: name, eldis_id, object_date, calculator_number, created_at"),
    sort_order: Optional[str] = Query("asc", regex="^(asc|desc)$"),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user)
):
    """Получить список объектов"""
    query = db.query(ObjectModel)
    
    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            or_(
                ObjectModel.name.ilike(search_pattern),
                ObjectModel.eldis_id.ilike(search_pattern),
                ObjectModel.calculator_number.ilike(search_pattern),
                ObjectModel.address.ilike(search_pattern),
                ObjectModel.email.ilike(search_pattern)
            )
        )
    
    if is_active is not None:
        query = query.filter(ObjectModel.is_active == is_active)
    
    if period:
        query = query.filter(ObjectModel.object_date.ilike(f"%{period}%"))
    
    if sort_by and sort_by not in {'name','eldis_id','object_date','calculator_number','created_at'}:
        raise HTTPException(status_code=422,detail='Unsupported sort field')
    sort_column = getattr(ObjectModel, sort_by) if sort_by else ObjectModel.created_at
    if sort_order == "desc":
        query = query.order_by(sort_column.desc())
    else:
        query = query.order_by(sort_column.asc())
    
    response.headers["X-Total-Count"]=str(query.count())
    return query.offset(skip).limit(limit).all()

@router.get("/{object_id}", response_model=Object)
async def get_object(object_id: UUID, db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    """Получить объект по ID"""
    obj = db.query(ObjectModel).filter(ObjectModel.id == object_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")
    return obj

@router.put("/{object_id}", response_model=Object)
async def update_object(
    object_id: UUID,
    obj_update: ObjectUpdate, 
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(require_admin)
):
    """Обновить объект (только админ)"""
    obj = db.query(ObjectModel).filter(ObjectModel.id == object_id).with_for_update().populate_existing().first()
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")
    
    update_data = obj_update.dict(exclude_unset=True)

    # Claim also locks Object. Lock before checking in-flight to serialize the
    # active decision with deactivation; changing email never rewrites an intent.
    if 'is_active' in update_data and not update_data['is_active']:
        in_flight=db.query(RecipientDelivery.id).join(DeliveryIntent,RecipientDelivery.intent_id==DeliveryIntent.id)\
            .join(ProcessingRevision,DeliveryIntent.revision_id==ProcessingRevision.id)\
            .filter(ProcessingRevision.object_id==obj.id,RecipientDelivery.state=='in_flight').first()
        if in_flight:
            raise HTTPException(status_code=409,detail='Wait for in-flight delivery before deactivating the object')
    
    if "name" in update_data:
        update_data["name_norm"] = normalize_name(update_data["name"])
    
    for field, value in update_data.items():
        setattr(obj, field, value)
    
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409,detail="Object conflicts with existing records") from exc
    db.refresh(obj)
    return obj

@router.delete("/{object_id}")
async def delete_object(object_id: UUID, db: Session = Depends(get_db), current_user: UserModel = Depends(require_admin)):
    """Удалить объект (только админ)"""
    obj = db.query(ObjectModel).filter(ObjectModel.id == object_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")
    
    if db.query(Attachment.id).filter(Attachment.object_id==obj.id).first() or db.query(ProcessingRevision.id).filter(ProcessingRevision.object_id==obj.id).first():
        raise HTTPException(status_code=409,detail="Object has processing history; deactivate it instead")
    db.delete(obj)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409,detail="Object conflicts with existing records") from exc
    return {"message": "Object deleted successfully"}
