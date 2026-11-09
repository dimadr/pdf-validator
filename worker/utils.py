import os
import sys
import json
import hashlib
from logging.config import dictConfig
from datetime import datetime

# Add backend directory to path for models
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'backend'))

from logging_config import setup_logging

# Setup logging
logger = setup_logging(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format_type=os.getenv("LOG_FORMAT", "json")
)

def get_file_sha256(file_content: bytes) -> str:
    """Calculate SHA256 hash of file content"""
    return hashlib.sha256(file_content).hexdigest()

def sanitize_filename(filename: str) -> str:
    """Remove/replace dangerous characters from filename"""
    import re
    # Remove invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # Remove control characters
    filename = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', filename)
    # Limit length
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:200-len(ext)] + ext
    return filename

def ensure_upload_dir():
    """Create upload directory if it doesn't exist"""
    upload_dir = os.getenv("UPLOAD_DIR", "./uploads")
    os.makedirs(upload_dir, exist_ok=True)
    return upload_dir