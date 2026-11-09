from pydantic import BaseModel, EmailStr, Field, validator
from email_validator import validate_email, EmailNotValidError
from uuid import UUID
from datetime import datetime
from typing import Optional

def _object_name(value):
    if value is None or not value.strip():
        raise ValueError("Object name must not be blank")
    return value


def _not_null(value):
    if value is None:
        raise ValueError('This field cannot be null; omit it to leave it unchanged')
    return value


def _recipient_list(value):
    if value is None or not value.strip():
        return None
    parts = [item.strip() for item in value.split(',')]
    if any(not item for item in parts):
        raise ValueError("Recipient list contains an empty address")
    try:
        normalized = [validate_email(item, check_deliverability=False).normalized for item in parts]
    except EmailNotValidError as exc:
        raise ValueError(str(exc)) from exc
    result = ', '.join(normalized)
    if len(result) > 255:
        raise ValueError("Recipient list exceeds 255 characters")
    return result


class ObjectBase(BaseModel):
    eldis_id: Optional[str] = None
    name: str
    calculator_number: Optional[str] = None
    address: Optional[str] = None
    email: Optional[str] = None
    object_date: Optional[str] = None

class ObjectCreate(ObjectBase):
    eldis_id: Optional[str] = Field(None, max_length=50)
    name: str = Field(..., min_length=1, max_length=255)
    calculator_number: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = Field(None, max_length=255)
    email: Optional[str] = Field(None, max_length=255)
    object_date: Optional[str] = Field(None, max_length=50)

    _validate_name = validator('name', allow_reuse=True)(_object_name)
    _validate_recipients = validator('email', allow_reuse=True)(_recipient_list)

class ObjectUpdate(BaseModel):
    eldis_id: Optional[str] = Field(None, max_length=50)
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    calculator_number: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = Field(None, max_length=255)
    email: Optional[str] = Field(None, max_length=255)
    is_active: Optional[bool] = None
    object_date: Optional[str] = Field(None, max_length=50)

    _validate_name = validator('name', allow_reuse=True)(_object_name)
    _validate_recipients = validator('email', allow_reuse=True)(_recipient_list)
    _validate_active = validator('is_active', allow_reuse=True)(_not_null)

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
    name: Optional[str] = Field(None, max_length=255)

class EmailSourceCreate(EmailSourceBase):
    pass

class EmailSourceUpdate(BaseModel):
    email: Optional[EmailStr] = None
    name: Optional[str] = Field(None, max_length=255)
    is_active: Optional[bool] = None

    _validate_nonnull = validator('email', 'is_active', allow_reuse=True)(_not_null)

class EmailSource(EmailSourceBase):
    # Read stored history verbatim (including inactive synthetic canaries).
    # Create/update inputs still use EmailStr and reject reserved domains.
    email: str = Field(..., max_length=255)
    id: UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
