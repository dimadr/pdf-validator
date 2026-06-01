import imaplib
import email
import os
import hashlib
import re
from email.header import decode_header
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

from celery_app import celery_app, IMAP_SERVER, IMAP_PORT, IMAP_USER, IMAP_PASSWORD, UPLOAD_DIR
from database import SessionLocal
from utils import logger
from models import IncomingMessage, Attachment, EmailSource

class IMAPClient:
    def __init__(self):
        self.server = IMAP_SERVER
        self.port = IMAP_PORT
        self.username = IMAP_USER
        self.password = IMAP_PASSWORD
        self.connection = None
    
    def connect(self):
        try:
            self.connection = imaplib.IMAP4_SSL(self.server, self.port)
            self.connection.login(self.username, self.password)
            logger.info("Connected to IMAP server")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to IMAP: {e}")
            return False
    
    def disconnect(self):
        if self.connection:
            try:
                self.connection.logout()
                logger.info("Disconnected from IMAP server")
            except:
                pass
            self.connection = None
    
    def get_new_messages(self, days_back: int = 1) -> List[Dict]:
        if not self.connection:
            raise Exception("Not connected to IMAP server")
        
        try:
            self.connection.select('INBOX')
            since_date = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")
            search_criteria = f'(SINCE {since_date})'
            
            status, messages = self.connection.search(None, search_criteria)
            if status != 'OK':
                return []
            
            message_ids = messages[0].split()
            logger.info(f"Found {len(message_ids)} messages")
            
            new_messages = []
            for msg_id in message_ids:
                try:
                    message_data = self._fetch_message(msg_id)
                    if message_data:
                        new_messages.append(message_data)
                except Exception as e:
                    logger.error(f"Error fetching message {msg_id}: {e}")
                    continue
            
            return new_messages
            
        except Exception as e:
            logger.error(f"Error getting messages: {e}")
            return []
    
    def _fetch_message(self, msg_id: bytes) -> Optional[Dict]:
        try:
            status, msg_data = self.connection.fetch(msg_id, '(RFC822)')
            if status != 'OK':
                return None
            
            raw_email = msg_data[0][1]
            email_message = email.message_from_bytes(raw_email)
            
            message_id = self._get_header(email_message, 'Message-ID')
            subject = self._decode_header(self._get_header(email_message, 'Subject'))
            from_email = self._decode_header(self._get_header(email_message, 'From'))
            date_str = self._get_header(email_message, 'Date')
            
            received_at = None
            if date_str:
                try:
                    received_at = email.utils.parsedate_to_datetime(date_str)
                except:
                    received_at = datetime.now(timezone.utc)
            
            # Check if this is a read receipt (MDN)
            content_type = email_message.get_content_type()
            is_mdn = False
            original_message_id = None
            
            # Handle multipart/report (MDN/disposition-notification)
            if content_type == 'multipart/report':
                original_message_id = self._get_header(email_message, 'Original-Message-ID')
                if original_message_id:
                    is_mdn = True
                    logger.info(f"Found MDN for message: {original_message_id}")
                else:
                    # Try to find in body
                    for part in email_message.walk():
                        if part.get_content_type() == 'message/disposition-notification':
                            original_message_id = part.get('Original-Message-ID')
                            if original_message_id:
                                is_mdn = True
                                logger.info(f"Found MDN in body for message: {original_message_id}")
                                break
            
            # Also check for read receipts by subject (Timeweb sends simple emails)
            # Use already decoded subject from line 84
            if not is_mdn and ('Прочтено' in subject or 'Read:' in subject or 'прочитано' in subject.lower()):
                # Try In-Reply-To or References headers
                original_message_id = self._get_header(email_message, 'In-Reply-To')
                if not original_message_id:
                    original_message_id = self._get_header(email_message, 'References')
                if original_message_id:
                    is_mdn = True
                    logger.info(f"Found read receipt via subject, original message: {original_message_id}")
            
            # Extract PDF attachments
            logger.info(f"Processing email: subject='{subject}', parts count: {len(list(email_message.walk()))}")
            attachments = self._extract_attachments(email_message)
            logger.info(f"Found {len(attachments)} PDF attachments")
            
            return {
                'provider_message_id': message_id,
                'from_email': from_email,
                'subject': subject,
                'received_at': received_at,
                'raw_content': raw_email,
                'attachments': attachments,
                'is_mdn': is_mdn,
                'original_message_id': original_message_id
            }
            
        except Exception as e:
            logger.error(f"Error fetching message {msg_id}: {e}")
            return None
    
    def _extract_attachments(self, email_message) -> List[Dict]:
        """Извлечь PDF вложения из письма"""
        attachments = []
        
        for part in email_message.walk():
            filename = part.get_filename()
            
            # Decode filename if encoded
            if filename:
                filename = self._decode_header(filename)
            
            # Check for PDF attachments - check by filename extension
            if filename and filename.lower().endswith('.pdf'):
                content = part.get_payload(decode=True)
                if content:
                    file_hash = hashlib.sha256(content).hexdigest()
                    attachments.append({
                        'filename': filename,
                        'content': content,
                        'file_size': len(content),
                        'file_sha256': file_hash
                    })
                    logger.info(f"Extracted PDF attachment: {filename}, size: {len(content)} bytes")
        
        return attachments
    
    def _get_header(self, email_message, header_name: str) -> str:
        if header_name in email_message:
            return email_message[header_name]
        return ""
    
    def _decode_header(self, header: str) -> str:
        if not header:
            return ""
        
        decoded_parts = decode_header(header)
        result = ""
        
        for part, encoding in decoded_parts:
            if isinstance(part, bytes):
                if encoding:
                    result += part.decode(encoding, errors='ignore')
                else:
                    result += part.decode('utf-8', errors='ignore')
            else:
                result += part
        
        return result

def is_allowed_sender(from_email: str) -> bool:
    """Проверить, разрешен ли отправитель"""
    try:
        if '<' in from_email and '>' in from_email:
            from_email = from_email.split('<')[1].split('>')[0]
        
        from_email = from_email.strip()
        return from_email == 'noreply@eldis24.ru'
    
    except Exception as e:
        logger.error(f"Error checking sender: {e}")
        return False

def parse_subject(subject: str) -> tuple:
    """Парсить тему письма: 'DD-MM Объект Адрес (DD.MM.YYYY)' или 'Объект ул Адрес'"""
    if not subject:
        return None, None
    
    object_name = None
    address = None
    
    # Remove date pattern at the end: (DD.MM.YYYY)
    subject = re.sub(r'\s*\(\d{2}\.\d{2}\.\d{4}\)\s*$', '', subject)
    
    # Remove date pattern at the beginning: DD-MM
    subject = re.sub(r'^\d{2}-\d{2}\s+', '', subject)
    
    # Try to split by common address patterns
    # Pattern 1: "Объект ул Адрес"
    if ' ул ' in subject.lower():
        parts = re.split(r'\s+ул\s+', subject, 1, flags=re.IGNORECASE)
        if len(parts) == 2:
            object_name = parts[0].strip()
            address = f"ул {parts[1].strip()}"
    # Pattern 2: "Объект г Город ул Адрес"
    elif ' г ' in subject.lower() and ' ул ' in subject.lower():
        parts = re.split(r'\s+г\s+', subject, 1, flags=re.IGNORECASE)
        if len(parts) == 2:
            object_name = parts[0].strip()
            address = parts[1].strip()
    # Pattern 3: "Объект, Адрес" (old format)
    elif ',' in subject:
        parts = subject.split(',', 1)
        object_name = parts[0].strip()
        address = parts[1].strip() if len(parts) > 1 else None
    # Pattern 4: "Распечатка: Объект, Адрес" (old format)
    elif 'Распечатка:' in subject:
        parts = subject.split('Распечатка:')[1].strip()
        if ',' in parts:
            parts_list = parts.split(',', 1)
            object_name = parts_list[0].strip()
            address = parts_list[1].strip() if len(parts_list) > 1 else None
        else:
            object_name = parts.strip()
    else:
        # Fallback: use entire subject as object name
        object_name = subject.strip()
    
    return object_name, address


def extract_address_from_body(email_message) -> Optional[str]:
    """Извлечь адрес из тела письма"""
    try:
        for part in email_message.walk():
            content_type = part.get_content_type()
            if content_type == 'text/plain' or content_type == 'text/html':
                body = part.get_payload(decode=True)
                if body:
                    try:
                        text = body.decode('utf-8', errors='ignore')
                    except:
                        text = body.decode('windows-1251', errors='ignore')
                    
                    # Ищем адрес по паттернам
                    # Паттерн 1: "Адрес: ул. Ленина 1" или "Адрес: г. Москва, ул. Ленина, 1"
                    address_match = re.search(r'Адрес[:\s]+([^\n]+)', text, re.IGNORECASE)
                    if address_match:
                        address = address_match.group(1).strip()
                        # Очищаем от HTML тегов если есть
                        address = re.sub(r'<[^>]+>', '', address)
                        return address
                    
                    # Паттерн 2: Ищем "ул." или "улица" в тексте
                    street_match = re.search(r'(г\.\s*[^,]+[,\s]+)?(ул\.?\s*[^,\n]+(\d+)?)', text, re.IGNORECASE)
                    if street_match:
                        return street_match.group(0).strip()
                    
                    # Паттерн 3: Ищем типичные адресные паттерны
                    addr_patterns = [
                        r'г\.\s*[^,]+[,\s]+ул\.?\s*[^,\n]+',
                        r'ул\.?\s*[^,\n]+,\s*д\.?\s*\d+',
                        r'город\s+[^,]+[,\s]+улица\s+[^,\n]+',
                    ]
                    for pattern in addr_patterns:
                        match = re.search(pattern, text, re.IGNORECASE)
                        if match:
                            return match.group(0).strip()
        
        return None
    except Exception as e:
        logger.error(f"Error extracting address from body: {e}")
        return None

@celery_app.task(bind=True)
def fetch_emails_task(self):
    """Основная задача получения и обработки писем"""
    logger.info("Starting email fetch task")
    
    client = IMAPClient()
    db = None
    
    try:
        if not client.connect():
            logger.error("Failed to connect to IMAP")
            return {'status': 'error', 'message': 'IMAP connection failed'}
        
        messages = client.get_new_messages(days_back=1)
        logger.info(f"Got {len(messages)} messages")
        
        db = SessionLocal()
        processed_count = 0
        error_count = 0
        
        for msg_data in messages:
            try:
                logger.info(f"Processing message: subject='{msg_data['subject']}', attachments={len(msg_data.get('attachments', []))}")
                
                # Handle MDN (read receipt)
                if msg_data.get('is_mdn'):
                    original_msg_id = msg_data.get('original_message_id')
                    if original_msg_id:
                        # Find sent attachment by original_message_id
                        sent_attachments = db.query(Attachment).filter(
                            Attachment.original_message_id == original_msg_id
                        ).all()
                        
                        for att in sent_attachments:
                            att.read_receipt_received = True
                            att.read_receipt_at = datetime.now(timezone.utc)
                            logger.info(f"Read receipt received for attachment {att.id}")
                        
                        if sent_attachments:
                            db.commit()
                            logger.info(f"Updated {len(sent_attachments)} attachments with read receipt")
                    continue
                
                # Check if already exists
                existing = db.query(IncomingMessage).filter(
                    IncomingMessage.provider_message_id == msg_data['provider_message_id']
                ).first()
                
                if existing:
                    logger.info(f"Message {msg_data['provider_message_id']} already exists")
                    continue
                
                # Check sender and get source
                if not is_allowed_sender(msg_data['from_email']):
                    logger.info(f"Sender {msg_data['from_email']} not allowed")
                    continue
                
                # Get source_id for noreply@eldis24.ru
                source = db.query(EmailSource).filter(
                    EmailSource.email == 'noreply@eldis24.ru',
                    EmailSource.is_active == True
                ).first()
                
                if not source:
                    logger.error("Email source noreply@eldis24.ru not found")
                    continue
                
                # Parse subject
                object_name, address = parse_subject(msg_data['subject'])
                
                # Если адрес не найден в теме, пробуем извлечь из тела письма
                if not address and msg_data.get('raw_content'):
                    try:
                        email_msg = email.message_from_bytes(msg_data['raw_content'])
                        address = extract_address_from_body(email_msg)
                        if address:
                            logger.info(f"Extracted address from body: {address}")
                    except Exception as e:
                        logger.error(f"Error extracting address from body: {e}")
                
                # Create message
                message = IncomingMessage(
                    provider_message_id=msg_data['provider_message_id'],
                    source_id=source.id,
                    from_email=msg_data['from_email'],
                    subject=msg_data['subject'],
                    parsed_object=object_name,
                    parsed_address=address,
                    status='new',
                    received_at=msg_data['received_at']
                )
                
                db.add(message)
                db.commit()
                db.refresh(message)
                
                logger.info(f"Created message {message.id}")
                
                # Process attachments
                for att_data in msg_data.get('attachments', []):
                    try:
                        # Save file with unique name (add hash to prevent overwrite)
                        upload_path = UPLOAD_DIR
                        os.makedirs(upload_path, exist_ok=True)
                        
                        # Create sent_filename: "Объект - Адрес.pdf"
                        sent_filename = None
                        if object_name and address:
                            sent_filename = f"{object_name} - {address}.pdf"
                        elif object_name:
                            sent_filename = f"{object_name}.pdf"
                        else:
                            sent_filename = att_data['filename']
                        
                        # Clean filename from invalid characters
                        sent_filename = re.sub(r'[<>"/\\|?*]', '', sent_filename)
                        
                        # Ensure .pdf extension
                        if not sent_filename.lower().endswith('.pdf'):
                            sent_filename += '.pdf'
                        
                        # Use unique filename: hash_sent_filename.pdf
                        file_path = os.path.join(upload_path, f"{att_data['file_sha256'][:16]}_{sent_filename}")
                        with open(file_path, 'wb') as f:
                            f.write(att_data['content'])
                        
                        # Create attachment
                        attachment = Attachment(
                            message_id=message.id,
                            filename=att_data['filename'],
                            sent_filename=sent_filename,
                            file_path=file_path,
                            file_sha256=att_data['file_sha256'],
                            file_size=att_data['file_size'],
                            status='new'
                        )
                        
                        db.add(attachment)
                        db.commit()
                        
                        logger.info(f"Created attachment {attachment.id}")
                        
                        # Queue PDF validation
                        from pdf_validator import validate_pdf_attachment
                        validate_pdf_attachment.delay(attachment.id)
                        
                    except Exception as e:
                        logger.error(f"Error processing attachment: {e}")
                        db.rollback()
                        continue
                
                processed_count += 1
                
            except Exception as e:
                error_count += 1
                logger.error(f"Error processing message: {e}")
                if db:
                    db.rollback()
                continue
        
        client.disconnect()
        
        logger.info(f"Processed {processed_count} messages, {error_count} errors")
        return {
            'status': 'success',
            'processed': processed_count,
            'errors': error_count
        }
        
    except Exception as e:
        logger.error(f"Email fetch task failed: {e}")
        return {'status': 'error', 'message': str(e)}
    
    finally:
        client.disconnect()
        if db:
            db.close()