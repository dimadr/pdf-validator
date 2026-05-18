# Worker документация

## Структура

```
worker/
├── celery_app.py         # Точка входа Celery
├── celeryconfig.py       # Конфигурация Celery
├── tasks.py              # Celery задачи
├── scheduler.py           # Расписание задач
├── database.py           # Подключение к БД
├── models.py             # SQLAlchemy модели
├── email_client.py       # Получение почты по IMAP
├── email_sender.py       # Отправка писем
├── attachment_processor.py # Обработка PDF
├── pdf_validator.py      # Валидация PDF
├── gpt_validator.py       # AI валидация через OpenAI
├── ai_client.py          # AI клиент
├── logging_config.py     # Настройка логирования
├── utils.py              # Утилиты
├── maintenance.py         # Обслуживание
├── rename_attachments.py # Переименование
├── link_attachments.py   # Связывание
├── reparse_messages.py   # Перепарсинг
├── requirements.txt
└── Dockerfile
```

## Celery Beat Schedule

```python
# scheduler.py
beat_schedule = {
    'check-emails-scheduled': {
        'task': 'email_client.fetch_emails_task',
        'schedule': crontab(minute='*/2'),
    },
    'process-attachments': {
        'task': 'tasks.process_pending_attachments',
        'schedule': crontab(minute='*/5'),
    },
    'send-approved-attachments': {
        'task': 'tasks.send_approved_attachments',
        'schedule': crontab(minute='*/5'),
    },
}
```

## Задачи Celery

### email_client.fetch_emails_task
- Подключение к IMAP
- Получение новых писем
- Парсинг вложений PDF
- Создание записей Attachment
- Обработка MDN (уведомлений о прочтении)

### tasks.process_pending_attachments
- Поиск вложений со статусом 'new'
- Валидация PDF
- AI проверка через GPT
- Определение объекта
- Установка статуса validated/rejected

### tasks.send_approved_attachments
- Поиск вложений со статусом 'validated'
- Формирование email с PDF
- Отправка через SMTP
- Обновление статуса на 'sent'
- Сохранение Message-ID

## Email Sender

```python
# email_sender.py
class EmailSender:
    def __init__(self):
        self.username = config['email_username']
        self.smtp_server = config['smtp_server']
        self.smtp_port = config['smtp_port']
        self.smtp_ssl = config['smtp_ssl']
        self.from_name = config['email_from_name']
    
    def send_email_with_pdf(self, to_email, subject, body, pdf_path, pdf_filename):
        # Отправка email с вложением
        # Добавление заголовков для MDN
        pass
```

## PDF обработка

```python
# attachment_processor.py
def process_attachment(attachment_id):
    # 1. Извлечение текста из PDF
    # 2. Парсинг таблиц
    # 3. Поиск номера прибора
    # 4. Валидация формата
    pass

# pdf_validator.py
def validate_pdf(file_path):
    # Проверка что это PDF
    # Проверка структуры
    pass
```

## Конфигурация из БД

Настройки читаются из таблицы `settings`:
- `imap_server`, `imap_port`, `imap_ssl`
- `smtp_server`, `smtp_port`, `smtp_ssl`
- `email_username`, `email_password`, `email_from_name`

## Логирование

Логи выводятся в stdout контейнера. Просмотр:
```bash
docker compose logs worker --tail=100
```

## Запуск вручную

```bash
# Подключиться к контейнеру
docker compose exec worker bash

# Запустить задачу
python -c "import email_client; email_client.fetch_emails_task()"

# Переотправить конкретное вложение
python -c "from email_sender import send_pdf_attachment; send_pdf_attachment('UUID')"
```
