from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional

from database import get_db
from models import EmailSource as EmailSourceModel
from schemas import EmailSource, EmailSourceCreate, EmailSourceUpdate
from routers.auth import get_current_user, require_admin
from models import User as UserModel

router = APIRouter()

@router.post("/", response_model=EmailSource)
async def create_email_source(source: EmailSourceCreate, db: Session = Depends(get_db), current_user: UserModel = Depends(require_admin)):
    """Добавить новый разрешенный email (только админ)"""
    # Check if email already exists
    existing = db.query(EmailSourceModel).filter(EmailSourceModel.email == source.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already exists")
    
    db_source = EmailSourceModel(
        email=source.email,
        name=source.name
    )
    db.add(db_source)
    db.commit()
    db.refresh(db_source)
    return db_source

@router.get("/", response_model=List[EmailSource])
async def list_email_sources(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    is_active: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Получить список разрешенных email"""
    query = db.query(EmailSourceModel)
    
    if is_active is not None:
        query = query.filter(EmailSourceModel.is_active == is_active)
    
    if search:
        query = query.filter(
            EmailSourceModel.email.contains(search) |
            EmailSourceModel.name.contains(search)
        )
    
    return query.offset(skip).limit(limit).all()

@router.get("/{source_id}", response_model=EmailSource)
async def get_email_source(source_id: str, db: Session = Depends(get_db)):
    """Получить источник по ID"""
    source = db.query(EmailSourceModel).filter(EmailSourceModel.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Email source not found")
    return source

@router.put("/{source_id}", response_model=EmailSource)
async def update_email_source(
    source_id: str,
    source_update: EmailSourceUpdate,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(require_admin)
):
    """Обновить источник (только админ)"""
    source = db.query(EmailSourceModel).filter(EmailSourceModel.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Email source not found")
    
    update_data = source_update.dict(exclude_unset=True)
    
    if "email" in update_data:
        # Check if new email conflicts
        existing = db.query(EmailSourceModel).filter(
            EmailSourceModel.email == update_data["email"],
            EmailSourceModel.id != source_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Email already exists")
    
    for field, value in update_data.items():
        setattr(source, field, value)
    
    db.commit()
    db.refresh(source)
    return source

@router.delete("/{source_id}")
async def delete_email_source(source_id: str, db: Session = Depends(get_db), current_user: UserModel = Depends(require_admin)):
    """Удалить источник (только админ)"""
    source = db.query(EmailSourceModel).filter(EmailSourceModel.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Email source not found")
    db.delete(source)
    db.commit()
    return {"message": "Email source deleted"}

@router.post("/{source_id}/disable")
async def disable_email_source(source_id: str, db: Session = Depends(get_db), current_user: UserModel = Depends(require_admin)):
    """Отключить источник (мягкое удаление) (только админ)"""
    source = db.query(EmailSourceModel).filter(EmailSourceModel.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Email source not found")
    
    source.is_active = False
    db.commit()
    db.refresh(source)
    return source