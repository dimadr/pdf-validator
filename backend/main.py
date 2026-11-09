from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List, Optional
import os
from dotenv import load_dotenv

from backend.database import get_db, require_backend_schema
from sqlalchemy import text, select, func
from common.config import enabled, require_schema, PipelineUnavailable
from common.models import RecipientDelivery, OutboxEvent, ProcessingRevision, User
from common.models import Object, EmailSource, IncomingMessage, Attachment, Report
from backend.logging_config import setup_logging

load_dotenv()

# Setup logging
logger = setup_logging(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format_type=os.getenv("LOG_FORMAT", "json")
)

app = FastAPI(
    title="Email Processor API",
    description="API для обработки PDF-вложений из email",
    version="1.0.0"
)

# CORS middleware
cors_origins = os.getenv("CORS_ORIGINS", "http://localhost").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in cors_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count"],
)

@app.on_event("startup")
async def startup_event():
    logger.info("Starting Email Processor API")

@app.get("/")
async def root():
    return {"message": "Email Processor API", "version": "1.0.0"}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get('/health/ready')
def readiness(db:Session=Depends(get_db)):
    db.execute(text('SELECT 1'))
    require_backend_schema(db)
    if enabled():
        try:
            require_schema(db)
        except PipelineUnavailable as exc:
            raise HTTPException(status_code=503,detail=str(exc)) from exc
    return {'status':'ready','database':'ready','schema':'0010','pipeline_v2_enabled':enabled(),
            'scope':'API and schema readiness; SMTP inbox delivery is not asserted'}

# Include routers first to test imports, then schemas are loaded via routers
from backend.routers import objects, email_sources, messages, reports, actions, auth, settings


@app.get('/api/v1/pipeline/status')
def pipeline_status(db:Session=Depends(get_db),current_user:User=Depends(auth.get_current_user)):
    states=dict(db.execute(select(RecipientDelivery.state,func.count()).group_by(RecipientDelivery.state)).all())
    pending=db.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.published_at.is_(None)))
    technical=db.scalar(select(func.count()).select_from(ProcessingRevision).where(ProcessingRevision.state=='technical_error'))
    return {'pipeline_v2_enabled':enabled(),'recipient_states':states,'pending_outbox':pending,
            'technical_errors':technical,'alerts':{'uncertain':states.get('uncertain',0),
            'failed':states.get('failed',0),'technical_errors':technical}}

# Import schemas after routers to avoid circular imports
from backend.schemas import (
    Object, ObjectCreate, ObjectUpdate,
    EmailSource, EmailSourceCreate, EmailSourceUpdate
)

app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(settings.router, prefix="/api/v1/settings", tags=["settings"])
app.include_router(objects.router, prefix="/api/v1/objects", tags=["objects"])
app.include_router(email_sources.router, prefix="/api/v1/email-sources", tags=["email-sources"])
app.include_router(messages.router, prefix="/api/v1", tags=["messages"])
app.include_router(reports.router, prefix="/api/v1/reports", tags=["reports"])
app.include_router(actions.router, prefix="/api/v1/attachments", tags=["actions"])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
