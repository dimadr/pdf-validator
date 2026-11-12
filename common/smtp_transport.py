"""One independent SMTP connection per recipient; fail conservatively after DATA begins."""
import smtplib,ssl
from email.message import EmailMessage
from email.policy import SMTP
from common.delivery import DefinitiveFailure,AmbiguousFailure
from common.smtp_settings import credentials

class RecordDataReply:
    def data(self, message):
        result=super().data(message)
        self.submission_reply=result
        return result

class RecordedSMTP(RecordDataReply,smtplib.SMTP):
    pass

class RecordedSMTPSSL(RecordDataReply,smtplib.SMTP_SSL):
    pass

def safe_reply(value,secret):
    text=value.decode('utf-8','replace') if isinstance(value,bytes) else str(value)
    if secret:text=text.replace(secret,'[redacted]')
    return ' '.join(text.split())[:1000]

class SMTPTransport:
    def send(self,work):
        try:cfg=credentials(work.get('provider','timeweb'))
        except (OSError,ValueError,KeyError):
            raise DefinitiveFailure('Selected SMTP configuration is unavailable',False,'smtp_config') from None
        if cfg['test'] and cfg['host'] not in ('127.0.0.1','localhost','smtp'):
            raise DefinitiveFailure('SMTP test mode requires an isolated local sink',False,'smtp_config')
        message=EmailMessage();message['From']=cfg['sender'];message['To']=work['recipient']
        message['Reply-To']=cfg['reply_to'];message['Date']=work['date'];message['Message-ID']=work['message_id']
        message['Disposition-Notification-To']=cfg['sender'];message['X-Priority']='3'
        message['Subject']=work['payload'].get('subject','PDF report')
        message.set_content(work['payload'].get('body','PDF report attached'))
        filename=work['payload'].get('filename','report.pdf')
        subtype='vnd.ms-excel' if isinstance(filename,str) and filename.lower().endswith('.xls') else 'pdf'
        message.add_attachment(work['content'],maintype='application',subtype=subtype,filename=filename)
        server=None;submission_started=False
        try:
            context=ssl.create_default_context()
            if cfg['port']==465:
                server=RecordedSMTPSSL(cfg['host'],cfg['port'],timeout=15,context=context)
            else:
                server=RecordedSMTP(cfg['host'],cfg['port'],timeout=15)
                if not cfg['test']:server.starttls(context=context)
            if cfg['user']:server.login(cfg['user'],cfg['password'] or '')
            submission_started=True
            refused=server.sendmail(cfg['sender'],[work['recipient']],message.as_bytes(policy=SMTP))
            if refused:raise DefinitiveFailure('Recipient refused',False)
            code,response=server.submission_reply
            return {'code':code,'response':safe_reply(response,cfg['password'])}
        except (smtplib.SMTPRecipientsRefused,smtplib.SMTPResponseException) as exc:
            code=getattr(exc,'smtp_code',None);reply=getattr(exc,'smtp_error',b'')
            if isinstance(exc,smtplib.SMTPRecipientsRefused):code,reply=next(iter(exc.recipients.values()))
            response=safe_reply(reply,cfg['password'])
            quota=work.get('provider')=='brevo' and code is not None and 400<=code<500 and any(
                term in response.lower() for term in ('quota','limit','credit'))
            raise DefinitiveFailure('SMTP explicit rejection '+str(code),code is not None and 400<=code<500,
                'smtp_quota' if quota else 'smtp_rejected',code,response) from None
        except DefinitiveFailure:raise
        except Exception:
            if submission_started:raise AmbiguousFailure('SMTP submission result unknown') from None
            raise DefinitiveFailure('SMTP connection/authentication failed before submission',True,'smtp_connect') from None
        finally:
            if server:
                try:server.quit()
                except Exception:server.close()
