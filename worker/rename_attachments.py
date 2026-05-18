#!/usr/bin/env python3
import os
import re
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Attachment, IncomingMessage
from dotenv import load_dotenv
import shutil

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/email_processor")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
db = Session()

def clean_filename(filename: str) -> str:
    """Очистить имя файла от недопустимых символов"""
    return re.sub(r'[<>"/\\|?*]', '', filename)

def rename_attachments():
    """Переименовать существующие файлы по адресу"""
    attachments = db.query(Attachment).all()
    
    renamed_count = 0
    skipped_count = 0
    error_count = 0
    
    for attachment in attachments:
        try:
            # Get message data
            message = db.query(IncomingMessage).filter(IncomingMessage.id == attachment.message_id).first()
            
            if not message:
                print(f"Skipping {attachment.id}: no message found")
                skipped_count += 1
                continue
            
            # Create new filename from address
            if message.parsed_address:
                new_filename = f"{message.parsed_address}.pdf"
            elif message.parsed_object:
                new_filename = f"{message.parsed_object}.pdf"
            else:
                print(f"Skipping {attachment.id}: no parsed data")
                skipped_count += 1
                continue
            
            # Clean filename
            new_filename = clean_filename(new_filename)
            
            # Ensure .pdf extension
            if not new_filename.lower().endswith('.pdf'):
                new_filename += '.pdf'
            
            # Get current file path
            current_path = attachment.file_path
            if not current_path or not os.path.exists(current_path):
                print(f"Skipping {attachment.id}: file not found at {current_path}")
                skipped_count += 1
                continue
            
            # Create new path with hash prefix
            hash_prefix = attachment.file_sha256[:16]
            new_path = os.path.join(UPLOAD_DIR, f"{hash_prefix}_{new_filename}")
            
            # Skip if already has correct name
            if current_path == new_path:
                print(f"Skipping {attachment.id}: already has correct name")
                skipped_count += 1
                continue
            
            # Rename file
            print(f"Renaming: {os.path.basename(current_path)} -> {os.path.basename(new_path)}")
            shutil.move(current_path, new_path)
            
            # Update database
            attachment.sent_filename = new_filename
            attachment.file_path = new_path
            db.commit()
            
            renamed_count += 1
            
        except Exception as e:
            print(f"Error processing {attachment.id}: {e}")
            error_count += 1
            db.rollback()
            continue
    
    print(f"\nSummary:")
    print(f"Renamed: {renamed_count}")
    print(f"Skipped: {skipped_count}")
    print(f"Errors: {error_count}")
    
    db.close()

if __name__ == "__main__":
    rename_attachments()