# Проект PDF - Email Processor

## Архитектура

### Общая структура
```
project1/
├── backend/       # FastAPI приложение
├── worker/       # Celery воркеры
├── ui/           # Веб-интерфейс
├── uploads/      # Загруженные PDF файлы
├── backup/       # Резервные копии
└── docker-compose.yml
```

## Директории

### backend/
API сервер на FastAPI. Запускается на порту 8000.

**Роутеры:**
- `auth.py` - авторизация, пользователи, роли
- `objects.py` - объекты (данные об объектах)
- `email_sources.py` - разрешённые email источники
- `messages.py` - входящие письма, вложения
- `reports.py` - отчёты
- `settings.py` - настройки (AI, email, trusted emails)
- `actions.py` - действия (переотправка, перепроверка)

**Модели (models.py):**
- `Object` - объекты
- `IncomingMessage` - входящие письма
- `Attachment` - вложения (PDF)
- `EmailSource` - email источники
- `User` - пользователи с ролями (admin/operator)
- `TrustedEmail` - доверенные email адреса
- `Setting` - настройки (key-value)
- `Report` - отчёты об отклонениях

### worker/
Celery воркеры для фоновых задач.

**Основные скрипты:**
- `celery_app.py` - точка входа Celery
- `tasks.py` - задачи Celery
- `email_client.py` - получение почты по IMAP
- `email_sender.py` - отправка писем
- `attachment_processor.py` - обработка PDF вложений
- `pdf_validator.py` - валидация PDF
- `gpt_validator.py` - AI валидация
- `scheduler.py` - расписание задач

### ui/
Веб-интерфейс (HTML + JavaScript).

**Файлы:**
- `index.html` - главная страница
- `app.js` - JavaScript логика
- `nginx.conf` - конфиг nginx

## Роли пользователей

### admin
Полный доступ:
- Создание/редактирование/удаление объектов
- Управление пользователями
- Все настройки
- Просмотр всех данных

### operator
Ограниченный доступ:
- Только просмотр объектов
- Просмотр документов и отчётов
- Нет доступа к настройкам
- Нет доступа к управлению объектами

## Поля объекта

| Поле | Описание |
|------|---------|
| `eldis_id` | ID объекта в системе Элдис (не UUID!) |
| `name` | Название объекта |
| `calculator_number` | Номер вычислителя |
| `address` | Адрес объекта |
| `email` | Email для отправки отчётов |
| `object_date` | Период отчёта (напр. "23-24") |

## Отслеживание уведомлений о прочтении

При отправке письма:
1. Генерируется уникальный `Message-ID`
2. В БД сохраняется в поле `original_message_id`
3. При получении MDN (read receipt) - обновляется `read_receipt_received = true`

**Поля в Attachment:**
- `original_message_id` - Message-ID отправленного письма
- `read_receipt_received` - флаг получения уведомления
- `read_receipt_at` - время получения

## Правила для новых моделей

При создании новой модели:

1. Добавить в `backend/models.py`
2. Создать миграцию Alembic в `backend/alembic/versions/`
3. Добавить схему Pydantic в `backend/schemas.py`
4. Добавить API endpoints в соответствующий router
5. Добавить UI компоненты если нужно
6. Задокументировать в этом файле

### Пример миграции
```python
"""Add new feature

Revision ID: XXXX
Revises: XXXX
Create Date: YYYY-MM-DD HH:MM:SS

"""
from alembic import op
import sqlalchemy as sa

revision = 'XXXX'
down_revision = 'XXXX'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column('table_name', sa.Column('new_column', sa.String(255)))

def downgrade() -> None:
    op.drop_column('table_name', 'new_column')
```

## История изменений

См. `history.log` в корне проекта.

## API Endpoints

### Auth
- `POST /api/v1/auth/login` - вход
- `POST /api/v1/auth/logout` - выход
- `GET /api/v1/auth/me` - текущий пользователь
- `GET /api/v1/auth/users` - список пользователей (admin)
- `POST /api/v1/auth/register` - регистрация (admin)

### Objects
- `GET /api/v1/objects` - список объектов
- `POST /api/v1/objects` - создать объект (admin)
- `PUT /api/v1/objects/{id}` - обновить (admin)
- `DELETE /api/v1/objects/{id}` - удалить (admin)

### Documents
- `GET /api/v1/attachments` - список вложений
- `GET /api/v1/attachments/{id}` - детали вложения
- `POST /api/v1/attachments/{id}/resend` - переотправить (admin)

### Settings
- `GET /api/v1/settings/ai-config` - AI настройки (admin)
- `PUT /api/v1/settings/ai-config` - сохранить AI настройки (admin)
- `GET /api/v1/settings/email-config` - email настройки (admin)
- `PUT /api/v1/settings/email-config` - сохранить email настройки (admin)

## Cron задачи (Celery Beat)

- `check-emails-scheduled` - каждые 2 минуты - проверка почты
- `process-attachments` - каждые 5 минут - обработка вложений
- `send-approved-attachments` - каждые 5 минут - отправка одобренных
