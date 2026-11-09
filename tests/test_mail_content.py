"""Client presentation regression; no production PDF or external SMTP is used."""
import uuid
from types import SimpleNamespace as Data
from datetime import datetime, timezone
from email import message_from_bytes, policy
from sqlalchemy import select, func
from common.mail_content import client_payload
from common.models import Attachment, Object, IncomingMessage, DeliveryIntent, RecipientDelivery, OutboxEvent
from common.pipeline import validate_revision
from common.service import request_resend
from common.delivery import deliver_recipient
from common.smtp_transport import SMTPTransport
from test_local_protocols import running, SMTPHandler

def vichuga(sent_filename=None):
    return Data(sent_filename=sent_filename,message=Data(
        subject='01-31 ЖСК "Заря-2" г Вичуга ул Ульяновская 36 (01.11.2026)',
        received_at=datetime(2026,11,3,6,39,54))),Data(name='ЖСК Заря-2',address='г Вичуга ул Ульяновская 36')

def test_exact_historical_name_and_client_template():
    attachment,obj=vichuga();payload=client_payload(attachment,obj)
    assert payload['filename']=='ЖСК Заря-2 г Вичуга - ул Ульяновская 36.pdf'
    assert payload['subject']=='Отчет ЖСК Заря-2 - г Вичуга ул Ульяновская 36'
    assert 'Здравствуйте!' in payload['body'] and '03.11.2026 09:39' in payload['body']
    assert 'Processing decision' not in payload['body']
    assert attachment.sent_filename is None

def test_saved_filename_has_priority_and_pdf_extension_is_preserved():
    attachment,obj=vichuga('Ивановская обл, Вичуга г, Ульяновская ул, д. 36.pdf')
    assert client_payload(attachment,obj)['filename']==attachment.sent_filename
    attachment.sent_filename='x'*300+'.pdf'
    assert len(client_payload(attachment,obj)['filename'])==200
    assert client_payload(attachment,obj)['filename'].endswith('.pdf')
    attachment.sent_filename='../../bad\r\nname.pdf';obj.name='Object\r\nBcc: other@example.invalid'
    payload=client_payload(attachment,obj)
    assert '/' not in payload['filename'] and '\n' not in payload['filename'] and '\n' not in payload['subject']

def configure(factory, record):
    with factory() as db:
        with db.begin():
            attachment=db.get(Attachment,record['attachment']);obj=db.get(Object,record['object'])
            model_attachment,model_object=vichuga()
            obj.name=model_object.name;obj.address=model_object.address
            obj.email='service.nd@example.invalid,dv@example.invalid'
            message=db.get(IncomingMessage,attachment.message_id)
            message.subject=model_attachment.message.subject;message.received_at=model_attachment.message.received_at

def test_normal_pipeline_and_explicit_resend_share_format_without_rerouting(session_factory,seed):
    record=seed(state='received');configure(session_factory,record)
    result=validate_revision(session_factory,record['revision'],lambda *a,**k:{
        'state':'validated','identity_state':'unique','calculator_number':record['serial']})
    assert result['state']=='validated'
    with session_factory() as db:
        intent=db.scalar(select(DeliveryIntent));original=dict(intent.payload)
        assert original['filename']=='ЖСК Заря-2 г Вичуга - ул Ульяновская 36.pdf'
        assert sorted(db.scalars(select(RecipientDelivery.recipient)))==['dv@example.invalid','service.nd@example.invalid']
        assert set(db.scalars(select(RecipientDelivery.role)))=={'client'}
        intent_id=intent.id
    assert validate_revision(session_factory,record['revision'])['state']=='skipped'
    with session_factory() as db:
        with db.begin():
            second=request_resend(db,record['attachment'],'synthetic-explicit-resend',record['revision'])
            assert {k:v for k,v in second.payload.items() if k!='command'}==original
        assert db.get(DeliveryIntent,intent_id).payload==original
        assert db.scalar(select(func.count()).select_from(DeliveryIntent))==2

def test_real_mime_preserves_cyrillic_name_pdf_bytes_and_single_effect(session_factory,seed,monkeypatch):
    record=seed(state='received');configure(session_factory,record)
    validate_revision(session_factory,record['revision'],lambda *a,**k:{
        'state':'validated','identity_state':'unique','calculator_number':record['serial']})
    with session_factory() as db:
        ids=list(db.scalars(select(RecipientDelivery.id)))
    with running(SMTPHandler) as server:
        monkeypatch.setenv('SMTP_SERVER','127.0.0.1');monkeypatch.setenv('SMTP_PORT',str(server.server_address[1]))
        monkeypatch.setenv('SMTP_TEST_MODE','true');monkeypatch.setenv('SMTP_FROM','sender@example.invalid');monkeypatch.delenv('SMTP_USER',raising=False)
        for id in ids:
            assert deliver_recipient(session_factory,id,SMTPTransport())['state']=='accepted'
            assert deliver_recipient(session_factory,id,SMTPTransport())['state']=='skipped'
        assert len(server.received)==2
        for raw in server.received:
            message=message_from_bytes(raw,policy=policy.default)
            assert message['Subject']=='Отчет ЖСК Заря-2 - г Вичуга ул Ульяновская 36'
            pdf=next(message.iter_attachments())
            assert pdf.get_filename()=='ЖСК Заря-2 г Вичуга - ул Ульяновская 36.pdf'
            assert pdf.get_payload(decode=True)==record['path'].read_bytes()
