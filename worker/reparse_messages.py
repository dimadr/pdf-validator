#!/usr/bin/env python3
import os
import re
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import IncomingMessage
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/email_processor")

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
db = Session()

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

def reparse_messages():
    """Репарсить существующие сообщения"""
    messages = db.query(IncomingMessage).all()
    
    updated_count = 0
    skipped_count = 0
    
    for message in messages:
        try:
            object_name, address = parse_subject(message.subject)
            
            # Skip if already has data
            if message.parsed_object == object_name and message.parsed_address == address:
                skipped_count += 1
                continue
            
            # Update message
            message.parsed_object = object_name
            message.parsed_address = address
            db.commit()
            
            print(f"Updated: {message.subject}")
            print(f"  Object: {object_name}")
            print(f"  Address: {address}")
            print()
            
            updated_count += 1
            
        except Exception as e:
            print(f"Error processing {message.id}: {e}")
            db.rollback()
            continue
    
    print(f"\nSummary:")
    print(f"Updated: {updated_count}")
    print(f"Skipped: {skipped_count}")
    
    db.close()

if __name__ == "__main__":
    reparse_messages()