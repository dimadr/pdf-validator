from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, Boolean, ForeignKey, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB
from datetime import datetime
import uuid

Base = declarative_base()

class Object(Base):
    __tablename__ = "objects"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    name_norm = Column(String(255), nullable=False, unique=True)
    calculator_number = Column(String(50), unique=True, index=True)
    address = Column(String(255))
    email = Column(String(255))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    attachments = relationship("Attachment", back_populates="object")
    
    __table_args__ = (
        Index('idx_objects_name_norm', 'name_norm'),
        Index('idx_objects_calculator', 'calculator_number'),
        Index('idx_objects_active', 'is_active'),
    )

class EmailSource(Base):
    __tablename__ = "email_sources"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), nullable=False, unique=True)
    name = Column(String(255))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    messages = relationship("IncomingMessage", back_populates="source")
    
    __table_args__ = (
        Index('idx_email_sources_active', 'is_active'),
    )

class IncomingMessage(Base):
    __tablename__ = "incoming_messages"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_message_id = Column(String(255), nullable=False, unique=True)
    source_id = Column(UUID(as_uuid=True), ForeignKey("email_sources.id"), nullable=False)
    from_email = Column(String(255), nullable=False)
    subject = Column(Text)
    parsed_object = Column(String(255))
    parsed_address = Column(String(255))
    status = Column(String(50), default="new")  # new, processing, done, failed
    error_message = Column(Text)
    received_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime)
    
    source = relationship("EmailSource", back_populates="messages")
    attachments = relationship("Attachment", back_populates="message")
    
    __table_args__ = (
        Index('idx_messages_provider_id', 'provider_message_id'),
        Index('idx_messages_status', 'status'),
        Index('idx_messages_source', 'source_id'),
        Index('idx_messages_received', 'received_at'),
    )

class Attachment(Base):
    __tablename__ = "attachments"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id = Column(UUID(as_uuid=True), ForeignKey("incoming_messages.id"), nullable=False)
    object_id = Column(UUID(as_uuid=True), ForeignKey("objects.id"))
    filename = Column(String(255), nullable=False)
    file_path = Column(String(500))
    file_sha256 = Column(String(64), nullable=False, unique=True)
    file_size = Column(Integer)
    calculator_number = Column(String(50), index=True)
    status = Column(String(50), default="new")  # new, processing, validated, sent, rejected
    reject_reason = Column(String(100))  # subject_parse, dates, tables, no_recipient, send_error, other
    validation_result = Column(JSONB)
    gpt_response = Column(JSONB)
    sent_to_email = Column(String(255))
    sent_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    message = relationship("IncomingMessage", back_populates="attachments")
    object = relationship("Object", back_populates="attachments")
    
    __table_args__ = (
        Index('idx_attachments_sha256', 'file_sha256'),
        Index('idx_attachments_status', 'status'),
        Index('idx_attachments_message', 'message_id'),
        Index('idx_attachments_object', 'object_id'),
        Index('idx_attachments_created', 'created_at'),
    )

class Report(Base):
    __tablename__ = "reports"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attachment_id = Column(UUID(as_uuid=True), ForeignKey("attachments.id"))
    report_type = Column(String(50))  # rejection, processing_error
    details = Column(JSONB)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        Index('idx_reports_type', 'report_type'),
        Index('idx_reports_attachment', 'attachment_id'),
        Index('idx_reports_created', 'created_at'),
    )