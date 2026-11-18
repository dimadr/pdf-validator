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
    assert payload['filename']=='ЖСК Заря2 — г Вичуга ул Ульяновская 36.pdf'
    assert payload['subject']=='Отчет ЖСК Заря-2 - г Вичуга ул Ульяновская 36'
    assert 'Здравствуйте!' in payload['body'] and '03.11.2026 09:39' in payload['body']
    assert 'Processing decision' not in payload['body']
    assert attachment.sent_filename is None

def test_object_address_overrides_saved_name_and_subject():
    attachment,obj=vichuga('Имя из темы.pdf')
    expected='ЖСК Заря2 — г Вичуга ул Ульяновская 36.pdf'
    for subject in ('Тест', 'Другой адрес', '01-31 Несвязанный объект'):
        attachment.message.subject=subject
        assert client_payload(attachment,obj)['filename']==expected
    obj.address='x'*300
    assert len(client_payload(attachment,obj)['filename'])==100
    assert client_payload(attachment,obj)['filename'].endswith('.pdf')
    obj.address='../../улица\r\nBcc: other@example.invalid'
    payload=client_payload(attachment,obj)
    assert '/' not in payload['filename'] and '\n' not in payload['filename']


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
        assert original['filename']=='ЖСК Заря2 — г Вичуга ул Ульяновская 36.pdf'
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
            assert pdf.get_filename()=='ЖСК Заря2 — г Вичуга ул Ульяновская 36.pdf'
            assert pdf.get_payload(decode=True)==record['path'].read_bytes()


def test_kosareva_and_another_object_use_card_address():
    attachment,_=vichuga('Взято из темы.pdf')
    attachment.file_path='/synthetic/report.pdf'
    attachment.filename='Потребление ресурса (Объект).pdf'
    for name,address,expected in [
        ('УК РусФонд','г Иваново ул Косарева 5','УК РусФонд — г Иваново ул Косарева 5.pdf'),
        ('Другой объект','г Иваново ул Примерная 10','Другой объект — г Иваново ул Примерная 10.pdf'),
    ]:
        obj=Data(name=name,address=address)
        for subject in ('Тест', 'Адрес из темы не совпадает'):
            attachment.message.subject=subject
            result=client_payload(attachment,obj)
            assert result['filename']==expected
            assert result['subject']==f'Отчет {name} - {address}'
            assert f'Адрес: {address}' in result['body']


def test_xls_names_and_review_template_stay_identical():
    from common.mail_content import attachment_name,review_payload

    attachment,obj=vichuga('Старое имя.pdf')
    attachment.file_path='/synthetic/Отчёт.xls'
    attachment.filename='Газовый отчёт.xls'
    for subject in ('Тест', 'Иное письмо'):
        attachment.message.subject=subject
        assert attachment_name(attachment,obj)=='Газовый отчёт.xls'
        assert client_payload(attachment,obj)['filename']=='Газовый отчёт.xls'
        new=review_payload(attachment,obj,'review_required','identity_missing',{})
        assert new['filename']=='Газовый отчёт.xls'
        assert new['subject']=='Eldis — проверка газового отчёта XLS'


def test_mime_filename_roundtrip_for_multiple_recipients_without_network(monkeypatch):
    import common.smtp_transport as smtp_module

    attachment,_=vichuga('Имя темы.pdf')
    obj=Data(name='УК РусФонд',address='г Иваново ул Косарева 5')
    payload=client_payload(attachment,obj)
    content=b'%PDF-unchanged-original-payload\x00\xff'
    captured=[]
    connections=[]

    class Capture:
        def __init__(self,*args,**kwargs):
            connections.append(self)
            self.submission_reply=(250,b'Captured locally')
        def starttls(self,**kwargs): pass
        def login(self,*args): pass
        def sendmail(self,sender,recipients,raw):
            captured.append((recipients,raw))
            return {}
        def quit(self): pass

    monkeypatch.setattr(smtp_module,'credentials',lambda provider:{
        'test':True,'host':'localhost','port':587,'sender':'sender@example.invalid',
        'reply_to':'sender@example.invalid','user':None,'password':None,
    })
    monkeypatch.setattr(smtp_module,'RecordedSMTP',Capture)
    for recipient in ('one@example.invalid','two@example.invalid'):
        SMTPTransport().send({
            'recipient':recipient,'message_id':f'<{uuid.uuid4()}@example.invalid>',
            'date':'Wed, 18 Nov 2026 09:00:00 +0300','payload':payload,'content':content,
        })
    assert len(connections)==len(captured)==2
    for recipients,raw in captured:
        assert len(recipients)==1
        for parser_policy in (policy.default,policy.compat32):
            message=message_from_bytes(raw,policy=parser_policy)
            parts=[part for part in message.walk() if part.get_content_disposition()=='attachment']
            assert len(parts)==1
            assert parts[0].get_filename()=='УК РусФонд — г Иваново ул Косарева 5.pdf'
            assert parts[0].get_payload(decode=True)==content
            assert parts[0].get_content_type()=='application/pdf'
        assert b"filename*=" in raw or b"filename*0*=" in raw
