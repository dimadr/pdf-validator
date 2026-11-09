"""Actual legacy task wrappers under an explicit isolated runtime, not copied AST functions."""
import os,sys,importlib,uuid
from pathlib import Path
from sqlalchemy import select
from common.models import Attachment,ProcessingRevision

def modules(monkeypatch,session_factory):
    worker=Path(__file__).resolve().parents[1]/'worker';monkeypatch.syspath_prepend(str(worker))
    monkeypatch.setenv('DATABASE_URL',session_factory.kw['bind'].url.render_as_string(hide_password=False))
    monkeypatch.setenv('REDIS_URL','memory://')
    # dotenv must not load any local production credentials into task imports.
    import dotenv
    monkeypatch.setattr(dotenv,'load_dotenv',lambda *a,**k:False)
    sender=importlib.import_module('email_sender');validator=importlib.import_module('pdf_validator')
    monkeypatch.setattr(sender,'SessionLocal',session_factory);monkeypatch.setattr(validator,'SessionLocal',session_factory)
    from contextlib import contextmanager
    @contextmanager
    def get_db():
        with session_factory() as db:yield db
    monkeypatch.setattr(validator,'get_db',get_db)
    return sender,validator

def test_actual_legacy_sender_and_finalizer_are_fenced(session_factory,seed,monkeypatch):
    record=seed();sender,validator=modules(monkeypatch,session_factory)
    monkeypatch.setattr(sender.EmailSender,'send_email_with_pdf',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Legacy SMTP effect forbidden')))
    assert sender.send_pdf_attachment.run(str(record['attachment']))['status']=='skipped'
    assert validator.finalize_validation.run(str(record['attachment']))['status']=='skipped'
    assert validator.validate_pdf_attachment.run(str(record['attachment']))['status']=='skipped'
    monkeypatch.setenv('PIPELINE_V2_ENABLED','false')
    assert sender.send_pdf_attachment.run(str(record['attachment']))['status']=='skipped'
    assert validator.finalize_validation.run(str(record['attachment']))['status']=='skipped'
    with session_factory() as db:assert db.get(Attachment,record['attachment']).status=='sent'
