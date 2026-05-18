from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from typing import Optional, List
from uuid import UUID
from pydantic import BaseModel, EmailStr

from database import get_db
from models import TrustedEmail as TrustedEmailModel, Setting as SettingModel
from routers.auth import require_admin
from models import User as UserModel

router = APIRouter()


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


class AIConfig(BaseModel):
    router_ai_url: Optional[str] = None
    router_ai_key: Optional[str] = None
    neuro_api_url: Optional[str] = None
    neuro_api_key: Optional[str] = None


class EmailConfig(BaseModel):
    imap_server: Optional[str] = None
    imap_port: Optional[int] = None
    imap_ssl: Optional[bool] = None
    smtp_server: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_ssl: Optional[bool] = None
    email_username: Optional[str] = None
    email_password: Optional[str] = None
    email_from_name: Optional[str] = None


# Email config keys
EMAIL_CONFIG_KEYS = [
    'imap_server', 'imap_port', 'imap_ssl',
    'smtp_server', 'smtp_port', 'smtp_ssl',
    'email_username', 'email_password', 'email_from_name'
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


@router.get("/ai-config", response_model=AIConfig)
async def get_ai_config(
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(require_admin)
):
    config = {}
    keys = ["router_ai_url", "router_ai_key", "neuro_api_url", "neuro_api_key"]
    for key in keys:
        setting = db.query(SettingModel).filter(SettingModel.key == key).first()
        if setting:
            config[key] = setting.value
    return config


@router.put("/ai-config", response_model=AIConfig)
async def update_ai_config(
    ai_config: AIConfig,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(require_admin)
):
    config = {}
    keys = {
        "router_ai_url": ai_config.router_ai_url,
        "router_ai_key": ai_config.router_ai_key,
        "neuro_api_url": ai_config.neuro_api_url,
        "neuro_api_key": ai_config.neuro_api_key,
    }
    
    for key, value in keys.items():
        setting = db.query(SettingModel).filter(SettingModel.key == key).first()
        if setting:
            setting.value = value
        else:
            setting = SettingModel(key=key, value=value)
            db.add(setting)
    
    db.commit()
    
    for key in keys:
        setting = db.query(SettingModel).filter(SettingModel.key == key).first()
        if setting:
            config[key] = setting.value
    
    return config


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
    config = {}
    for key in EMAIL_CONFIG_KEYS:
        setting = db.query(SettingModel).filter(SettingModel.key == key).first()
        if setting and setting.value is not None:
            # Convert string booleans to bool
            if key in ('imap_ssl', 'smtp_ssl'):
                config[key] = setting.value.lower() in ('true', '1', 'yes')
            elif key in ('imap_port', 'smtp_port'):
                config[key] = int(setting.value)
            else:
                config[key] = setting.value
    return config


@router.put("/email-config", response_model=EmailConfig)
async def update_email_config(
    email_config: EmailConfig,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(require_admin)
):
    keys = {
        "imap_server": email_config.imap_server,
        "imap_port": str(email_config.imap_port) if email_config.imap_port else None,
        "imap_ssl": str(email_config.imap_ssl).lower() if email_config.imap_ssl is not None else None,
        "smtp_server": email_config.smtp_server,
        "smtp_port": str(email_config.smtp_port) if email_config.smtp_port else None,
        "smtp_ssl": str(email_config.smtp_ssl).lower() if email_config.smtp_ssl is not None else None,
        "email_username": email_config.email_username,
        "email_password": email_config.email_password,
        "email_from_name": email_config.email_from_name,
    }
    
    for key, value in keys.items():
        setting = db.query(SettingModel).filter(SettingModel.key == key).first()
        if setting:
            setting.value = value
        else:
            setting = SettingModel(key=key, value=value)
            db.add(setting)
    
    db.commit()
    return email_config
