import smtplib
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import os
from typing import Optional, Dict
import re
from datetime import datetime, timezone, timedelta

# Moscow timezone
MOSCOW_TZ = timezone(timedelta(hours=3))
import sys

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from celery_app import celery_app, SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ADMIN_EMAIL, NOTIFY_EMAIL
from utils import logger
from database import SessionLocal
from models import Attachment, Object

def transliterate(text: str) -> str:
    """Транслитерация русских букв в латиницу"""
    translit_dict = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '',
        'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
        'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'Yo',
        'Ж': 'Zh', 'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L', 'М': 'M',
        'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U',
        'Ф': 'F', 'Х': 'H', 'Ц': 'Ts', 'Ч': 'Ch', 'Ш': 'Sh', 'Щ': 'Sch', 'Ъ': '',
        'Ы': 'Y', 'Ь': '', 'Э': 'E', 'Ю': 'Yu', 'Я': 'Ya'
    }
    result = ''
    for char in text:
        result += translit_dict.get(char, char)
    return result

class EmailSender:
    def __init__(self):
        self.smtp_server = SMTP_SERVER or "localhost"
        self.smtp_port = SMTP_PORT or 587
        self.username = SMTP_USER or ""
        self.password = SMTP_PASSWORD or ""
    
    def create_safe_filename(self, object_name: str, address: str) -> str:
        """Создать безопасное имя файла из объекта и адреса"""
        # Remove special characters and normalize
        safe_object = re.sub(r'[^\w\s]', '', object_name).strip()
        safe_address = re.sub(r'[^\w\s.,-]', '', address).strip()
        
        # Combine and limit length
        filename = f"{safe_object} — {safe_address}.pdf"
        
        # Limit to reasonable length
        if len(filename) > 100:
            filename = filename[:97] + "..."
        
        return filename or "document.pdf"
    
    def _find_object_for_attachment(self, db, attachment) -> Optional[Object]:
        """Найти объект для вложения: exact → partial → fallback по имени"""
        obj = None
        
        if not attachment.calculator_number:
            return None
        
        # 1. Exact match по calculator_number
        obj = db.query(Object).filter(
            Object.calculator_number == attachment.calculator_number,
            Object.is_active == True
        ).first()
        if obj:
            return obj
        
        # 2. Partial match (первые 3+ цифры)
        calc_num_digits = re.match(r'^\d+', attachment.calculator_number)
        if calc_num_digits and len(calc_num_digits.group()) >= 3:
            digits_only = calc_num_digits.group()
            candidates = db.query(Object).filter(
                Object.calculator_number.like(f"{digits_only}%"),
                Object.is_active == True
            ).all()
            valid_candidates = [c for c in candidates if c.calculator_number and c.email]
            if len(valid_candidates) == 1:
                obj = valid_candidates[0]
                logger.info(f"Found object by partial match (single match): {obj.name} ({obj.calculator_number})")
            elif len(valid_candidates) > 1:
                logger.warning(
                    f"Multiple objects match calculator {digits_only}%, not sending (require manual review): "
                    f"{[c.name for c in valid_candidates]}"
                )
                return None
        
        # 3. Fallback: если номер короткий (<3 цифр), ищем по object_name из PDF
        if not obj and len(attachment.calculator_number) < 3:
            obj_name_from_pdf = None
            if attachment.validation_result:
                obj_name_from_pdf = attachment.validation_result.get('object_name')
            if not obj_name_from_pdf and attachment.message:
                obj_name_from_pdf = attachment.message.parsed_object
            
            if obj_name_from_pdf:
                search_name = obj_name_from_pdf.lower().strip()
                candidates = db.query(Object).filter(
                    Object.name.ilike(f"%{search_name}%"),
                    Object.is_active == True
                ).all()
                
                if not candidates:
                    search_base = re.sub(r'№\s*\d+.*$', '', search_name).strip()
                    if search_base:
                        candidates = db.query(Object).filter(
                            Object.name.ilike(f"%{search_base}%"),
                            Object.is_active == True
                        ).all()
                
                # Match by address if we have address from PDF
                if candidates and attachment.validation_result:
                    addr_from_pdf = attachment.validation_result.get('address', '').lower()
                    if addr_from_pdf:
                        matching = []
                        for c in candidates:
                            if c.address:
                                c_addr = c.address.lower()
                                if any(part in c_addr for part in addr_from_pdf.split() if len(part) > 4):
                                    matching.append(c)
                        if matching:
                            candidates = matching
                
                valid_candidates = [c for c in candidates if c.calculator_number and c.email]
                if len(valid_candidates) == 1:
                    obj = valid_candidates[0]
                    logger.info(f"Found object by object_name fallback: {obj.name} ({obj.calculator_number})")
                elif len(valid_candidates) > 1:
                    logger.warning(
                        f"Multiple objects match name '{obj_name_from_pdf}', sending to admin: "
                        f"{[c.name for c in valid_candidates]}"
                    )
        
        return obj
    
    def _build_message(
        self,
        to_email: str,
        subject: str,
        body: str,
        pdf_path: str,
        pdf_filename: str
    ) -> MIMEMultipart:
        """Сформировать MIME-сообщение с PDF-вложением"""
        message = MIMEMultipart()
        message["From"] = self.username
        message["To"] = to_email
        message["Subject"] = subject
        message["Disposition-Notification-To"] = self.username
        message["X-Priority"] = "3"
        message["Message-ID"] = f"<{uuid.uuid4()}@email-sender>"
        
        message.attach(MIMEText(body, "plain", "utf-8"))
        
        if os.path.exists(pdf_path):
            with open(pdf_path, "rb") as f:
                part = MIMEBase("application", "pdf")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            filename_bytes = pdf_filename.encode('utf-8')
            encoded_filename = ''.join([f'%{b:02X}' for b in filename_bytes])
            part.add_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{encoded_filename}"
            )
            message.attach(part)
        else:
            raise Exception(f"PDF file not found: {pdf_path}")
        
        return message
    
    def _connect_smtp(self):
        """Установить SMTP-соединение и авторизоваться"""
        if self.smtp_port == 465:
            logger.info("[SMTP] Using SSL connection")
            server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, timeout=10)
        else:
            logger.info("[SMTP] Using STARTTLS connection")
            server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=10)
        
        logger.info("[SMTP] Connection established")
        
        if self.smtp_port != 465:
            logger.info("[SMTP] Starting TLS...")
            server.starttls()
            logger.info("[SMTP] TLS started")
        
        logger.info(f"[SMTP] Logging in as {self.username}")
        server.login(self.username, self.password)
        logger.info("[SMTP] Login successful")
        return server
    
    def send_email_with_pdf(
        self,
        to_email: str,
        subject: str,
        body: str,
        pdf_path: str,
        pdf_filename: str
    ) -> Dict:
        """Отправить email с PDF вложением (отдельное SMTP-соединение на каждый вызов)"""
        logger.info(f"[EMAIL] Starting send_email_with_pdf to {to_email}")
        server = None
        try:
            message = self._build_message(to_email, subject, body, pdf_path, pdf_filename)
            message_id = message["Message-ID"]
            
            server = self._connect_smtp()
            
            text = message.as_string()
            logger.info(f"[SMTP] Sending email to {to_email}...")
            server.sendmail(self.username, to_email, text)
            logger.info("[SMTP] Email sent successfully!")
            
            return {"status": "success", "to_email": to_email, "message_id": message_id}
            
        except Exception as e:
            logger.error(f"Error sending email to {to_email}: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            if server:
                try:
                    server.quit()
                    logger.info("[SMTP] Connection closed")
                except Exception:
                    pass
    
@celery_app.task(bind=True, max_retries=3)
def send_pdf_attachment(self, attachment_id: str):
    """Отправить PDF вложение найденному получателю"""
    logger.info(f"Starting to send attachment {attachment_id}")
    
    sender = EmailSender()
    db = SessionLocal()
    
    try:
        # Get attachment from DB
        attachment = db.query(Attachment).filter(Attachment.id == attachment_id).first()
        if not attachment:
            logger.error(f"Attachment {attachment_id} not found")
            return {'status': 'error', 'message': 'Attachment not found'}
        
        if attachment.status not in ['approved', 'rejected', 'validated']:
            logger.error(f"Attachment {attachment_id} cannot be sent (status: {attachment.status})")
            return {'status': 'error', 'message': f'Attachment status is {attachment.status}, cannot send'}
        
        # Check if linked object is inactive — skip sending entirely
        inactive_obj = None
        if attachment.calculator_number:
            inactive_obj = db.query(Object).filter(
                Object.calculator_number == attachment.calculator_number,
                Object.is_active == False
            ).first()
        if not inactive_obj and attachment.object_id:
            inactive_obj = db.query(Object).filter(
                Object.id == attachment.object_id,
                Object.is_active == False
            ).first()
        # Name-based fallback only when calculator_number is missing
        # (number/id checks above already cover known calculator_number)
        if not inactive_obj and not attachment.calculator_number:
            names_to_check = []
            if attachment.message and attachment.message.parsed_object:
                names_to_check.append(attachment.message.parsed_object)
            if attachment.validation_result and attachment.validation_result.get('object_name'):
                names_to_check.append(attachment.validation_result['object_name'])
            for raw_name in names_to_check:
                name_clean = re.split(r'\s+[Аа]дрес[:\s]|\s+[Уу]л[\.\s]|\s+[Гг][\.\s]', raw_name)[0].strip()
                if name_clean and len(name_clean) >= 5:
                    inactive_obj = db.query(Object).filter(
                        Object.name.ilike(f"%{name_clean}%"),
                        Object.is_active == False
                    ).first()
                    if inactive_obj:
                        break
        if inactive_obj:
            logger.info(f"Attachment {attachment_id} skipped: object {inactive_obj.name} is inactive")
            return {'status': 'skipped', 'reason': 'object_inactive'}
        
        # Safety: if status says approved but reject_reason is set, treat as rejected
        if attachment.reject_reason and attachment.status in ('approved', 'validated'):
            logger.warning(
                f"Attachment {attachment_id} has status={attachment.status} "
                f"but reject_reason='{attachment.reject_reason}', treating as rejected"
            )
            attachment.status = 'rejected'
            db.commit()
        
        # Determine recipients based on status
        recipients = []
        obj = None
        
        if attachment.status in ('approved', 'validated'):
            if attachment.calculator_number:
                obj = sender._find_object_for_attachment(db, attachment)
                
                if obj and obj.email:
                    email_list = [e.strip() for e in obj.email.split(',') if e.strip()]
                    recipients.extend(email_list)
                    logger.info(f"Attachment {attachment_id} approved, found object {obj.name}, sending to {email_list}")
            
            if not recipients:
                recipients = [ADMIN_EMAIL]
                logger.warning(f"Attachment {attachment_id} approved, object not found or no email, sending to {ADMIN_EMAIL}")
        else:
            recipients = [ADMIN_EMAIL]
            logger.info(f"Attachment {attachment_id} rejected, sending to admin: {ADMIN_EMAIL}")
        
        # Get object name and address (reuse found object or fallback to message)
        if obj:
            object_name = obj.name
            address = obj.address
        else:
            object_name = attachment.message.parsed_object if attachment.message else None
            address = attachment.message.parsed_address if attachment.message else None
        
        object_name = object_name or "Документ"
        address = address or "Без адреса"
        
        # Generate safe filename
        if attachment.sent_filename:
            safe_filename = attachment.sent_filename
        else:
            safe_filename = sender.create_safe_filename(object_name, address)
        
        # Prepare email based on status
        if attachment.status == 'rejected':
            empty_cells_info = []
            if attachment.validation_result and 'tables' in attachment.validation_result:
                tables_result = attachment.validation_result['tables']
                empty_cells_info = tables_result.get('errors', [])
            
            # Parse reject_reason into human-readable text
            reject_parts = []
            if attachment.reject_reason:
                for part in attachment.reject_reason.split(';'):
                    part = part.strip()
                    if part.startswith('empty_cells:'):
                        count = part.split(':', 1)[1]
                        reject_parts.append(f"• Пустые ячейки: {count} шт.")
                    elif part == 'dates_invalid':
                        reject_parts.append("• Нет валидных дат в документе")
                    elif part == 'validation_error':
                        reject_parts.append("• Ошибка при обработке PDF")
                    elif part == 'file_not_found':
                        reject_parts.append("• Файл PDF не найден")
                    elif part:
                        reject_parts.append(f"• {part}")
            
            reason_text = '\n'.join(reject_parts) if reject_parts else ''
            
            # Build subject based on reason
            if 'empty_cells' in (attachment.reject_reason or ''):
                subject = f"⚠️ ВНИМАНИЕ: Пропуски в данных - {object_name}"
            elif 'dates_invalid' in (attachment.reject_reason or ''):
                subject = f"⚠️ ОШИБКА: Нет дат в документе - {object_name}"
            else:
                subject = f"⚠️ ОШИБКА: {object_name}"
            
            body = f"""Здравствуйте!

Во вложении находится документ по объекту: {object_name}
Адрес: {address}

⚠️ ВНИМАНИЕ: В документе обнаружены проблемы!

Причина:
{reason_text}

Подробности ({len(empty_cells_info)}):
{chr(10).join(empty_cells_info[:10])}{'...' if len(empty_cells_info) > 10 else ''}

Дата получения: {datetime.now(MOSCOW_TZ).strftime('%d.%m.%Y %H:%M')}

---
Это автоматическое сообщение, пожалуйста, не отвечайте на него."""
        else:
            subject = f"Отчет {object_name} - {address}" if address != "Без адреса" else f"Отчет {object_name}"
            body = f"""Здравствуйте!

Мы запускаем автоматическую рассылку отчётов. Если вы обнаружили какие‑либо неточности в отчёте, пожалуйста, сообщите об этом:
    по электронной почте: {ADMIN_EMAIL}
    или по телефону: +7 902 318-86-89 Дмитрий.

Во вложении находится отчет:
Адрес: {address}

Дата получения сообщения: {datetime.now(MOSCOW_TZ).strftime('%d.%m.%Y %H:%M')}

---
Это автоматическое сообщение, пожалуйста, не отвечайте на него."""
        
        # Add NOTIFY_EMAIL for rejected attachments
        if attachment.status == 'rejected' and NOTIFY_EMAIL not in recipients:
            recipients.append(NOTIFY_EMAIL)
        
        # Send to each recipient with its own SMTP connection
        sent_to = []
        failed_to = []
        message_id = None
        
        for recipient in recipients:
            try:
                result = sender.send_email_with_pdf(
                    to_email=recipient,
                    subject=subject,
                    body=body.strip(),
                    pdf_path=attachment.file_path,
                    pdf_filename=safe_filename
                )
                
                if result['status'] == 'success':
                    sent_to.append(recipient)
                    message_id = result.get('message_id')
                    logger.info(f"Attachment {attachment_id} sent successfully to {recipient}")
                else:
                    failed_to.append(f"{recipient}: {result.get('message', 'unknown error')}")
                    logger.error(f"Failed to send attachment {attachment_id} to {recipient}: {result.get('message')}")
            except Exception as e:
                failed_to.append(f"{recipient}: {str(e)}")
                logger.error(f"Exception sending attachment {attachment_id} to {recipient}: {e}")
        
        # Update attachment status if at least one email was sent successfully
        if sent_to:
            if attachment.status != 'rejected':
                attachment.status = 'sent'
            attachment.sent_to_email = ', '.join(sent_to)
            attachment.sent_at = datetime.now(timezone.utc)
            attachment.original_message_id = message_id
            db.commit()
            
            return {
                'status': 'success',
                'recipient_email': ', '.join(sent_to),
                'filename': safe_filename,
                'failed': failed_to if failed_to else None
            }
        else:
            error_msg = '; '.join(failed_to) if failed_to else 'All sends failed'
            logger.error(f"Failed to send attachment {attachment_id} to all recipients: {error_msg}")
            
            if self.request.retries >= self.max_retries:
                attachment.status = 'rejected'
                attachment.reject_reason = 'send_error'
                db.commit()
            
            if self.request.retries < self.max_retries:
                raise self.retry(countdown=60 * (2 ** self.request.retries))
            
            return {'status': 'error', 'message': error_msg}
        
    except Exception as e:
        logger.error(f"Error sending attachment {attachment_id}: {e}")
        
        attachment = db.query(Attachment).filter(Attachment.id == attachment_id).first()
        if attachment:
            if self.request.retries >= self.max_retries:
                attachment.status = 'rejected'
                attachment.reject_reason = 'send_error'
                db.commit()
        
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=60 * (2 ** self.request.retries))
        
        return {'status': 'error', 'message': str(e)}
    
    finally:
        db.close()