"""Legacy client presentation only; object binding and recipients belong to the pipeline."""
import os
import re
from datetime import timezone, timedelta

MOSCOW = timezone(timedelta(hours=3))

def _line(value):
    return ' '.join(str(value or '').split())

def _pdf_name(value):
    value=re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]', '', str(value or '')).strip(' .')
    if value.lower().endswith('.pdf'):value=value[:-4].rstrip(' .')
    return (value or 'document')[:196]+'.pdf'

def attachment_name(attachment, obj):
    if attachment.sent_filename:
        return _pdf_name(attachment.sent_filename)
    message=attachment.message
    subject=_line(message.subject) if message else ''
    # Same subject-derived filename as legacy email_client.py. This text never routes mail.
    subject=re.sub(r'\s*\(\d{2}\.\d{2}\.\d{4}\)\s*$', '', subject)
    subject=re.sub(r'^\d{2}-\d{2}\s+', '', subject)
    if subject:
        if ' ул ' in subject.lower():
            name,address=re.split(r'\s+ул\s+',subject,1,flags=re.IGNORECASE)
            return _pdf_name(f'{name.strip()} - ул {address.strip()}')
        if ',' in subject:
            name,address=subject.split(',',1)
            return _pdf_name(f'{name.strip()} - {address.strip()}')
        if 'Распечатка:' in subject:subject=subject.split('Распечатка:',1)[1].strip()
        return _pdf_name(subject)
    name=_line(getattr(obj,'name',None)) or 'Документ';address=_line(getattr(obj,'address',None))
    return _pdf_name(f'{name} - {address}' if address else name)

def client_payload(attachment, obj):
    """Use the already verified object; freeze content in the existing delivery intent."""
    name=_line(obj.name) or 'Документ';address=_line(obj.address) or 'Без адреса'
    subject=f'Отчет {name} - {address}' if address!='Без адреса' else f'Отчет {name}'
    message=attachment.message
    received=message.received_at if message else None
    date_line=''
    if received is not None:
        if received.tzinfo is None:received=received.replace(tzinfo=timezone.utc)
        date_line='\n\nДата получения сообщения: '+received.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M')
    body=f'''Здравствуйте!

Мы запускаем автоматическую рассылку отчётов. Если вы обнаружили какие-либо неточности в отчёте, пожалуйста, сообщите об этом:
    по электронной почте: {_line(os.getenv('ADMIN_EMAIL'))}

Во вложении находится отчет:
Адрес: {address}{date_line}

---
Это автоматическое сообщение, пожалуйста, не отвечайте на него.'''
    return {'subject':subject,'body':body,'filename':attachment_name(attachment,obj)}

def review_payload(attachment,obj,state,reason,result=None):
    """Administrative counterpart of the legacy Russian rejection template."""
    message=attachment.message
    name=_line(getattr(obj,'name',None) or (message.parsed_object if message else None)) or 'Документ'
    address=_line(getattr(obj,'address',None) or (message.parsed_address if message else None)) or 'Без адреса'
    labels={'missing_measurements':'Отсутствуют обязательные измерения',
        'identity_missing':'Не определён полный номер прибора','identity_ambiguous':'Неоднозначный номер прибора',
        'exact_object_not_found':'Не найден объект с точным номером прибора',
        'invalid_client_recipients':'Не определены корректные получатели',
        'empty_cells':'Пустые ячейки измерений','dates_invalid':'Нет валидных дат в документе'}
    code=_line(reason or state);explanation=labels.get(code,'Документ требует проверки')
    details='\n'.join('- '+_line(issue.get('code','')) for issue in (result or {}).get('issues',[])[:10])
    subject=('ОШИБКА: ' if state=='rejected' else 'Требуется проверка: ')+name
    body=f'Здравствуйте!\n\nВо вложении находится документ по объекту: {name}\nАдрес: {address}\n\n{explanation}.\nПричина: {code}'
    if details:body+='\n\nПодробности:\n'+details
    body+='\n\n---\nЭто автоматическое сообщение, пожалуйста, не отвечайте на него.'
    return {'subject':subject,'body':body,'filename':attachment_name(attachment,obj)}
