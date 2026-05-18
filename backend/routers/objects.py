from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Optional
import re

from database import get_db
from models import Object as ObjectModel
from schemas import Object, ObjectCreate, ObjectUpdate
from routers.auth import get_current_user, require_admin
from models import User as UserModel

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
    db.commit()
    db.refresh(db_obj)
    return db_obj

@router.get("/", response_model=List[Object])
async def list_objects(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    search: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    period: Optional[str] = Query(None, description="Filter by period (e.g. '23-24')"),
    sort_by: Optional[str] = Query(None, description="Sort field: name, eldis_id, object_date, calculator_number, created_at"),
    sort_order: Optional[str] = Query("asc", regex="^(asc|desc)$"),
    db: Session = Depends(get_db)
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
    
    sort_column = getattr(ObjectModel, sort_by, None) if sort_by else ObjectModel.created_at
    if sort_order == "desc":
        query = query.order_by(sort_column.desc())
    else:
        query = query.order_by(sort_column.asc())
    
    return query.offset(skip).limit(limit).all()

@router.get("/{object_id}", response_model=Object)
async def get_object(object_id: str, db: Session = Depends(get_db)):
    """Получить объект по ID"""
    obj = db.query(ObjectModel).filter(ObjectModel.id == object_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")
    return obj

@router.put("/{object_id}", response_model=Object)
async def update_object(
    object_id: str, 
    obj_update: ObjectUpdate, 
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(require_admin)
):
    """Обновить объект (только админ)"""
    obj = db.query(ObjectModel).filter(ObjectModel.id == object_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")
    
    update_data = obj_update.dict(exclude_unset=True)
    
    if "name" in update_data:
        update_data["name_norm"] = normalize_name(update_data["name"])
    
    for field, value in update_data.items():
        setattr(obj, field, value)
    
    db.commit()
    db.refresh(obj)
    return obj

@router.delete("/{object_id}")
async def delete_object(object_id: str, db: Session = Depends(get_db), current_user: UserModel = Depends(require_admin)):
    """Удалить объект (только админ)"""
    obj = db.query(ObjectModel).filter(ObjectModel.id == object_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")
    
    db.delete(obj)
    db.commit()
    return {"message": "Object deleted successfully"}