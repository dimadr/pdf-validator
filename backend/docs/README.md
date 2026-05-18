# Backend документация

## Структура

```
backend/
├── main.py              # Точка входа FastAPI
├── database.py          # Подключение к БД
├── models.py            # SQLAlchemy модели
├── schemas.py           # Pydantic схемы
├── logging_config.py    # Настройка логирования
├── alembic/             # Миграции БД
│   ├── env.py
│   └── versions/
│       ├── 0001_initial_migration.py
│       ├── 0002_add_read_receipt_tracking.py
│       └── 0003_add_email_config_settings.py
├── routers/            # API endpoints
│   ├── auth.py         # Авторизация
│   ├── objects.py      # Объекты
│   ├── email_sources.py # Email источники
│   ├── messages.py     # Письма и вложения
│   ├── reports.py       # Отчёты
│   ├── settings.py      # Настройки
│   └── actions.py       # Действия
├── requirements.txt
├── Dockerfile
└── alembic.ini
```

## API Endpoints

### /api/v1/auth/*
- `POST /login` - авторизация по логину/паролю
- `POST /logout` - выход
- `GET /me` - текущий пользователь
- `GET /users` - список пользователей (admin)
- `GET /users/{id}` - пользователь (admin)
- `PUT /users/{id}` - обновить пользователя (admin)
- `DELETE /users/{id}` - удалить пользователя (admin)
- `POST /register` - создать пользователя (admin)

### /api/v1/objects/*
- `GET /` - список объектов (фильтры: period, sort_by)
- `POST /` - создать объект (admin)
- `GET /{id}` - объект
- `PUT /{id}` - обновить объект (admin)
- `DELETE /{id}` - удалить объект (admin)

### /api/v1/email-sources/*
- `GET /` - список источников
- `POST /` - добавить источник (admin)
- `PUT /{id}` - обновить источник (admin)
- `DELETE /{id}` - удалить источник (admin)
- `POST /{id}/disable` - отключить источник (admin)

### /api/v1/attachments/*
- `GET /` - список вложений (фильтры: status, object_id)
- `GET /{id}` - вложение с объектом и письмом
- `GET /{id}/details` - детали вложения

### /api/v1/settings/*
- `GET /trusted-emails` - список доверенных email (admin)
- `POST /trusted-emails` - добавить (admin)
- `PUT /trusted-emails/{id}` - обновить (admin)
- `DELETE /trusted-emails/{id}` - удалить (admin)
- `GET /ai-config` - AI настройки (admin)
- `PUT /ai-config` - сохранить AI настройки (admin)
- `GET /email-config` - email настройки (admin)
- `PUT /email-config` - сохранить email настройки (admin)

## Модели базы данных

### User
| Поле | Тип | Описание |
|------|-----|---------|
| id | UUID | PK |
| username | String(50) | Уникальный логин |
| password_hash | String(255) | Хеш пароля |
| email | String(255) | Email |
| role | String(20) | admin или operator |
| is_active | Boolean | Активен |
| created_at | DateTime | Создан |

### Object
| Поле | Тип | Описание |
|------|-----|---------|
| id | UUID | PK |
| eldis_id | String(50) | ID в системе Элдис |
| name | String(255) | Название |
| name_norm | String(255) | Нормализованное название |
| calculator_number | String(50) | Номер вычислителя |
| address | String(500) | Адрес |
| email | String(255) | Email для отправки |
| object_date | String(20) | Период отчёта |
| is_active | Boolean | Активен |
| created_at | DateTime | Создан |

### Attachment
| Поле | Тип | Описание |
|------|-----|---------|
| id | UUID | PK |
| message_id | UUID | FK на письмо |
| object_id | UUID | FK на объект (nullable) |
| filename | String(255) | Имя файла |
| file_path | String(500) | Путь к файлу |
| file_sha256 | String(64) | Хеш файла |
| status | String(20) | new/processing/validated/rejected/sent |
| sent_to_email | String(255) | Кому отправлено |
| sent_at | DateTime | Когда отправлено |
| original_message_id | String(255) | Message-ID для трекинга MDN |
| read_receipt_received | Boolean | Уведомление о прочтении |
| read_receipt_at | DateTime | Когда получено уведомление |
| created_at | DateTime | Создан |

### Setting
| Поле | Тип | Описание |
|------|-----|---------|
| id | UUID | PK |
| key | String(100) | Уникальный ключ |
| value | Text | Значение |
| description | String(255) | Описание |
| updated_at | DateTime | Обновлён |

**Известные ключи настроек:**
- `router_ai_url` - URL RouterAI
- `router_ai_key` - API ключ RouterAI
- `neuro_api_url` - URL NeuroApi
- `neuro_api_key` - API ключ NeuroApi
- `imap_server` - IMAP сервер
- `imap_port` - IMAP порт
- `imap_ssl` - IMAP SSL (true/false)
- `smtp_server` - SMTP сервер
- `smtp_port` - SMTP порт
- `smtp_ssl` - SMTP SSL (true/false)
- `email_username` - Email для отправки
- `email_password` - Пароль / App Password
- `email_from_name` - Имя отправителя

## Аутентификация

JWT токены. Токен хранится в localStorage браузера.

```python
# Роли
SECRET_KEY = os.getenv("SECRET_KEY", "...")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 часа
```

## Создание миграции

```bash
docker compose exec backend alembic revision --autogenerate -m "description"
```

Или вручную создать файл в `alembic/versions/`.
