from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from typing import Optional, List, Literal
from uuid import UUID
from pydantic import BaseModel, EmailStr, StrictBool
import os

from backend.database import get_db
from common.models import TrustedEmail as TrustedEmailModel, Setting as SettingModel
from backend.routers.auth import require_admin
from common.models import User as UserModel

router = APIRouter()

class SMTPSelection(BaseModel):
    provider: Literal['brevo', 'timeweb']
    expected_provider: Literal['brevo', 'timeweb']
    confirmed: StrictBool

@router.get('/smtp-provider')
def get_smtp_provider(db: Session = Depends(get_db), current_user: UserModel = Depends(require_admin)):
    from common.smtp_settings import status
    return status(db)

@router.put('/smtp-provider')
def update_smtp_provider(value: SMTPSelection, db: Session = Depends(get_db), current_user: UserModel = Depends(require_admin)):
    from common.smtp_settings import set_provider
    from common.delivery import ServiceConflict
    if not value.confirmed:
        raise HTTPException(status_code=422, detail='Confirm the SMTP provider change')
    try:
        result=set_provider(db,value.provider,f'{current_user.id} ({current_user.username})',value.expected_provider)
        db.commit()
        return result
    except ServiceConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

class AISwitch(BaseModel):
    enabled: StrictBool

@router.get('/ai-fallback')
def get_ai_fallback(db: Session = Depends(get_db), current_user: UserModel = Depends(require_admin)):
    from common.ai_settings import status
    return status(db)

@router.put('/ai-fallback')
def update_ai_fallback(value: AISwitch, db: Session = Depends(get_db), current_user: UserModel = Depends(require_admin)):
    from common.ai_settings import set_enabled
    result=set_enabled(db,value.enabled,current_user.id)
    db.commit()
    return result


class TrustedEmailBase(BaseModel):
    email: EmailStr
    description: Optional[str] = None


class TrustedEmailCreate(TrustedEmailBase):
    pass


class TrustedEmailUpdate(BaseModel):
    email: Optional[EmailStr] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class TrustedEmailResponse(BaseModel):
    id: UUID
    email: str
    description: Optional[str]
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class SettingBase(BaseModel):
    key: str
    value: Optional[str] = None
    description: Optional[str] = None


class SettingCreate(SettingBase):
    pass


class SettingUpdate(BaseModel):
    value: Optional[str] = None
    description: Optional[str] = None


class SettingResponse(BaseModel):
    id: UUID
    key: str
    value: Optional[str]
    description: Optional[str]
    updated_at: datetime

    class Config:
        from_attributes = True


class EmailConfig(BaseModel):
    managed_by: str = "deployment_environment"
    editable: bool = False
    imap_server: Optional[str] = None
    imap_port: Optional[int] = None
    imap_ssl: Optional[bool] = None
    smtp_server: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_ssl: Optional[bool] = None
    email_username: Optional[str] = None
    email_from_name: Optional[str] = None


# Email config keys
EMAIL_CONFIG_KEYS = [
    'imap_server', 'imap_port', 'imap_ssl',
    'smtp_server', 'smtp_port', 'smtp_ssl',
    'email_username', 'email_from_name'
]


@router.get("/trusted-emails", response_model=List[TrustedEmailResponse])
async def list_trusted_emails(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(require_admin)
):
    """Список доверенных email (только админ)"""
    return db.query(TrustedEmailModel).offset(skip).limit(limit).all()


@router.post("/trusted-emails", response_model=TrustedEmailResponse)
async def create_trusted_email(
    email: TrustedEmailCreate,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(require_admin)
):
    existing = db.query(TrustedEmailModel).filter(TrustedEmailModel.email == email.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email уже добавлен")
    
    db_email = TrustedEmailModel(
        email=email.email,
        description=email.description
    )
    db.add(db_email)
    db.commit()
    db.refresh(db_email)
    return db_email


@router.put("/trusted-emails/{email_id}", response_model=TrustedEmailResponse)
async def update_trusted_email(
    email_id: UUID,
    email_update: TrustedEmailUpdate,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(require_admin)
):
    email = db.query(TrustedEmailModel).filter(TrustedEmailModel.id == email_id).first()
    if not email:
        raise HTTPException(status_code=404, detail="Email не найден")
    
    if email_update.email is not None:
        email.email = email_update.email
    if email_update.description is not None:
        email.description = email_update.description
    if email_update.is_active is not None:
        email.is_active = email_update.is_active
    
    db.commit()
    db.refresh(email)
    return email


@router.delete("/trusted-emails/{email_id}")
async def delete_trusted_email(
    email_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(require_admin)
):
    email = db.query(TrustedEmailModel).filter(TrustedEmailModel.id == email_id).first()
    if not email:
        raise HTTPException(status_code=404, detail="Email не найден")
    
    db.delete(email)
    db.commit()
    return {"message": "Email удален"}


@router.get("/settings", response_model=List[SettingResponse])
async def list_settings(
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(require_admin)
):
    """Список настроек (только админ)"""
    return db.query(SettingModel).all()


@router.get("/settings/{setting_key}", response_model=SettingResponse)
async def get_setting(
    setting_key: str,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(require_admin)
):
    setting = db.query(SettingModel).filter(SettingModel.key == setting_key).first()
    if not setting:
        raise HTTPException(status_code=404, detail="Настройка не найдена")
    return setting


@router.put("/settings/{setting_key}", response_model=SettingResponse)
async def update_setting(
    setting_key: str,
    setting_update: SettingUpdate,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(require_admin)
):
    if setting_key.lower().startswith(('ai_', 'routerai_', 'smtp_', 'brevo_')):
        raise HTTPException(status_code=422,detail='Use the typed settings endpoint; secrets belong in protected configuration')
    setting = db.query(SettingModel).filter(SettingModel.key == setting_key).first()
    if not setting:
        setting = SettingModel(key=setting_key)
        db.add(setting)
    
    if setting_update.value is not None:
        setting.value = setting_update.value
    if setting_update.description is not None:
        setting.description = setting_update.description
    
    db.commit()
    db.refresh(setting)
    return setting


@router.get("/email-config", response_model=EmailConfig)
async def get_email_config(
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(require_admin)
):
    return {"managed_by":"deployment_environment","editable":False,
            "imap_server":os.getenv("IMAP_SERVER"),"imap_port":int(os.getenv("IMAP_PORT","993")),
            "imap_ssl":True,"smtp_server":os.getenv("SMTP_SERVER"),
            "smtp_port":int(os.getenv("SMTP_PORT","587")),"smtp_ssl":os.getenv("SMTP_PORT","587")=="465",
            "email_username":os.getenv("SMTP_USER"),"email_from_name":None}



@router.put("/email-config", response_model=EmailConfig)
async def update_email_config(
    email_config: EmailConfig,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(require_admin)
):
    raise HTTPException(status_code=409,detail="Mail configuration is managed by the deployment environment")
