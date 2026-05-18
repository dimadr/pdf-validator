from pydantic import BaseModel, EmailStr
from uuid import UUID
from datetime import datetime
from typing import Optional

class ObjectBase(BaseModel):
    eldis_id: Optional[str] = None
    name: str
    calculator_number: Optional[str] = None
    address: Optional[str] = None
    email: Optional[str] = None
    object_date: Optional[str] = None

class ObjectCreate(ObjectBase):
    pass

class ObjectUpdate(BaseModel):
    eldis_id: Optional[str] = None
    name: Optional[str] = None
    calculator_number: Optional[str] = None
    address: Optional[str] = None
    email: Optional[str] = None
    is_active: Optional[bool] = None
    object_date: Optional[str] = None

class Object(ObjectBase):
    id: UUID
    name_norm: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class EmailSourceBase(BaseModel):
    email: EmailStr
    name: Optional[str] = None

class EmailSourceCreate(EmailSourceBase):
    pass

class EmailSourceUpdate(BaseModel):
    email: Optional[EmailStr] = None
    name: Optional[str] = None
    is_active: Optional[bool] = None

class EmailSource(EmailSourceBase):
    id: UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
