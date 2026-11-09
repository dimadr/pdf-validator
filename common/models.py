from sqlalchemy import Column, Integer, BigInteger, String, DateTime, Text, Boolean, ForeignKey, Index, UniqueConstraint, CheckConstraint, text
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB
from datetime import datetime, timezone
import uuid

Base = declarative_base()

class Object(Base):
    __tablename__ = "objects"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    eldis_id = Column(String(50))  # ID из Элдис
    name = Column(String(255), nullable=False)
    name_norm = Column(String(255), nullable=False)
    calculator_number = Column(String(50), unique=True)
    address = Column(String(255))
    email = Column(String(255))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    object_date = Column(String(50), nullable=True)  # Период отчёта (например "23-24")
    
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    messages = relationship("IncomingMessage", back_populates="source")
    
    __table_args__ = (
        Index('idx_email_sources_active', 'is_active'),
    )

class IncomingMessage(Base):
    __tablename__ = "incoming_messages"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_message_id = Column(String(255), nullable=True)
    source_id = Column(UUID(as_uuid=True), ForeignKey("email_sources.id"), nullable=False)
    from_email = Column(String(255), nullable=False)
    subject = Column(Text)
    parsed_object = Column(String(255))
    parsed_address = Column(String(255))
    status = Column(String(50), default="new")  # new, processing, done, failed
    error_message = Column(Text)
    received_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    processed_at = Column(DateTime)
    
    imap_account = Column(String(255))
    imap_mailbox = Column(String(255))
    imap_uidvalidity = Column(BigInteger)
    imap_uid = Column(BigInteger)
    ingest_state = Column(String(32), default="legacy")
    part_manifest = Column(JSONB)
    raw_mime_path = Column(Text)

    source = relationship("EmailSource", back_populates="messages")
    attachments = relationship("Attachment", back_populates="message")
    
    __table_args__ = (
        UniqueConstraint('imap_account', 'imap_mailbox', 'imap_uidvalidity', 'imap_uid', name='uq_message_imap_identity'),
        Index('uq_legacy_message_provider_id', 'provider_message_id', unique=True,
              postgresql_where=text('imap_account IS NULL')),
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
    sent_filename = Column(String(255))
    file_path = Column(String(500))
    file_sha256 = Column(String(64), nullable=False, unique=True)
    file_size = Column(Integer)
    calculator_number = Column(String(50))
    status = Column(String(50), default="new")
    reject_reason = Column(String(100))
    validation_result = Column(JSONB)
    gpt_response = Column(JSONB)
    sent_to_email = Column(String(255))
    sent_at = Column(DateTime)
    read_receipt_received = Column(Boolean, default=False, server_default=text("false"))
    read_receipt_at = Column(DateTime)
    original_message_id = Column(String(255))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    current_revision_id = Column(UUID(as_uuid=True), ForeignKey("processing_revisions.id",
        name="fk_attachments_current_revision", use_alter=True), nullable=True)
    revisions = relationship("ProcessingRevision", back_populates="attachment", foreign_keys="ProcessingRevision.attachment_id")
    message = relationship("IncomingMessage", back_populates="attachments")
    object = relationship("Object", back_populates="attachments")
    
    __table_args__ = (
        Index('idx_attachments_sha256', 'file_sha256'),
        Index('idx_attachments_status', 'status'),
        Index('idx_attachments_message', 'message_id'),
        Index('idx_attachments_object', 'object_id'),
        Index('idx_attachments_created', 'created_at'),
        Index('idx_attachments_calculator', 'calculator_number'),
    )

class Report(Base):
    __tablename__ = "reports"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attachment_id = Column(UUID(as_uuid=True), ForeignKey("attachments.id"))
    report_type = Column(String(50))  # rejection, processing_error
    details = Column(JSONB)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    __table_args__ = (
        Index('idx_reports_type', 'report_type'),
        Index('idx_reports_attachment', 'attachment_id'),
        Index('idx_reports_created', 'created_at'),
    )


class User(Base):
    __tablename__ = "users"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))
    username = Column(String(50), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    email = Column(String(255))
    role = Column(String(20), default="operator", server_default=text("'operator'"))  # admin, operator
    is_active = Column(Boolean, default=True, server_default=text("true"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), server_default=text("now()"))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), server_default=text("now()"))
    
    __table_args__ = (
        Index('idx_users_username', 'username'),
        Index('idx_users_active', 'is_active'),
    )


class TrustedEmail(Base):
    __tablename__ = "trusted_emails"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))
    email = Column(String(255), nullable=False, unique=True)
    description = Column(String(255))
    is_active = Column(Boolean, default=True, server_default=text("true"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), server_default=text("now()"))
    
    __table_args__ = (
        Index('idx_trusted_emails_active', 'is_active'),
    )


class Setting(Base):
    __tablename__ = "settings"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))
    key = Column(String(100), nullable=False, unique=True)
    value = Column(Text)
    description = Column(String(255))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), server_default=text("now()"))

def utcnow():
    return datetime.now(timezone.utc)


class ProcessingRevision(Base):
    __tablename__ = "processing_revisions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attachment_id = Column(UUID(as_uuid=True), ForeignKey("attachments.id"), nullable=False, index=True)
    request_key = Column(String(255), nullable=False, unique=True)
    request_hash = Column(String(64), nullable=False)
    state = Column(String(32), nullable=False, default="received")
    content_sha256 = Column(String(64), nullable=False)
    file_path = Column(Text, nullable=False)
    calculator_number = Column(String(50))
    object_id = Column(UUID(as_uuid=True), ForeignKey("objects.id"))
    validation_result = Column(JSONB)
    reason = Column(Text)
    parser_version = Column(String(100))
    validation_attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    owner = Column(String(255))
    lease_until = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()"))
    finished_at = Column(DateTime(timezone=True))
    attachment = relationship("Attachment", back_populates="revisions", foreign_keys=[attachment_id])
    __table_args__ = (CheckConstraint("state IN ('received','extracting','validated','rejected','review_required','technical_error','discarded_inactive')", name="ck_revision_state"),)


class DeliveryIntent(Base):
    __tablename__ = "delivery_intents"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    revision_id = Column(UUID(as_uuid=True), ForeignKey("processing_revisions.id"), nullable=False, index=True)
    request_key = Column(String(255), nullable=False, unique=True)
    payload_hash = Column(String(64), nullable=False)
    kind = Column(String(32), nullable=False)
    payload = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()"))
    __table_args__ = (CheckConstraint("kind IN ('automatic','manual_resend','admin_notification')", name="ck_intent_kind"),)


class RecipientDelivery(Base):
    __tablename__ = "recipient_deliveries"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    intent_id = Column(UUID(as_uuid=True), ForeignKey("delivery_intents.id"), nullable=False)
    recipient = Column(String(320), nullable=False)
    original_address = Column(Text, nullable=False)
    role = Column(String(16), nullable=False)
    state = Column(String(16), nullable=False, default="pending", server_default=text("'pending'"))
    attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    error = Column(Text)
    error_code = Column(String(100))
    message_id = Column(String(255), nullable=False)
    smtp_provider = Column(String(16))
    smtp_response_code = Column(Integer)
    smtp_response = Column(Text)
    accepted_at = Column(DateTime(timezone=True))
    lease_until = Column(DateTime(timezone=True))
    owner = Column(String(255))
    retry_at = Column(DateTime(timezone=True))
    mime_date = Column(String(100), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow, server_default=text("now()"))
    __table_args__ = (
        UniqueConstraint("intent_id", "recipient", name="uq_delivery_intent_recipient"),
        CheckConstraint("role IN ('client','admin')", name="ck_delivery_role"),
        CheckConstraint("state IN ('pending','in_flight','accepted','failed','uncertain','cancelled')", name="ck_delivery_state"),
        CheckConstraint("attempts >= 0", name="ck_delivery_attempts"),
        Index("idx_delivery_ready", "state", "retry_at"), Index("idx_delivery_lease", "state", "lease_until"))


class SMTPProviderAudit(Base):
    __tablename__ = 'smtp_provider_audit'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor = Column(String(255), nullable=False)
    previous = Column(String(16), nullable=False)
    provider = Column(String(16), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=text('now()'))


class SMTPAttempt(Base):
    __tablename__ = 'smtp_attempts'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    delivery_id = Column(UUID(as_uuid=True), ForeignKey('recipient_deliveries.id'), nullable=False)
    attempt = Column(Integer, nullable=False)
    provider = Column(String(16), nullable=False)
    state = Column(String(16), nullable=False)
    response_code = Column(Integer)
    response = Column(Text)
    error_code = Column(String(100))
    started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=text('now()'))
    finished_at = Column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint('delivery_id', 'attempt', name='uq_smtp_attempt'),
        CheckConstraint("provider IN ('timeweb','brevo')", name='ck_smtp_attempt_provider'),
        CheckConstraint("state IN ('in_flight','accepted','failed','uncertain')", name='ck_smtp_attempt_state'),
        Index('idx_smtp_quota', 'provider', 'started_at'))


class BrevoDeliveryEvent(Base):
    __tablename__ = 'brevo_delivery_events'
    event_key = Column(String(64), primary_key=True)
    smtp_attempt_id = Column(UUID(as_uuid=True), ForeignKey('smtp_attempts.id'), nullable=False, index=True)
    provider_message_id = Column(String(255), nullable=False)
    event_type = Column(String(64), nullable=False)
    occurred_at = Column(DateTime(timezone=True))
    reason = Column(Text)
    processing_state = Column(String(32), nullable=False)
    payload = Column(JSONB, nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=text('now()'))


class BrevoPollCheckpoint(Base):
    __tablename__ = 'brevo_poll_checkpoints'
    smtp_attempt_id = Column(UUID(as_uuid=True), ForeignKey('smtp_attempts.id'), primary_key=True)
    next_poll_at = Column(DateTime(timezone=True), nullable=False, index=True)
    page_offset = Column(Integer, nullable=False, default=0, server_default=text('0'))
    window_end = Column(DateTime(timezone=True))
    last_polled_at = Column(DateTime(timezone=True))
    last_error = Column(String(64))


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    topic = Column(String(100), nullable=False)
    payload = Column(JSONB, nullable=False)
    available_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()"))
    published_at = Column(DateTime(timezone=True))
    attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    last_error = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()"))
    __table_args__ = (Index("idx_outbox_pending", "published_at", "available_at"),)


class MailboxCheckpoint(Base):
    __tablename__ = "mailbox_checkpoints"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account = Column(String(255), nullable=False)
    mailbox = Column(String(255), nullable=False)
    uidvalidity = Column(BigInteger, nullable=False)
    last_uid = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow, server_default=text("now()"))
    __table_args__ = (UniqueConstraint("account", "mailbox", name="uq_mailbox_account_folder"),)


class MailboxAction(Base):
    __tablename__ = 'mailbox_actions'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account = Column(String(255), nullable=False)
    mailbox = Column(String(255), nullable=False)
    uidvalidity = Column(BigInteger, nullable=False)
    uid = Column(BigInteger, nullable=False)
    destination = Column(String(255), nullable=False)
    raw_path = Column(Text, nullable=False)
    raw_sha256 = Column(String(64), nullable=False)
    message_id = Column(String(255))
    state = Column(String(16), nullable=False, default='pending',server_default=text("'pending'"))
    error = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow,server_default=text('now()'))
    finished_at = Column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('account','mailbox','uidvalidity','uid','destination',name='uq_mailbox_action'),
        CheckConstraint("state IN ('pending','done','uncertain')",name='ck_mailbox_action_state'))


class AIControl(Base):
    __tablename__ = 'ai_control'
    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default=text('false'))
    generation = Column(Integer, nullable=False, default=0, server_default=text('0'))
    enabled_at = Column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint('id = 1', name='ck_ai_singleton'),)


class AISettingAudit(Base):
    __tablename__ = 'ai_setting_audit'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor = Column(String(255), nullable=False)
    previous = Column(Boolean, nullable=False)
    enabled = Column(Boolean, nullable=False)
    generation = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=text('now()'))


class AIRequest(Base):
    __tablename__ = 'ai_requests'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    revision_id = Column(UUID(as_uuid=True), ForeignKey('processing_revisions.id'), nullable=False, index=True)
    cache_key = Column(String(64), nullable=False, unique=True)
    content_sha256 = Column(String(64), nullable=False)
    model = Column(String(150), nullable=False)
    parser_engine = Column(String(32), nullable=False)
    prompt_version = Column(String(100), nullable=False)
    state = Column(String(32), nullable=False)
    generation = Column(Integer, nullable=False)
    reason = Column(Text, nullable=False)
    result = Column(JSONB)
    usage = Column(JSONB)
    error_code = Column(String(100))
    elapsed_ms = Column(Integer)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=text('now()'), index=True)
    finished_at = Column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("state IN ('reserved','complete','failed','unknown')", name='ck_ai_request_state'),)


__all__ = ["MailboxAction", "SMTPAttempt", "SMTPProviderAudit", "AIControl", "AISettingAudit", "AIRequest", "Base", "Object", "EmailSource", "IncomingMessage", "Attachment", "Report",
           "User", "TrustedEmail", "Setting", "ProcessingRevision", "DeliveryIntent",
           "RecipientDelivery", "OutboxEvent", "MailboxCheckpoint", "utcnow"]
