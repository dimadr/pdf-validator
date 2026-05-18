from celery_app import celery_app
from utils import logger
from database import get_db
from models import Attachment, IncomingMessage
from datetime import datetime, timezone, timedelta
from sqlalchemy import text
import os
import subprocess

@celery_app.task(bind=True, name="maintenance.cleanup_old_files_task")
def cleanup_old_files_task(self):
    """Очистка каждое утро - запускает полную очистку"""
    logger.info("Starting daily cleanup task")
    
    results = {
        'status': 'success',
        'cleanup_results': {}
    }
    
    # 1. Очистка старых файлов
    try:
        cleanup_result = cleanup_old_files.delay()
        results['cleanup_results']['old_files'] = 'scheduled'
    except Exception as e:
        logger.error(f"Failed to schedule old files cleanup: {e}")
        results['cleanup_results']['old_files'] = f'error: {e}'
    
    # 2. Очистка логов (если они копятся)
    try:
        # Очищаем старые логи старше 7 дней
        log_dirs = ['/var/log', './logs', '/tmp']
        cleaned_logs = 0
        for log_dir in log_dirs:
            if os.path.exists(log_dir):
                # Здесь можно добавить логику очистки логов
                pass
        results['cleanup_results']['logs'] = 'checked'
    except Exception as e:
        logger.error(f"Failed to cleanup logs: {e}")
        results['cleanup_results']['logs'] = f'error: {e}'
    
    logger.info(f"Daily cleanup task completed: {results}")
    return results

@celery_app.task(bind=True)
def cleanup_old_files(self):
    """Удалить старые файлы и записи"""
    logger.info("Starting cleanup task")
    
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=30)  # Keep 30 days
    
    with get_db() as db:
        try:
            # Delete old attachments
            old_attachments = db.query(Attachment).filter(
                Attachment.created_at < cutoff_date,
                Attachment.status.in_(['sent', 'rejected'])
            ).all()
            
            deleted_files = 0
            for attachment in old_attachments:
                # Delete physical file
                if attachment.file_path and os.path.exists(attachment.file_path):
                    try:
                        os.remove(attachment.file_path)
                        deleted_files += 1
                    except Exception as e:
                        logger.error(f"Error deleting file {attachment.file_path}: {e}")
                
                # Delete database record
                db.delete(attachment)
            
            db.commit()
            
            logger.info(f"Cleanup completed: {len(old_attachments)} attachments, {deleted_files} files deleted")
            return {
                'status': 'success',
                'attachments_deleted': len(old_attachments),
                'files_deleted': deleted_files
            }
            
        except Exception as e:
            logger.error(f"Cleanup task failed: {e}")
            return {'status': 'error', 'message': str(e)}

@celery_app.task(bind=True)
def health_check(self):
    """Проверить здоровье системы"""
    logger.info("Starting health check")
    
    health_status = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'services': {},
        'overall_status': 'healthy'
    }
    
    with get_db() as db:
        try:
            # Check database connection
            db.execute(text("SELECT 1"))
            health_status['services']['database'] = 'healthy'
        except Exception as e:
            health_status['services']['database'] = f'unhealthy: {str(e)}'
            health_status['overall_status'] = 'unhealthy'
        
        try:
            # Check message processing
            recent_messages = db.query(IncomingMessage).filter(
                IncomingMessage.received_at > datetime.now(timezone.utc) - timedelta(hours=1)
            ).count()
            health_status['services']['message_processing'] = {
                'status': 'healthy',
                'recent_messages': recent_messages
            }
        except Exception as e:
            health_status['services']['message_processing'] = f'unhealthy: {str(e)}'
            health_status['overall_status'] = 'unhealthy'
        
        try:
            # Check attachment processing
            pending_attachments = db.query(Attachment).filter(
                Attachment.status == 'new'
            ).count()
            health_status['services']['attachment_processing'] = {
                'status': 'healthy',
                'pending_attachments': pending_attachments
            }
        except Exception as e:
            health_status['services']['attachment_processing'] = f'unhealthy: {str(e)}'
            health_status['overall_status'] = 'unhealthy'
    
    logger.info(f"Health check completed: {health_status['overall_status']}")
    return health_status