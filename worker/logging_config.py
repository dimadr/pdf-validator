import logging
import os
import sys
from datetime import datetime, timezone

def setup_logging(level: str = "INFO", format_type: str = "json"):
    """Setup logging configuration"""
    
    log_level = getattr(logging, level.upper(), logging.INFO)
    
    if format_type.lower() == "json":
        import json
        
        class JSONFormatter(logging.Formatter):
            def format(self, record):
                log_obj = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "level": record.levelname,
                    "message": record.getMessage(),
                    "module": record.module,
                    "function": record.funcName,
                    "line": record.lineno
                }
                
                if hasattr(record, 'attachment_id'):
                    log_obj['attachment_id'] = record.attachment_id
                if hasattr(record, 'message_id'):
                    log_obj['message_id'] = record.message_id
                    
                return json.dumps(log_obj, ensure_ascii=False)
        
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
    
    # Configure root logger
    logger = logging.getLogger()
    logger.setLevel(log_level)
    
    # Console handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    return logger