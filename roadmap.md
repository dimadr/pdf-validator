# Roadmap: Аутентификация и Настройки

## 1. Аутентификация (JWT)

### Backend
- [ ] Добавить модель User в `backend/models.py`
- [ ] Создать `backend/routers/auth.py`
  - POST /api/v1/auth/login
  - POST /api/v1/auth/logout
  - GET /api/v1/auth/me
- [ ] Создать middleware для защиты API
- [ ] Добавить эндпоинт /api/v1/users (CRUD)

### UI
- [ ] Добавить форму логина на главной странице
- [ ] Реализовать хранение токена (localStorage)
- [ ] Добавить проверку аутентификации при загрузке
- [ ] Кнопка "Выйти"

---

## 2. Блок настроек

### Backend
- [ ] Добавить модель TrustedEmail в `backend/models.py`
- [ ] Добавить модель Settings в `backend/models.py`
- [ ] Создать `backend/routers/settings.py`
  - GET/POST /api/v1/settings/trusted-emails
  - GET/PUT /api/v1/settings/ai-config

### UI
- [ ] Добавить раздел "Настройки" в меню
- [ ] Создать вкладку "Пользователи"
  - Таблица пользователей
  - Кнопки: добавить, редактировать, удалить
- [ ] Создать вкладку "Доверенные адреса"
  - Список email
  - Форма добавления/редактирования
- [ ] Создать вкладку "AI/API"
  - Поля: RouterAI URL, RouterAI Key
  - Поля: NeuroApi URL, NeuroApi Key
  - Кнопка сохранения

---

## 3. База данных

- [ ] Миграция: создать таблицы users, trusted_emails, settings
- [ ] Заполнить настройки по умолчанию
- [ ] Создать первого администратора

---

## 4. Тестирование

- [ ] Проверить регистрацию/вход
- [ ] Проверить доступ к API только для авторизованных
- [ ] Проверить CRUD настроек
- [ ] Проверить UI
