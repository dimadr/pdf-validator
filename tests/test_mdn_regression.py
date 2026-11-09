import pytest
from sqlalchemy import select
from common.models import RecipientDelivery,Attachment,OutboxEvent
from common.ingest import BlobStore,ingest_uid
from common.delivery import deliver_recipient
from test_delivery_core import planned,SMTP
from test_local_protocols import running,SMTPHandler
from common.smtp_transport import SMTPTransport
from email import message_from_bytes,policy

def test_client_mime_requests_receipt_using_sender_address(session_factory,seed,monkeypatch):
    delivery=planned(session_factory,seed())[0]
    with running(SMTPHandler) as sink:
        for k,v in {'SMTP_SERVER':'127.0.0.1','SMTP_PORT':str(sink.server_address[1]),'SMTP_FROM':'sender@example.invalid','SMTP_TEST_MODE':'true','SMTP_USER':''}.items():monkeypatch.setenv(k,v)
        deliver_recipient(session_factory,delivery,SMTPTransport())
        message=message_from_bytes(sink.received[0],policy=policy.default)
        assert message['Disposition-Notification-To']=='sender@example.invalid'
        assert message['X-Priority']=='3'

@pytest.mark.parametrize('recipient,displayed,expected',[('one@example.invalid',True,True),('wrong@example.invalid',True,False),('one@example.invalid',False,False)])
def test_structured_mdn_restores_read_flag_without_reprocessing(session_factory,seed,tmp_path,recipient,displayed,expected):
    record=seed();delivery=planned(session_factory,record)[0];deliver_recipient(session_factory,delivery,SMTP())
    with session_factory() as db:mid=db.get(RecipientDelivery,delivery).message_id
    disposition='displayed' if displayed else 'deleted'
    raw=(f'From: {recipient}\r\nTo: sender@example.invalid\r\nMIME-Version: 1.0\r\n'
         'Content-Type: multipart/report; report-type=disposition-notification; boundary="mdn"\r\n\r\n'
         '--mdn\r\nContent-Type: text/plain\r\n\r\nReceipt\r\n'
         '--mdn\r\nContent-Type: message/disposition-notification\r\n\r\n'
         f'Final-Recipient: rfc822; {recipient}\r\nOriginal-Message-ID: {mid}\r\n'
         f'Disposition: manual-action/MDN-sent-manually; {disposition}\r\n\r\n--mdn--\r\n').encode()
    ingest_uid(session_factory,BlobStore(tmp_path/'store'),'account','INBOX',1,9,raw)
    with session_factory() as db:
        attachment=db.get(Attachment,record['attachment'])
        assert bool(attachment.read_receipt_received)==expected
        assert db.get(RecipientDelivery,delivery).attempts==1
