#!/usr/bin/env python3
import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Attachment, Object
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/email_processor")

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
db = Session()

def link_attachments_to_objects():
    """Связать вложения с объектами по номеру вычислителя"""
    attachments = db.query(Attachment).filter(
        Attachment.calculator_number.isnot(None),
        Attachment.object_id.is_(None)
    ).all()
    
    updated_count = 0
    skipped_count = 0
    
    for attachment in attachments:
        try:
            # Find object by calculator number
            obj = db.query(Object).filter(
                Object.calculator_number == attachment.calculator_number,
                Object.is_active == True
            ).first()
            
            if obj:
                attachment.object_id = obj.id
                db.commit()
                print(f"Linked attachment {attachment.id} to object {obj.name} (calculator: {attachment.calculator_number})")
                updated_count += 1
            else:
                print(f"Skipped attachment {attachment.id}: no object found for calculator {attachment.calculator_number}")
                skipped_count += 1
                
        except Exception as e:
            print(f"Error processing {attachment.id}: {e}")
            db.rollback()
            continue
    
    print(f"\nSummary:")
    print(f"Updated: {updated_count}")
    print(f"Skipped: {skipped_count}")
    
    db.close()

if __name__ == "__main__":
    link_attachments_to_objects()