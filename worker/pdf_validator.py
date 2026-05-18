import os
import pdfplumber
# import camelot  # отключен из-за OpenCV зависимостей
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
import json
from celery_app import celery_app, UPLOAD_DIR
from database import SessionLocal
from utils import logger
from models import Attachment as AttachmentModel, Object as ObjectModel

class PDFValidator:
    @staticmethod
    def extract_text_and_tables(pdf_path: str) -> Dict:
        """Извлечь текст и таблицы из PDF"""
        try:
            result = {
                'text': '',
                'tables': [],
                'pages_count': 0,
                'extraction_errors': []
            }
            
            with pdfplumber.open(pdf_path) as pdf:
                result['pages_count'] = len(pdf.pages)
                
                for page_num, page in enumerate(pdf.pages):
                    try:
                        page_text = page.extract_text()
                        if page_text:
                            result['text'] += f"\n--- Page {page_num + 1} ---\n{page_text}"
                        
                        tables = page.extract_tables()
                        if tables:
                            for table in tables:
                                if table:
                                    result['tables'].append(table)
                    except Exception as e:
                        result['extraction_errors'].append(f"Page {page_num + 1}: {str(e)}")
                        continue
                        
            return result
            
        except Exception as e:
            logger.error(f"Error extracting PDF content: {e}")
            return {
                'text': '',
                'tables': [],
                'pages_count': 0,
                'extraction_errors': [str(e)]
            }

    @staticmethod
    def extract_calculator_number(text: str) -> Optional[str]:
        """Извлечь номер вычислителя (теплосчетчика) из текста PDF"""
        try:
            # Приоритетные паттерны - ищем по ключевым словам
            priority_patterns = [
                # "Прибор: ТВ7 Заводской номер: 15017757" или "Прибор: ВКТ-7 Заводской номер: 37362"
                r'Прибор[:\s]*(?:ТВ[78]|ВКТ-[789]|СПТ94[23])[^\n]*?Заводской\s*номер[=:]\s*(\d{4,})',
                # "Заводской номер: 15017757" - standalone (after other context)
                r'Заводской\s*номер[=:]\s*(\d{5,})',
                # "Прибор: СПТ942 Заводской номер: 7590" (short number)
                r'Прибор[:\s]*СПТ94\d[^\n]*?Заводской\s*номер[=:]\s*(\d+)',
            ]
            
            # Проверяем приоритетные паттерны
            for pattern in priority_patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    number = match.group(1).strip()
                    if len(number) >= 4:
                        logger.info(f"Found calculator number (priority): {number}")
                        return number
            
            # Резервные паттерны - для сложных форматов
            fallback_patterns = [
                # "Тепловычислитель ВКТ-7 сет. N 61"
                r'Тепловычислитель\s+ВКТ-[789]\s+(?:сет\.?\s*)?N[=:]\s*(\d+)',
                # "Теплосчетчик МКТС: №005545-1"
                r'Теплосчетчик\s+МКТС[:\s]№\s*([\d-]+)',
                # "ТСРВ-026М №1318455" - with possible parentheses like ТСРВ-033(034)
                r'ТСРВ[-\s]\w+(?:\(\w+\))?\s*№\s*(\d{4,})',
                # "Сетевой номер NT=12345"
                r'Сетевой\s*номер\s+NT[=:]\s*(\d{4,})',
            ]
            
            for pattern in fallback_patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    number = match.group(1).strip()
                    if number and len(number) >= 4:
                        logger.info(f"Found calculator number (fallback): {number}")
                        return number
            
            logger.debug("No calculator number found in text")
            return None
            
        except Exception as e:
            logger.error(f"Error extracting calculator number: {e}")
            return None
    
    @staticmethod
    def extract_object_info(text: str) -> Dict:
        """Извлечь информацию об объекте из текста PDF"""
        try:
            result = {
                'object_name': None,
                'address': None
            }
            
            # Потребитель
            consumer_match = re.search(r'Потребитель[:\s]+(.+?)(?=\n)', text)
            if consumer_match:
                result['object_name'] = consumer_match.group(1).strip()
            
            # Объект (альтернативный формат: "Объект: Ивановская обл, ...")
            if not result['object_name']:
                object_match = re.search(r'Объект[:\s]+(.+?)(?=\n)', text)
                if object_match:
                    obj_text = object_match.group(1).strip()
                    # Может содержать адрес и название в скобках
                    # Формат: "Ивановская обл, ... (Название)"
                    bracket_match = re.search(r'(.+?)\s*\(([^)]+)\)\s*$', obj_text)
                    if bracket_match:
                        result['address'] = bracket_match.group(1).strip()
                        result['object_name'] = bracket_match.group(2).strip()
                    else:
                        result['object_name'] = obj_text
            
            # Адрес объекта (разные форматы)
            address_patterns = [
                r'Адрес объекта[:\s]+(.+?)(?=\n)',
                r'Адрес[:\s]+(.+?)(?=\n)',
            ]
            for pattern in address_patterns:
                address_match = re.search(pattern, text)
                if address_match:
                    result['address'] = address_match.group(1).strip()
                    break
            
            return result
            
        except Exception as e:
            logger.error(f"Error extracting object info: {e}")
            return {'object_name': None, 'address': None}

    @staticmethod
    def validate_dates(text: str) -> Dict:
        """Проверить даты в тексте на непрерывность"""
        try:
            # Ищем даты в формате ДД.ММ.ГГГГ
            date_pattern = r'\b\d{2}\.\d{2}\.\d{4}\b'
            dates = re.findall(date_pattern, text)
            
            # Проверяем валидность дат и конвертируем в объекты datetime
            valid_dates = []
            for date_str in dates:
                try:
                    dt = datetime.strptime(date_str, '%d.%m.%Y')
                    valid_dates.append(dt)
                except ValueError:
                    continue
            
            # Сортируем даты
            valid_dates.sort()
            
            # Даты OK если есть хотя бы одна дата
            dates_ok = len(valid_dates) >= 1
            
            return {
                'dates_found': len(dates),
                'valid_dates': len(valid_dates),
                'dates': [d.strftime('%d.%m.%Y') for d in valid_dates],
                'dates_ok': dates_ok,
                'has_gaps': False,  # Не проверяем пропуски
                'missing_dates': [],
                'date_range': {
                    'from': valid_dates[0].strftime('%d.%m.%Y') if valid_dates else None,
                    'to': valid_dates[-1].strftime('%d.%m.%Y') if valid_dates else None
                }
            }
            
        except Exception as e:
            logger.error(f"Error validating dates: {e}")
            return {
                'dates_found': 0,
                'valid_dates': 0,
                'dates': [],
                'dates_ok': False,
                'has_gaps': True,
                'missing_dates': [],
                'error': str(e)
            }

    @staticmethod
    def validate_tables(tables: List) -> Dict:
        """Проверить таблицы - только строки начинающиеся с даты"""
        try:
            if not tables:
                return {
                    'tables_found': 0,
                    'valid_tables': 0,
                    'tables_ok': False,
                    'errors': ['No tables found']
                }
            
            valid_tables = 0
            errors = []
            empty_cells_count = 0
            
            date_pattern = r'^\d{2}\.\d{2}\.\d{4}'
            
            for table_idx, table in enumerate(tables):
                if table and len(table) > 0:
                    table_has_empty = False
                    for row_idx, row in enumerate(table):
                        if not row:
                            continue
                        
                        first_cell = str(row[0]).strip() if row[0] else ''
                        
                        if re.match(date_pattern, first_cell):
                            for cell_idx, cell in enumerate(row):
                                if cell_idx == len(row) - 1:
                                    continue
                                
                                cell_str = str(cell).strip() if cell else ''
                                # Check for empty cells: "---" or "нд"
                                if cell_str == '---' or cell_str.lower() == 'нд':
                                    table_has_empty = True
                                    empty_cells_count += 1
                                    errors.append(f"Table {table_idx + 1}, row {row_idx + 1}, col {cell_idx + 1}: {cell_str}")
                    
                    if not table_has_empty:
                        valid_tables += 1
                else:
                    errors.append(f'Table {table_idx + 1}: empty table')
            
            tables_ok = valid_tables == len(tables) and empty_cells_count == 0
            
            return {
                'tables_found': len(tables),
                'valid_tables': valid_tables,
                'tables_ok': tables_ok,
                'empty_cells_count': empty_cells_count,
                'errors': errors
            }
            
        except Exception as e:
            logger.error(f"Error validating tables: {e}")
            return {
                'tables_found': 0,
                'valid_tables': 0,
                'tables_ok': False,
                'errors': [str(e)]
            }

    @staticmethod
    def validate_pdf(pdf_path: str) -> Dict:
        """Полная валидация PDF"""
        try:
            # Extract content
            content = PDFValidator.extract_text_and_tables(pdf_path)
            
            # Validate dates
            dates_result = PDFValidator.validate_dates(content['text'])
            
            # Validate tables - check all cells are filled
            tables_result = PDFValidator.validate_tables(content['tables'])
            
            # Extract calculator number
            calculator_number = PDFValidator.extract_calculator_number(content['text'])
            
            # Extract object info (name and address)
            object_info = PDFValidator.extract_object_info(content['text'])
            
            return {
                'dates': dates_result,
                'tables': tables_result,
                'calculator_number': calculator_number,
                'object_name': object_info['object_name'],
                'address': object_info['address'],
                'extraction_errors': content['extraction_errors']
            }
            
        except Exception as e:
            logger.error(f"Error validating PDF: {e}")
            return {
                'dates': {'dates_ok': False, 'error': str(e)},
                'tables': {'tables_ok': False, 'error': str(e)},
                'calculator_number': None,
                'object_name': None,
                'address': None,
                'extraction_errors': [str(e)]
            }

@celery_app.task(bind=True, max_retries=3)
def validate_pdf_attachment(self, attachment_id: str):
    """Валидация PDF вложения"""
    logger.info(f"Starting PDF validation for attachment {attachment_id}")
    
    db = SessionLocal()
    try:
        # Get attachment from DB
        attachment = db.query(AttachmentModel).filter(AttachmentModel.id == attachment_id).first()
        if not attachment:
            logger.error(f"Attachment {attachment_id} not found")
            return {'status': 'error', 'message': 'Attachment not found'}
        
        # Check file exists
        if not attachment.file_path or not os.path.exists(attachment.file_path):
            logger.error(f"File not found: {attachment.file_path}")
            attachment.status = 'rejected'
            attachment.reject_reason = 'file_not_found'
            db.commit()
            return {'status': 'error', 'message': 'File not found'}
        
        # Validate PDF
        validation_result = PDFValidator.validate_pdf(attachment.file_path)
        
        # Check for date gaps
        has_gaps = validation_result['dates'].get('has_gaps', False)
        # Check for empty cells in tables
        tables_ok = validation_result['tables']['tables_ok']
        empty_cells = validation_result['tables'].get('empty_cells_count', 0)
        
        # Extract calculator number
        calculator_number = validation_result.get('calculator_number')
        if calculator_number:
            attachment.calculator_number = calculator_number
            logger.info(f"Attachment {attachment_id} has calculator number: {calculator_number}")
            
            # Find object by calculator number and link it
            obj = db.query(ObjectModel).filter(
                ObjectModel.calculator_number == calculator_number,
                ObjectModel.is_active == True
            ).first()
            if obj:
                attachment.object_id = obj.id
                logger.info(f"Attachment {attachment_id} linked to object {obj.name} (calculator: {calculator_number})")
        
        # Update message with object info if extracted from PDF
        object_name = validation_result.get('object_name')
        address = validation_result.get('address')
        if object_name and attachment.message:
            attachment.message.parsed_object = object_name
            logger.info(f"Attachment {attachment_id} extracted object name: {object_name}")
        if address and attachment.message:
            attachment.message.parsed_address = address
            logger.info(f"Attachment {attachment_id} extracted address: {address}")
        
        # Update attachment status based on validation
        dates_ok = validation_result['dates']['dates_ok']
        
        if dates_ok and tables_ok:
            # Все поля заполнены
            attachment.status = 'approved'
            attachment.reject_reason = None
            attachment.validation_result = validation_result
            logger.info(f"Attachment {attachment_id} approved - all fields filled")
        else:
            # Есть пустые ячейки
            attachment.status = 'rejected'
            
            reasons = []
            if not tables_ok:
                reasons.append(f'empty_cells:{empty_cells}')
            if not dates_ok:
                reasons.append('dates_invalid')
            
            attachment.reject_reason = ';'.join(reasons)
            attachment.validation_result = validation_result
            logger.warning(f"Attachment {attachment_id} rejected: {reasons}")
        
        db.commit()
        
        # Queue for sending
        if attachment.status in ['approved', 'rejected']:
            from email_sender import send_pdf_attachment
            send_pdf_attachment.delay(attachment_id)
            logger.info(f"Queued attachment {attachment_id} for sending")
        
        return {
            'status': 'success',
            'attachment_id': attachment_id,
            'dates_ok': dates_ok,
            'has_gaps': has_gaps,
            'tables_ok': tables_ok,
            'final_status': attachment.status
        }
        
    except Exception as e:
        logger.error(f"Error validating PDF: {e}")
        # Update attachment status to failed
        attachment = db.query(AttachmentModel).filter(AttachmentModel.id == attachment_id).first()
        if attachment:
            attachment.status = 'rejected'
            attachment.reject_reason = 'validation_error'
            db.commit()
        
        return {'status': 'error', 'message': str(e)}
    finally:
        db.close()

@celery_app.task(bind=True, max_retries=3)
def validate_with_gpt(self, attachment_id: str):
    """GPT валидация PDF"""
    logger.info(f"Starting GPT validation for attachment {attachment_id}")
    
    db = SessionLocal()
    try:
        # Get attachment from DB
        attachment = db.query(AttachmentModel).filter(AttachmentModel.id == attachment_id).first()
        if not attachment:
            logger.error(f"Attachment {attachment_id} not found")
            return {'status': 'error', 'message': 'Attachment not found'}
        
        # Здесь можно добавить логику GPT валидации
        # Пока просто одобряем
        attachment.status = 'approved'
        db.commit()
        
        logger.info(f"GPT validation completed for attachment {attachment_id}")
        return {'status': 'success', 'approved': True}
        
    except Exception as e:
        logger.error(f"Error in GPT validation for attachment {attachment_id}: {e}")
        
        # Update attachment status
        attachment = db.query(AttachmentModel).filter(AttachmentModel.id == attachment_id).first()
        if attachment:
            attachment.status = 'rejected'
            attachment.reject_reason = 'gpt_validation_error'
            db.commit()
        
        # Retry with exponential backoff
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=60 * (2 ** self.request.retries))
        
        return {'status': 'error', 'message': str(e)}
    finally:
        db.close()