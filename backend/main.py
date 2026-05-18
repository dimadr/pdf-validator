from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List, Optional
import os
from dotenv import load_dotenv

from database import get_db, create_tables
from models import Object, EmailSource, IncomingMessage, Attachment, Report
from logging_config import setup_logging

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
)

@app.on_event("startup")
async def startup_event():
    logger.info("Starting Email Processor API")
    # Create tables if they don't exist
    create_tables()

@app.get("/")
async def root():
    return {"message": "Email Processor API", "version": "1.0.0"}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

# Include routers first to test imports, then schemas are loaded via routers
from routers import objects, email_sources, messages, reports, actions, auth, settings

# Import schemas after routers to avoid circular imports
from schemas import (
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